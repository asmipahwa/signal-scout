"""Competitor-level views: the list, a per-competitor brief, recent changes, and side-by-side comparison.

These are the questions a seller asks before a deal ("what did ClickUp change this month?"), so
they aggregate in Postgres over a time window instead of making the caller page through the feed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Competitor, Enrichment, Insight, InsightSignal, Signal, SignalStatus, Source, SourceType
from .errors import InvalidRequestError, NotFoundError
from .insights import insight_seen_at, insight_summary, insights_with_competitor
from .schemas import (
    ChangeItem,
    Comparison,
    CompetitorBrief,
    CompetitorComparison,
    CompetitorOut,
    InsightSummary,
    RecentChanges,
)

# Categories that count as a "change" a seller must know about, plus watched-page diffs.
CHANGE_CATEGORIES = ("pricing_change", "product_launch")
WEBPAGE_CHANGE = "webpage_change"
CHANGE_TYPES = (*CHANGE_CATEGORIES, WEBPAGE_CHANGE)
HIDDEN_STATUSES = (SignalStatus.duplicate, SignalStatus.filtered_out, SignalStatus.not_relevant)
MAX_DAYS = 365
TOP_INSIGHTS = 5
MAX_CHANGES = 50


def _window_start(days: int) -> datetime:
    if not 1 <= days <= MAX_DAYS:
        raise InvalidRequestError(f"days must be between 1 and {MAX_DAYS}")
    return datetime.now(UTC) - timedelta(days=days)


def _competitor_out(c: Competitor) -> CompetitorOut:
    return CompetitorOut(id=c.id, name=c.name, aliases=list(c.aliases), domains=list(c.domains))


async def list_competitors(session: AsyncSession) -> list[CompetitorOut]:
    """Every tracked competitor, by name."""
    rows = (await session.execute(select(Competitor).order_by(Competitor.name))).scalars()
    return [_competitor_out(c) for c in rows]


async def resolve_competitor(session: AsyncSession, ref: int | str) -> Competitor:
    """Find a competitor by id, or by name/alias case-insensitively. Raises NotFoundError."""
    if isinstance(ref, int):
        found = await session.get(Competitor, ref)
    else:
        needle = ref.strip().lower()
        found = (
            await session.execute(select(Competitor).where(func.lower(Competitor.name) == needle))
        ).scalar_one_or_none()
        if found is None:
            for c in (await session.execute(select(Competitor))).scalars():
                if needle in (a.lower() for a in c.aliases):
                    found = c
                    break
    if found is None:
        raise NotFoundError(f"competitor {ref!r} not found")
    return found


async def _category_counts(session: AsyncSession, competitor_id: int, since: datetime) -> dict[str, int]:
    stmt = (
        select(Insight.category, func.count())
        .where(Insight.competitor_id == competitor_id, insight_seen_at() >= since)
        .group_by(Insight.category)
        .order_by(func.count().desc(), Insight.category)
    )
    return {category: int(n) for category, n in (await session.execute(stmt)).all()}


async def _top_insights(session: AsyncSession, competitor_id: int, since: datetime, limit: int) -> list[InsightSummary]:
    stmt = (
        insights_with_competitor()
        .where(Insight.competitor_id == competitor_id, insight_seen_at() >= since)
        .order_by(Insight.score.desc(), Insight.id.desc())
        .limit(limit)
    )
    return [insight_summary(i, name) for i, name in (await session.execute(stmt)).all()]


async def list_recent_changes(
    session: AsyncSession,
    competitor: int | str | None = None,
    change_type: str | None = None,
    days: int = 7,
    limit: int = MAX_CHANGES,
) -> RecentChanges:
    """Pricing changes, product launches and watched-page changes, newest first.

    `change_type` is one of pricing_change, product_launch, webpage_change (or None for all). For
    watched pages, `diff` is the stored excerpt, which for a page change is the -/+ line diff.
    """
    since = _window_start(days)
    condition: ColumnElement[bool]
    if change_type is None:
        condition = or_(Enrichment.category.in_(CHANGE_CATEGORIES), Source.type == SourceType.webpage_watch)
    elif change_type == WEBPAGE_CHANGE:
        condition = Source.type == SourceType.webpage_watch
    elif change_type in CHANGE_CATEGORIES:
        condition = Enrichment.category == change_type
    else:
        raise InvalidRequestError(f"change_type must be one of {', '.join(CHANGE_TYPES)}")

    first_insight = (
        select(func.min(InsightSignal.insight_id)).where(InsightSignal.signal_id == Signal.id).scalar_subquery()
    )
    when = func.coalesce(Signal.published_at, Signal.fetched_at)
    stmt = (
        select(Signal, Source.type, Source.competitor_id, Enrichment.category, Enrichment.summary, first_insight)
        .join(Source, Source.id == Signal.source_id)
        .outerjoin(Enrichment, Enrichment.signal_id == Signal.id)
        .where(condition, when >= since, Signal.status.not_in(HIDDEN_STATUSES))
        .order_by(when.desc(), Signal.id.desc())
        .limit(max(1, min(limit, MAX_CHANGES)))
    )
    name: str | None = None
    if competitor is not None:
        found = await resolve_competitor(session, competitor)
        name = found.name
        stmt = stmt.where(or_(Signal.matched_competitors.contains([found.name]), Source.competitor_id == found.id))

    owners = {c.id: c.name for c in (await session.execute(select(Competitor))).scalars()}
    items = []
    for signal, source_type, source_competitor_id, category, summary, insight_id in (await session.execute(stmt)).all():
        is_page = source_type is SourceType.webpage_watch
        items.append(
            ChangeItem(
                signal_id=signal.id,
                competitor=name
                or (signal.matched_competitors[0] if signal.matched_competitors else owners.get(source_competitor_id)),
                change_type=category if category in CHANGE_CATEGORIES else WEBPAGE_CHANGE,
                title=signal.title,
                url=signal.url,
                source_type=source_type.value,
                published_at=signal.published_at or signal.fetched_at,
                summary=summary,
                diff=signal.excerpt if is_page else None,
                insight_id=insight_id,
            )
        )
    return RecentChanges(days=days, items=items)


async def competitor_brief(session: AsyncSession, competitor: int | str, days: int = 30) -> CompetitorBrief:
    """Top insights, insight counts by category, and notable pricing/page changes for one competitor."""
    since = _window_start(days)
    found = await resolve_competitor(session, competitor)
    counts = await _category_counts(session, found.id, since)
    changes: list[ChangeItem] = []
    for change_type in ("pricing_change", WEBPAGE_CHANGE):
        changes.extend((await list_recent_changes(session, found.id, change_type, days, limit=10)).items)
    unique = {c.signal_id: c for c in changes}
    notable = sorted(unique.values(), key=lambda c: (c.published_at or since, c.signal_id), reverse=True)
    return CompetitorBrief(
        competitor=_competitor_out(found),
        days=days,
        insight_count=sum(counts.values()),
        counts_by_category=counts,
        top_insights=await _top_insights(session, found.id, since, TOP_INSIGHTS),
        notable_changes=notable[:10],
    )


async def compare_competitors(session: AsyncSession, names: list[str], days: int = 30) -> Comparison:
    """Per competitor: insight counts by category, average importance, and the top insight."""
    since = _window_start(days)
    if not names:
        raise InvalidRequestError("give at least one competitor name")
    if len(names) > 10:
        raise InvalidRequestError("compare at most 10 competitors")
    out: list[CompetitorComparison] = []
    unknown: list[str] = []
    seen: set[int] = set()
    for raw in names:
        try:
            found = await resolve_competitor(session, raw)
        except NotFoundError:
            unknown.append(raw)
            continue
        if found.id in seen:
            continue
        seen.add(found.id)
        avg: Any = (
            await session.execute(
                select(func.avg(Insight.importance)).where(
                    Insight.competitor_id == found.id, insight_seen_at() >= since
                )
            )
        ).scalar_one()
        counts = await _category_counts(session, found.id, since)
        top = await _top_insights(session, found.id, since, 1)
        out.append(
            CompetitorComparison(
                competitor=found.name,
                insight_count=sum(counts.values()),
                counts_by_category=counts,
                avg_importance=round(float(avg), 2) if avg is not None else None,
                top_insight=top[0] if top else None,
            )
        )
    return Comparison(days=days, competitors=out, unknown=unknown)
