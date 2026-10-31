"""Historical backfill: load past Hacker News stories for every `hn_search` source.

The live poller only sees the newest 50 stories per query. Algolia's HN Search API keeps the full
history, but returns at most 1,000 hits per query, so we walk each query backwards through time
windows small enough to stay under that cap. Every story goes through the same `store_items` path as
live ingestion (S3 objects, exact dedupe, case-sensitive alias matching, `job_runs`), so backfilled
signals are indistinguishable from live ones and the funnel counts stay honest: stories whose title or
text doesn't mention a tracked alias are stored as `filtered_out`, never padded into the relevant set.

`typoTolerance=false` makes Algolia match the query word exactly, so we don't ingest fuzzy junk
("Asian" for "Asana") just to inflate counts.

    uv run python -m signal_scout.backfill hn --since 2016-01-01        # make backfill
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlencode, urlparse

from sqlalchemy import select

from .config import get_settings
from .db.models import Competitor, Source, SourceType
from .ingest.feeds import parse_hn
from .ingest.fetch_source import IngestContext, _SourceSnapshot, store_items
from .ingest.matching import CompetitorMatcher
from .jobruns import RunStats, track_job_run
from .log import configure_logging, get_logger
from .runtime import ingest_context

log = get_logger(__name__)

HN_SEARCH = "https://hn.algolia.com/api/v1/search_by_date"
MAX_HITS_PER_QUERY = 1000  # Algolia's pagination cap
PAGE_SIZE = 1000


def query_of(url: str) -> str:
    return parse_qs(urlparse(url).query)["query"][0]


async def _count(ctx: IngestContext, query: str, start: int, end: int) -> int:
    params = {
        "query": query,
        "tags": "story",
        "hitsPerPage": 0,
        "typoTolerance": "false",
        "numericFilters": f"created_at_i>={start},created_at_i<{end}",
    }
    result = await ctx.http.get(f"{HN_SEARCH}?{urlencode(params)}")
    count: int = json.loads(result.body)["nbHits"]
    return count


async def _windows(ctx: IngestContext, query: str, start: int, end: int) -> list[tuple[int, int]]:
    """Split [start, end) into windows with fewer than MAX_HITS_PER_QUERY stories each."""
    if await _count(ctx, query, start, end) < MAX_HITS_PER_QUERY or end - start <= 86400:
        return [(start, end)]
    mid = (start + end) // 2
    return await _windows(ctx, query, start, mid) + await _windows(ctx, query, mid, end)


async def backfill_hn_source(
    ctx: IngestContext, source: _SourceSnapshot, matcher: CompetitorMatcher, since: datetime, run: RunStats
) -> int:
    query = query_of(source.url)
    start, end = int(since.timestamp()), int(datetime.now(UTC).timestamp())
    stored = 0
    for lo, hi in await _windows(ctx, query, start, end):
        page = 0
        while True:
            params = {
                "query": query,
                "tags": "story",
                "hitsPerPage": PAGE_SIZE,
                "page": page,
                "typoTolerance": "false",
                "numericFilters": f"created_at_i>={lo},created_at_i<{hi}",
            }
            result = await ctx.http.get(f"{HN_SEARCH}?{urlencode(params)}")
            body = json.loads(result.body)
            items = parse_hn(result.body)
            run.items_in += len(items)
            counts = await store_items(ctx, source, items, matcher, run)
            stored += sum(v for k, v in counts.items() if k != "already_stored")
            page += 1
            if page >= body.get("nbPages", 0):
                break
    return stored


async def backfill_hn(since: datetime, only: str | None = None) -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    async with ingest_context(settings) as ctx:
        async with ctx.sessions() as session:
            aliases = {c.name: c.aliases for c in (await session.execute(select(Competitor))).scalars()}
            rows = (
                await session.execute(
                    select(Source, Competitor.name)
                    .outerjoin(Competitor, Competitor.id == Source.competitor_id)
                    .where(Source.type == SourceType.hn_search, Source.enabled.is_(True))
                    .where(Source.name == only if only else Source.id.is_not(None))
                    .order_by(Source.id)
                )
            ).all()
        matcher = CompetitorMatcher(aliases)
        for source, competitor in rows:
            snapshot = _SourceSnapshot(
                id=source.id,
                name=source.name,
                type=source.type,
                url=source.url,
                etag=None,
                last_modified=None,
                consecutive_failures=source.consecutive_failures,
                competitor_name=competitor,
            )
            # Wall-clock, like job_runs (time.monotonic pauses while macOS sleeps, which overstated throughput).
            started = datetime.now(UTC)
            async with track_job_run(ctx.sessions, "backfill_hn", source=source.name) as run:
                stored = await backfill_hn_source(ctx, snapshot, matcher, since, run)
                elapsed_h = max((datetime.now(UTC) - started).total_seconds(), 1e-6) / 3600
                run.items_out = stored
                run.details.update(
                    source=source.name,
                    query=query_of(source.url),
                    since=since.date().isoformat(),
                    new_signals=stored,
                    signals_per_hour=round(stored / elapsed_h),
                )
            log.info("backfill_source_done", source=source.name, fetched=run.items_in, new_signals=stored)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="kind", required=True)
    hn = sub.add_parser("hn", help="backfill Hacker News stories for every hn_search source")
    hn.add_argument("--since", default="2016-01-01", help="earliest story date (YYYY-MM-DD)")
    hn.add_argument("--only", help='backfill a single source by name, e.g. "HN: Linear"')
    args = parser.parse_args()
    since = datetime.fromisoformat(args.since).replace(tzinfo=UTC)
    if args.kind == "hn":
        asyncio.run(backfill_hn(since, args.only))


if __name__ == "__main__":
    main()
