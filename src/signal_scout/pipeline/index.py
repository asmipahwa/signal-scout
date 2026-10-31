"""The `index_insights` job: copy insights from Postgres into Elasticsearch.

Postgres is the source of truth; the search index is derived from it. Normal runs upsert insights
changed since the last successful index run. A full run (`--full`, or payload {"full": true}) builds
a fresh versioned index from Postgres and moves the `insights` alias to it, so the index can be
deleted or remapped at any time and rebuilt with nothing lost.

    uv run python -m signal_scout.pipeline.index --full     # make reindex
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from elasticsearch import AsyncElasticsearch
from elasticsearch.helpers import async_bulk
from sqlalchemy import func, select

from ..config import get_settings
from ..db.models import Competitor, Insight, InsightSignal, JobRun, JobRunStatus, Signal, Source
from ..ingest.fetch_source import IngestContext
from ..jobruns import RunStats, track_job_run
from ..log import configure_logging
from ..runtime import ingest_context
from ..search.mapping import INDEX_NAME, index_body, versioned_index_name
from .common import SIGNAL_SOURCE_TYPE, elasticsearch_client

BULK_CHUNK = 500
SIGNALS_TEXT_CHARS = 400  # per member signal, to keep documents small


async def ensure_index(es: AsyncElasticsearch, *, recreate: bool = False) -> str:
    """Make sure the versioned index exists and the `insights` alias points at it."""
    name = versioned_index_name()
    if recreate and await es.indices.exists(index=name):
        await es.indices.delete(index=name)
    if not await es.indices.exists(index=name):
        await es.indices.create(index=name, **index_body())
    if not await es.indices.exists_alias(name=INDEX_NAME, index=name):
        await es.indices.put_alias(index=name, name=INDEX_NAME)
    return name


async def _last_success(ctx: IngestContext) -> datetime | None:
    async with ctx.sessions() as session:
        result: datetime | None = (
            await session.execute(
                select(func.max(JobRun.started_at)).where(
                    JobRun.job_type == "index_insights", JobRun.status == JobRunStatus.success
                )
            )
        ).scalar_one()
    return result


async def _documents(ctx: IngestContext, index: str, since: datetime | None) -> AsyncIterator[dict[str, Any]]:
    async with ctx.sessions() as session:
        stmt = select(Insight, Competitor.name).outerjoin(Competitor, Competitor.id == Insight.competitor_id)
        if since is not None:
            stmt = stmt.where(Insight.updated_at >= since)
        insights = (await session.execute(stmt)).all()
        member_rows = (
            await session.execute(
                select(InsightSignal.insight_id, Signal.title, Signal.excerpt, Signal.url, Source.type)
                .join(Signal, Signal.id == InsightSignal.signal_id)
                .join(Source, Source.id == Signal.source_id)
                .where(InsightSignal.insight_id.in_([i.id for i, _ in insights]))
            )
        ).all()
    members: dict[int, list[tuple[str, str, str, str]]] = defaultdict(list)
    for insight_id, title, excerpt, url, stype in member_rows:
        members[insight_id].append((title, excerpt, url, SIGNAL_SOURCE_TYPE[stype]))
    for insight, competitor in insights:
        sigs = members.get(insight.id, [])
        yield {
            "_index": index,
            "_id": str(insight.id),
            "_source": {
                "insight_id": str(insight.id),
                "headline": insight.title,
                "summary": insight.summary,
                "why_it_matters": insight.why_it_matters,
                "signals_text": " ".join(f"{t} {x[:SIGNALS_TEXT_CHARS]}" for t, x, _, _ in sigs),
                "competitor": competitor or "",
                "competitor_suggest": {"input": [competitor]} if competitor else {"input": ["unknown"]},
                "category": insight.category,
                "importance": insight.importance,
                "sentiment": insight.sentiment,
                "rank_score": insight.score,
                "signal_count": insight.signal_count,
                "source_types": insight.source_types,
                "source_urls": [u for _, _, u, _ in sigs],
                "first_seen": insight.first_seen.isoformat() if insight.first_seen else None,
                "last_seen": insight.last_seen.isoformat() if insight.last_seen else None,
            },
        }


async def index_insights(
    ctx: IngestContext, payload: dict[str, Any], run: RunStats, es: AsyncElasticsearch | None = None
) -> None:
    full = bool(payload.get("full"))
    client = es or elasticsearch_client(ctx.settings)
    try:
        index = await ensure_index(client, recreate=full)
        since = None if full else await _last_success(ctx)
        ok, errors = await async_bulk(
            client, _documents(ctx, index, since), chunk_size=BULK_CHUNK, raise_on_error=False, refresh="wait_for"
        )
        failed = errors if isinstance(errors, int) else len(errors)
        run.items_out = ok
        run.items_in = ok + failed
        if failed:
            run.record_error(f"{failed} documents failed to index")
        run.details.update(full=full, since=since.isoformat() if since else None, index=index)
    finally:
        if es is None:
            await client.close()


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Index insights from Postgres into Elasticsearch.")
    parser.add_argument("--full", action="store_true", help="rebuild the index from scratch")
    args = parser.parse_args()
    settings = get_settings()
    configure_logging(settings.log_level)
    async with ingest_context(settings) as ctx, track_job_run(ctx.sessions, "index_insights") as run:
        await index_insights(ctx, {"full": args.full}, run)
    print(f"indexed {run.items_out} insights (full={args.full}, errors={run.errors})")


if __name__ == "__main__":
    asyncio.run(_main())
