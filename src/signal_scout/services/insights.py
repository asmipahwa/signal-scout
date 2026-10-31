"""Insight reads: full-text search (Elasticsearch), the ranked feed and insight detail (Postgres).

Search goes to Elasticsearch because it needs BM25 + highlighting; everything that must be exact
and current (the feed, detail, citations) reads Postgres, the source of truth.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from elasticsearch import AsyncElasticsearch
from elasticsearch import NotFoundError as EsNotFoundError
from elasticsearch import TransportError
from elasticsearch.exceptions import ApiError
from sqlalchemy import Select, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Competitor, Insight, InsightSignal, Signal, Source
from ..log import get_logger
from ..metrics import SEARCH_REQUEST_SECONDS
from ..search.mapping import versioned_index_name
from ..search.query import MAX_PAGE_SIZE, SearchParams, build_search
from .cursor import FeedCursor, decode_cursor, encode_cursor
from .errors import DependencyError, NotFoundError
from .schemas import InsightDetail, InsightPage, InsightSources, InsightSummary, SearchHit, SearchResult, SignalRef

log = get_logger(__name__)

MAX_FEED_LIMIT = 100


@dataclass(frozen=True)
class InsightFilters:
    """Filters for the ranked feed. `competitor` is a name, matched case-insensitively."""

    competitor: str | None = None
    category: str | None = None
    min_importance: int | None = None
    since: datetime | None = None


def insight_summary(insight: Insight, competitor: str | None) -> InsightSummary:
    return InsightSummary(
        id=insight.id,
        competitor=competitor,
        title=insight.title,
        summary=insight.summary,
        why_it_matters=insight.why_it_matters,
        category=insight.category,
        importance=insight.importance,
        score=insight.score,
        sentiment=insight.sentiment,
        signal_count=insight.signal_count,
        source_types=list(insight.source_types),
        first_seen=insight.first_seen,
        last_seen=insight.last_seen,
    )


def insights_with_competitor() -> Select[tuple[Insight, str | None]]:
    """SELECT insight, competitor name: the shape every insight listing starts from."""
    return select(Insight, Competitor.name).outerjoin(Competitor, Competitor.id == Insight.competitor_id)


def insight_seen_at() -> Any:
    """When an insight was last active; older rows written before migration 2 may lack last_seen."""
    return func.coalesce(Insight.last_seen, Insight.created_at)


async def search_insights(es: AsyncElasticsearch, params: SearchParams, index: str | None = None) -> SearchResult:
    """Full-text search over the insight index, using the shared query builder in `search.query`."""
    body = build_search(params)
    started = time.perf_counter()
    try:
        response = await es.search(index=index or versioned_index_name(), body=body)
    except EsNotFoundError:
        # No index yet (fresh install, nothing indexed): an empty result, not an error.
        return SearchResult(total=0, took_ms=0, page=max(1, params.page), size=body["size"], hits=[])
    except (ApiError, TransportError) as exc:
        log.warning("search_failed", error=f"{type(exc).__name__}: {exc}")
        raise DependencyError("search backend unavailable") from exc
    finally:
        SEARCH_REQUEST_SECONDS.observe(time.perf_counter() - started)

    hits = []
    for hit in response["hits"]["hits"]:
        src: dict[str, Any] = hit.get("_source", {})
        hits.append(
            SearchHit(
                insight_id=str(src.get("insight_id", hit["_id"])),
                score=hit.get("_score"),
                headline=src.get("headline", ""),
                summary=src.get("summary", ""),
                why_it_matters=src.get("why_it_matters", ""),
                competitor=src.get("competitor"),
                category=src.get("category"),
                importance=src.get("importance"),
                rank_score=src.get("rank_score"),
                signal_count=src.get("signal_count"),
                source_types=list(src.get("source_types") or []),
                source_urls=list(src.get("source_urls") or []),
                first_seen=src.get("first_seen"),
                last_seen=src.get("last_seen"),
                highlights=dict(hit.get("highlight", {})),
            )
        )
    total = response["hits"]["total"]
    return SearchResult(
        total=int(total["value"] if isinstance(total, dict) else total),
        took_ms=int(response.get("took", 0)),
        page=max(1, params.page),
        size=max(1, min(params.size, MAX_PAGE_SIZE)),
        hits=hits,
    )


async def list_insights(
    session: AsyncSession, filters: InsightFilters, cursor: str | None = None, limit: int = 20
) -> InsightPage:
    """The ranked feed: score DESC, id DESC, keyset-paginated with an opaque cursor."""
    limit = max(1, min(limit, MAX_FEED_LIMIT))
    stmt = insights_with_competitor()
    if filters.competitor:
        stmt = stmt.where(func.lower(Competitor.name) == filters.competitor.lower())
    if filters.category:
        stmt = stmt.where(Insight.category == filters.category)
    if filters.min_importance:
        stmt = stmt.where(Insight.importance >= filters.min_importance)
    if filters.since:
        stmt = stmt.where(insight_seen_at() >= filters.since)
    if cursor:
        position = decode_cursor(cursor)
        stmt = stmt.where(tuple_(Insight.score, Insight.id) < tuple_(position.score, position.id))
    stmt = stmt.order_by(Insight.score.desc(), Insight.id.desc()).limit(limit + 1)

    rows = (await session.execute(stmt)).all()
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1][0]
        next_cursor = encode_cursor(FeedCursor(score=last.score, id=last.id))
    return InsightPage(items=[insight_summary(i, name) for i, name in page], next_cursor=next_cursor)


async def _signal_refs(session: AsyncSession, insight_id: int) -> list[SignalRef]:
    stmt = (
        select(Signal, Source.type, Source.name)
        .join(InsightSignal, InsightSignal.signal_id == Signal.id)
        .join(Source, Source.id == Signal.source_id)
        .where(InsightSignal.insight_id == insight_id)
        .order_by(Signal.published_at.desc().nulls_last(), Signal.id)
    )
    return [
        SignalRef(
            id=signal.id,
            title=signal.title,
            url=signal.url,
            source_type=source_type.value,
            source_name=source_name,
            published_at=signal.published_at,
        )
        for signal, source_type, source_name in (await session.execute(stmt)).all()
    ]


async def _insight_row(session: AsyncSession, insight_id: int) -> tuple[Insight, str | None]:
    row = (await session.execute(insights_with_competitor().where(Insight.id == insight_id))).first()
    if row is None:
        raise NotFoundError(f"insight {insight_id} not found")
    return row[0], row[1]


async def get_insight(session: AsyncSession, insight_id: int) -> InsightDetail:
    """One insight with its linked signals."""
    insight, competitor = await _insight_row(session, insight_id)
    summary = insight_summary(insight, competitor)
    return InsightDetail(**summary.model_dump(), signals=await _signal_refs(session, insight_id))


async def get_signal_sources(session: AsyncSession, insight_id: int) -> InsightSources:
    """The underlying articles for an insight, with links, so an agent can cite them."""
    insight, competitor = await _insight_row(session, insight_id)
    return InsightSources(
        insight_id=insight.id,
        title=insight.title,
        competitor=competitor,
        sources=await _signal_refs(session, insight_id),
    )
