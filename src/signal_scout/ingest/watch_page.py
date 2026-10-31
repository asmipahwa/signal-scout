"""The `watch_page` job: snapshot a competitor's web page (pricing, plans) and turn real changes into signals.

Each poll fetches the page politely, extracts its main text, and normalizes away cosmetic noise
(dates, cookie banners, copyright lines; see processing/page_diff.py). If the normalized text differs
from the last snapshot, a new `page_snapshots` row is stored, and if the difference is meaningful a
`webpage_change` signal is created whose text is the **diff** (removed and added lines). The LLM then
explains what changed instead of re-reading the whole page. The first snapshot is only a baseline.

Pages that render their content with JavaScript (e.g. Smartsheet, Jira pricing) yield little text
with a plain HTTP fetch, so only the server-rendered parts of those pages are tracked.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import trafilatura
from sqlalchemy import select

from ..db.models import Competitor, PageSnapshot, Signal, SignalStatus, Source, SourceType
from ..jobruns import RunStats
from ..log import get_logger
from ..processing.page_diff import content_hash, diff_pages, normalize
from ..storage import raw_key, sha256_hex, text_key
from .fetch_source import IngestContext, _record_poll, _SourceSnapshot
from .http import FetchError
from .text import excerpt

log = get_logger(__name__)


def page_text(html: str) -> str:
    """Main text of a page, keeping tables (pricing pages are often tables)."""
    return trafilatura.extract(html, include_tables=True, favor_recall=True) or ""


async def watch_page(ctx: IngestContext, payload: dict[str, Any], run: RunStats) -> None:
    """Job handler. Payload: {"source_id": int} for a `webpage_watch` source."""
    source_id = int(payload["source_id"])
    async with ctx.sessions() as session:
        source = await session.get(Source, source_id)
        if source is None or not source.enabled or source.type != SourceType.webpage_watch:
            run.details["skipped"] = "missing, disabled, or not a webpage_watch source"
            return
        competitor = await session.get(Competitor, source.competitor_id) if source.competitor_id else None
        snapshot = _SourceSnapshot(
            id=source.id,
            name=source.name,
            type=source.type,
            url=source.url,
            etag=None,  # always fetch: many pages send ETags that change on every request
            last_modified=None,
            consecutive_failures=source.consecutive_failures,
            competitor_name=competitor.name if competitor else None,
        )
        previous = (
            await session.execute(
                select(PageSnapshot)
                .where(PageSnapshot.source_id == source_id)
                .order_by(PageSnapshot.fetched_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    run.details.update(source_id=source_id, source=snapshot.name)

    try:
        result = await ctx.http.get(snapshot.url)
    except FetchError as exc:
        await _record_poll(ctx, snapshot, succeeded=False, error=str(exc))
        raise
    html = result.body.decode("utf-8", "replace")
    text = page_text(html)
    run.items_in = 1
    run.details.update(http_status=result.status, text_chars=len(text))
    if not normalize(text):
        await _record_poll(ctx, snapshot, succeeded=True)
        run.record_error("no extractable text (page may be rendered with JavaScript)")
        return

    page_hash = content_hash(text)
    if previous is not None and previous.content_hash == page_hash:
        await _record_poll(ctx, snapshot, succeeded=True)
        run.details["changed"] = False
        return

    now = datetime.now(UTC)
    raw = result.body
    key = raw_key(sha256_hex(raw), now)
    await ctx.blobs.put_if_absent(key, raw, "text/html; charset=utf-8")
    await ctx.blobs.put_if_absent(text_key(page_hash), text.encode(), "text/plain; charset=utf-8")

    change = None
    if previous is not None:
        old_text = (await ctx.blobs.get(text_key(previous.content_hash))).decode("utf-8", "replace")
        change = diff_pages(old_text, text)

    async with ctx.sessions() as session:
        session.add(
            PageSnapshot(source_id=source_id, url=snapshot.url, fetched_at=now, content_hash=page_hash, s3_key=key)
        )
        if change is not None:
            diff_text = change.as_signal_text()
            diff_hash = sha256_hex(diff_text.encode())
            await ctx.blobs.put_if_absent(text_key(diff_hash), diff_text.encode(), "text/plain; charset=utf-8")
            name = snapshot.competitor_name or snapshot.name
            session.add(
                Signal(
                    source_id=source_id,
                    url=snapshot.url,
                    # One signal per distinct change: the page URL plus the new content hash.
                    canonical_url=f"{snapshot.url}#snapshot-{page_hash[:16]}",
                    title=f"{name}: change detected on {snapshot.name}",
                    published_at=now,
                    fetched_at=now,
                    content_hash=diff_hash,
                    s3_key=key,
                    excerpt=excerpt(diff_text, ctx.settings.excerpt_chars),
                    matched_competitors=[name] if snapshot.competitor_name else [],
                    status=SignalStatus.new,
                )
            )
            run.items_out = 1
        await session.commit()
    await _record_poll(ctx, snapshot, succeeded=True)
    run.details.update(changed=True, baseline=previous is None, meaningful=change is not None)
    log.info("page_snapshot_stored", source_id=source_id, baseline=previous is None, meaningful=change is not None)
