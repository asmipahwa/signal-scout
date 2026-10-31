"""The `fetch_source` job: poll one source and turn its new items into signal rows.

Steps: conditional GET -> parse -> skip items already stored for this source -> extract text ->
upload raw + text to S3 (content-addressed, skipped if present) -> insert signals with exact
dedupe -> update the source's caching headers and circuit-breaker state.

A failed poll (network, HTTP status, unparseable body) counts toward the circuit breaker and is
re-raised so the queue retries it. A storage failure is ours, not the source's, so it is re-raised
without counting against the source.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from ..config import Settings
from ..db.models import Competitor, Signal, SignalStatus, Source, SourceType
from ..db.session import SessionFactory
from ..jobruns import RunStats
from ..log import get_logger
from ..storage import BlobStore, raw_key, sha256_hex, text_key
from .canonical import canonical_url
from .circuit import after_poll
from .feeds import FeedItem, FeedParseError, parse_items
from .http import FetchError, PoliteHttpClient
from .matching import CompetitorMatcher
from .text import content_hash, excerpt, extract_text, normalize_text

log = get_logger(__name__)

# Items from a competitor's own blog/changelog are about that competitor by definition. Search-style
# sources (Bing News, HN) also carry a competitor_id, but their results must still mention an alias.
OFFICIAL_SOURCE_TYPES = frozenset({SourceType.blog_rss, SourceType.changelog_rss, SourceType.press_release_rss})


@dataclass
class IngestContext:
    """Shared resources a worker hands to ingestion jobs."""

    settings: Settings
    sessions: SessionFactory
    blobs: BlobStore
    http: PoliteHttpClient


@dataclass(frozen=True)
class _SourceSnapshot:
    id: int
    name: str
    type: SourceType
    url: str
    etag: str | None
    last_modified: str | None
    consecutive_failures: int
    competitor_name: str | None


async def fetch_source(ctx: IngestContext, payload: dict[str, Any], run: RunStats) -> None:
    """Job handler. Payload: {"source_id": int}."""
    source_id = int(payload["source_id"])
    async with ctx.sessions() as session:
        source = await session.get(Source, source_id)
        if source is None or not source.enabled:
            run.details["skipped"] = "missing" if source is None else "disabled"
            log.info("source_skipped", source_id=source_id, reason=run.details["skipped"])
            return
        competitor = await session.get(Competitor, source.competitor_id) if source.competitor_id else None
        snapshot = _SourceSnapshot(
            id=source.id,
            name=source.name,
            type=source.type,
            url=source.url,
            etag=source.etag,
            last_modified=source.last_modified,
            consecutive_failures=source.consecutive_failures,
            competitor_name=competitor.name if competitor else None,
        )
        aliases = {c.name: c.aliases for c in (await session.execute(select(Competitor))).scalars()}
    run.details.update(source_id=snapshot.id, source=snapshot.name)

    try:
        result = await ctx.http.get(snapshot.url, etag=snapshot.etag, last_modified=snapshot.last_modified)
        items = [] if result.not_modified else parse_items(snapshot.type, result.body)
    except (FetchError, FeedParseError) as exc:
        await _record_poll(ctx, snapshot, succeeded=False, error=str(exc))
        raise

    run.items_in = len(items)
    run.details["http_status"] = result.status
    counts = await store_items(ctx, snapshot, items, CompetitorMatcher(aliases), run)
    run.details["statuses"] = dict(counts)
    await _record_poll(
        ctx,
        snapshot,
        succeeded=True,
        etag=result.etag or snapshot.etag,
        last_modified=result.last_modified or snapshot.last_modified,
    )


async def store_items(
    ctx: IngestContext,
    source: _SourceSnapshot,
    items: Sequence[FeedItem],
    matcher: CompetitorMatcher,
    run: RunStats,
) -> Counter[str]:
    """Persist the items not yet stored for this source. Returns signal counts by status."""
    counts: Counter[str] = Counter()
    by_url: dict[str, FeedItem] = {}
    for item in items:
        if not item.url:
            continue
        by_url.setdefault(canonical_url(item.url), item)  # first occurrence wins within a batch
    if not by_url:
        return counts

    async with ctx.sessions() as session:
        existing_urls = set(
            (
                await session.execute(
                    select(Signal.canonical_url).where(
                        Signal.source_id == source.id, Signal.canonical_url.in_(list(by_url))
                    )
                )
            ).scalars()
        )
    fresh = {url: item for url, item in by_url.items() if url not in existing_urls}
    counts["already_stored"] = len(by_url) - len(fresh)
    if not fresh:
        return counts

    fetched_at = datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    for url, item in fresh.items():
        title = normalize_text(item.title) or url
        text = extract_text(item.raw_html) or title
        chash = content_hash(title, text)
        raw_bytes = (item.raw_html or title).encode()
        key = raw_key(sha256_hex(raw_bytes), fetched_at)
        await ctx.blobs.put_if_absent(key, raw_bytes, "text/html; charset=utf-8")
        await ctx.blobs.put_if_absent(text_key(chash), text.encode(), "text/plain; charset=utf-8")
        matched = set(matcher.match(title, text))
        if source.competitor_name and source.type in OFFICIAL_SOURCE_TYPES:
            matched.add(source.competitor_name)
        rows.append(
            {
                "source_id": source.id,
                "url": item.url,
                "canonical_url": url,
                "title": title,
                "published_at": item.published_at,
                "fetched_at": fetched_at,
                "content_hash": chash,
                "s3_key": key,
                "excerpt": excerpt(text, ctx.settings.excerpt_chars),
                "matched_competitors": sorted(matched),
            }
        )

    async with ctx.sessions() as session:
        hashes = [r["content_hash"] for r in rows]
        known_hashes = set(
            (await session.execute(select(Signal.content_hash).where(Signal.content_hash.in_(hashes)))).scalars()
        )
        for row in rows:
            if row["content_hash"] in known_hashes:
                row["status"] = SignalStatus.duplicate
            elif not row["matched_competitors"]:
                row["status"] = SignalStatus.filtered_out
            else:
                row["status"] = SignalStatus.new
            known_hashes.add(row["content_hash"])
        stmt = (
            insert(Signal)
            .values(rows)
            .on_conflict_do_nothing(index_elements=[Signal.source_id, Signal.canonical_url])
            .returning(Signal.status)
        )
        inserted = (await session.execute(stmt)).scalars().all()
        await session.commit()
    for status in inserted:
        counts[status.value] += 1
    run.items_out = len(inserted)
    return counts


async def _record_poll(
    ctx: IngestContext,
    source: _SourceSnapshot,
    *,
    succeeded: bool,
    error: str | None = None,
    etag: str | None = None,
    last_modified: str | None = None,
) -> None:
    decision = after_poll(
        source.consecutive_failures,
        succeeded=succeeded,
        threshold=ctx.settings.circuit_breaker_threshold,
        error=error,
    )
    values: dict[str, Any] = {
        "last_polled_at": datetime.now(UTC),
        "consecutive_failures": decision.consecutive_failures,
    }
    if succeeded:
        values.update(etag=etag, last_modified=last_modified)
    if decision.tripped:
        values.update(enabled=False, disabled_reason=decision.disabled_reason)
        log.warning(
            "source_disabled_by_circuit_breaker",
            source_id=source.id,
            source=source.name,
            consecutive_failures=decision.consecutive_failures,
            error=error,
        )
    async with ctx.sessions() as session:
        await session.execute(update(Source).where(Source.id == source.id).values(**values))
        await session.commit()
