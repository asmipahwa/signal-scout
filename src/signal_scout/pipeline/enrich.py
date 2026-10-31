"""The `enrich_signals` job: cheap filters first, then the LLM, for a batch of new signals.

Per signal, in order of cost:
1. Low-value title patterns (reviews, comparisons, explainers...) -> `filtered_out`.
2. Near-duplicate of a recently processed signal (MinHash LSH) -> `duplicate`.
3. CostGuard (paid spend caps) and the provider's daily quota -> `deferred` and stop the batch.
4. LLM enrichment -> `enriched` or `not_relevant`; a bad response twice -> `enrichment_failed`;
   a provider outage -> `deferred` (retried by a later run).

Every LLM call is written to `llm_calls` with its tokens and cost, and the run's totals go to `job_runs`.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, case, func, select, update

from ..db.models import Enrichment, LlmCall, Signal, SignalStatus, Source, SourceType
from ..enrichment import (
    LLMClient,
    LlmError,
    LlmQuotaExhaustedError,
    LlmTransientError,
    SignalInput,
)
from ..ingest.fetch_source import IngestContext
from ..jobruns import RunStats
from ..log import get_logger
from ..processing.near_dup import NearDuplicateIndex
from ..processing.relevance import decide
from ..storage import text_key
from .common import OFFICIAL_SOURCE_TYPES, SIGNAL_SOURCE_TYPE, competitor_names, guess_language, llm_client_for
from .costguard import check_budget

log = get_logger(__name__)

DEFAULT_BATCH = 20
NEAR_DUP_LOOKBACK = timedelta(days=30)
MAX_LLM_TEXT_CHARS = 6000


# The daily LLM quota is the bottleneck, so spend it where it matters: fresh signals before the
# historical backfill, and official sources (page changes, press releases, changelogs) before news,
# before forum posts.
FRESH = timedelta(days=7)
SOURCE_PRIORITY = {
    SourceType.webpage_watch: 0,
    SourceType.press_release_rss: 1,
    SourceType.changelog_rss: 2,
    SourceType.inbound_webhook: 2,
    SourceType.blog_rss: 3,
    SourceType.news_rss: 4,
    SourceType.hn_search: 5,
}


def _priority() -> list[ColumnElement[Any]]:
    published = func.coalesce(Signal.published_at, Signal.fetched_at)
    fresh_first = case((published >= func.now() - FRESH, 0), else_=1)
    by_source = case(*((Source.type == t, p) for t, p in SOURCE_PRIORITY.items()), else_=9)
    return [fresh_first, by_source, published.desc()]


async def _near_dup_index(ctx: IngestContext) -> NearDuplicateIndex:
    """Index the recent signals that already went through the LLM, so copies of them are skipped."""
    since = datetime.now(UTC) - NEAR_DUP_LOOKBACK
    async with ctx.sessions() as session:
        rows = (
            await session.execute(
                select(Signal.id, Signal.title, Signal.excerpt).where(
                    Signal.status.in_([SignalStatus.enriched, SignalStatus.not_relevant]),
                    Signal.fetched_at >= since,
                )
            )
        ).all()
    index = NearDuplicateIndex()
    for signal_id, title, excerpt in rows:
        index.add(str(signal_id), f"{title} {excerpt}")
    return index


async def _signal_text(ctx: IngestContext, signal: Signal) -> str:
    """Full extracted text from S3; fall back to the stored excerpt if the object is missing."""
    try:
        return (await ctx.blobs.get(text_key(signal.content_hash))).decode("utf-8", "replace")[:MAX_LLM_TEXT_CHARS]
    except Exception as exc:  # missing object or S3 hiccup: the excerpt is still useful
        log.warning("signal_text_unavailable", signal_id=signal.id, error=type(exc).__name__)
        return signal.excerpt


async def _set_status(ctx: IngestContext, signal_id: int, **values: Any) -> None:
    async with ctx.sessions() as session:
        await session.execute(update(Signal).where(Signal.id == signal_id).values(**values))
        await session.commit()


async def enrich_signals(
    ctx: IngestContext, payload: dict[str, Any], run: RunStats, client: LLMClient | None = None
) -> None:
    batch = int(payload.get("batch", DEFAULT_BATCH))
    async with ctx.sessions() as session:
        competitors = await competitor_names(session)
        rows = (
            await session.execute(
                select(Signal, Source.type)
                .join(Source, Source.id == Signal.source_id)
                .where(Signal.status.in_([SignalStatus.new, SignalStatus.deferred]))
                .order_by(*_priority(), Signal.id)
                .limit(batch)
            )
        ).all()
    if not rows:
        return

    llm: LLMClient = client or llm_client_for(ctx.settings, competitors)
    near_dups = await _near_dup_index(ctx)
    outcomes: Counter[str] = Counter()
    categories: Counter[str] = Counter()
    tokens: Counter[str] = Counter()
    stop_reason: str | None = None

    for signal, source_type in rows:
        run.items_in += 1
        if stop_reason:
            await _set_status(ctx, signal.id, status=SignalStatus.deferred)
            outcomes["deferred"] += 1
            continue

        decision = decide(
            signal.title, signal.matched_competitors, official_source=source_type in OFFICIAL_SOURCE_TYPES
        )
        if not decision.keep:
            await _set_status(ctx, signal.id, status=SignalStatus.filtered_out, filter_reason=decision.reason)
            outcomes[decision.reason.split(":")[0]] += 1
            continue
        duplicate_of = near_dups.add(str(signal.id), f"{signal.title} {signal.excerpt}")
        if duplicate_of is not None:
            await _set_status(
                ctx,
                signal.id,
                status=SignalStatus.duplicate,
                duplicate_of_id=int(duplicate_of),
                filter_reason="near_duplicate",
            )
            outcomes["near_duplicate"] += 1
            continue

        text = await _signal_text(ctx, signal)
        try:
            async with ctx.sessions() as session:
                await check_budget(session, ctx.settings.cost_daily_cap_usd, ctx.settings.cost_total_cap_usd)
            result = await llm.enrich(
                SignalInput(
                    source_type=SIGNAL_SOURCE_TYPE[source_type],
                    source=str(source_type.value),
                    title=signal.title,
                    url=signal.url,
                    published_at=signal.published_at or signal.fetched_at,
                    text=text,
                    matched_competitors=signal.matched_competitors,
                )
            )
        except LlmQuotaExhaustedError as exc:  # daily quota or budget: stop and let a later run continue
            stop_reason = str(exc)
            await _set_status(ctx, signal.id, status=SignalStatus.deferred)
            outcomes["deferred"] += 1
            continue
        except LlmTransientError as exc:
            run.record_error(f"signal {signal.id}: {exc}")
            await _set_status(ctx, signal.id, status=SignalStatus.deferred)
            outcomes["deferred"] += 1
            continue
        except LlmError as exc:
            run.record_error(f"signal {signal.id}: {exc}")
            await _set_status(ctx, signal.id, status=SignalStatus.enrichment_failed)
            outcomes["enrichment_failed"] += 1
            continue

        e, u = result.enrichment, result.usage
        relevant = e.is_about_tracked_competitor and bool(e.competitors)
        async with ctx.sessions() as session:
            session.add(
                Enrichment(
                    signal_id=signal.id,
                    is_about_tracked_competitor=e.is_about_tracked_competitor,
                    category=e.category,
                    summary=e.summary,
                    why_it_matters=e.why_it_matters,
                    importance=e.importance,
                    sentiment=e.sentiment,
                    confidence=e.confidence,
                    competitors=e.competitors,
                    provider=u.provider,
                    model=u.model,
                    prompt_version=result.prompt_version,
                    input_tokens=u.input_tokens,
                    output_tokens=u.output_tokens,
                    cache_read_tokens=u.cache_read_tokens,
                    cache_write_tokens=u.cache_write_tokens,
                    thinking_tokens=u.thinking_tokens,
                )
            )
            session.add(
                LlmCall(
                    provider=u.provider,
                    model=u.model,
                    signal_id=signal.id,
                    run_id=run.run_id,
                    input_tokens=u.input_tokens,
                    output_tokens=u.output_tokens,
                    cache_read_tokens=u.cache_read_tokens,
                    cache_write_tokens=u.cache_write_tokens,
                    thinking_tokens=u.thinking_tokens,
                    cost_usd=Decimal(str(round(u.cost_usd, 6))),
                )
            )
            await session.execute(
                update(Signal)
                .where(Signal.id == signal.id)
                .values(
                    status=SignalStatus.enriched if relevant else SignalStatus.not_relevant,
                    language=guess_language(f"{signal.title} {text}"),
                )
            )
            await session.commit()
        run.items_out += 1
        run.tokens += u.input_tokens + u.output_tokens + u.cache_read_tokens + u.cache_write_tokens + u.thinking_tokens
        run.cost_usd += Decimal(str(round(u.cost_usd, 6)))
        outcomes["enriched" if relevant else "not_relevant"] += 1
        if relevant:
            categories[e.category] += 1
        tokens.update(
            input=u.input_tokens, output=u.output_tokens, cache_read=u.cache_read_tokens,
            cache_write=u.cache_write_tokens, thinking=u.thinking_tokens,
        )  # fmt: skip

    run.details.update(
        outcomes=dict(outcomes), provider=llm.provider, model=llm.model, prompt_version=llm.prompt_version
    )
    if stop_reason:
        run.details["stopped"] = stop_reason
        log.warning("enrichment_stopped", reason=stop_reason)
