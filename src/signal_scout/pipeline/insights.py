"""The `build_insights` job: cluster recent enriched signals into insights and rank them.

Each run reclusters the enriched signals from the last WINDOW_DAYS (cheap: pure Python over a few
thousand rows) and reconciles the result with the stored insights. A cluster keeps the id of the
existing insight that already holds most of its signals, so insight ids are stable across runs and
alerts/webhook deliveries keep pointing at the same insight as new coverage arrives.

Scores are recomputed every run because recency decay changes them as time passes.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, insert, select, update

from ..db.models import Competitor, Enrichment, Insight, InsightSignal, Signal, SignalStatus, Source
from ..ingest.fetch_source import IngestContext
from ..jobruns import RunStats
from ..processing.clustering import ClusterInput, cluster_signals
from ..processing.ranking import RankingConfig, RankInput, rank_score
from .common import SIGNAL_SOURCE_TYPE

WINDOW_DAYS = 60
NOVELTY_WINDOW = timedelta(days=7)
RANKING = RankingConfig()


@dataclass
class _Member:
    signal_id: int
    competitor: str
    published_at: datetime
    title: str
    summary: str
    why_it_matters: str
    category: str
    importance: int
    sentiment: float
    source_type: str


def _lead(members: list[_Member]) -> _Member:
    """The signal that speaks for the insight: most important, then most official, then newest."""
    return max(
        members,
        key=lambda m: (
            m.importance,
            RANKING.source_weight.get(m.source_type, RANKING.default_source_weight),
            m.published_at,
        ),
    )


async def build_insights(ctx: IngestContext, payload: dict[str, Any], run: RunStats) -> None:
    now = datetime.now(UTC)
    since = now - timedelta(days=int(payload.get("window_days", WINDOW_DAYS)))
    published = func.coalesce(Signal.published_at, Signal.fetched_at)
    async with ctx.sessions() as session:
        rows = (
            await session.execute(
                select(Signal.id, published, Signal.title, Source.type, Enrichment)
                .join(Enrichment, Enrichment.signal_id == Signal.id)
                .join(Source, Source.id == Signal.source_id)
                .where(
                    Signal.status == SignalStatus.enriched,
                    Enrichment.importance >= RANKING.min_importance,
                    published >= since,
                )
            )
        ).all()
        competitor_ids = dict((await session.execute(select(Competitor.name, Competitor.id))).all())
        existing_links = dict(
            (
                await session.execute(
                    select(InsightSignal.signal_id, InsightSignal.insight_id).where(
                        InsightSignal.signal_id.in_([r[0] for r in rows])
                    )
                )
            ).all()
        )
        window_insight_ids = set(
            (await session.execute(select(Insight.id).where(Insight.last_seen >= since))).scalars()
        )

    members = {
        sid: _Member(
            signal_id=sid,
            competitor=e.competitors[0],
            published_at=pub,
            title=title,
            summary=e.summary,
            why_it_matters=e.why_it_matters,
            category=e.category,
            importance=e.importance,
            sentiment=e.sentiment,
            source_type=SIGNAL_SOURCE_TYPE[stype],
        )
        for sid, pub, title, stype, e in rows
        if e.competitors
    }
    run.items_in = len(members)
    clusters = cluster_signals(
        [ClusterInput(str(m.signal_id), m.competitor, m.published_at, m.title, m.summary) for m in members.values()]
    )

    # Reconcile clusters with stored insights; each existing id is reused by at most one cluster.
    claimed: set[int] = set()
    plan: list[tuple[int | None, list[_Member]]] = []
    for cluster in sorted(clusters, key=lambda c: -len(c.members)):
        group = [members[int(m.id)] for m in cluster.members]
        votes = Counter(existing_links[m.signal_id] for m in group if m.signal_id in existing_links)
        reuse = next((iid for iid, _ in votes.most_common() if iid not in claimed), None)
        if reuse is not None:
            claimed.add(reuse)
        plan.append((reuse, group))

    # Novelty: an insight repeats a theme if the same competitor had a same-category insight in the week before it.
    seen_at: dict[tuple[str, str], list[datetime]] = {}
    for _, group in plan:
        lead = _lead(group)
        seen_at.setdefault((lead.competitor, lead.category), []).append(max(m.published_at for m in group))

    created = updated = 0
    async with ctx.sessions() as session:
        for insight_id, group in plan:
            lead = _lead(group)
            first_seen = min(m.published_at for m in group)
            last_seen = max(m.published_at for m in group)
            importance = max(m.importance for m in group)
            source_types = sorted({m.source_type for m in group})
            repeats = any(
                last_seen - NOVELTY_WINDOW <= t < last_seen for t in seen_at[(lead.competitor, lead.category)]
            )
            score = rank_score(
                RankInput(importance, last_seen, frozenset(source_types), len(group), repeats), now, RANKING
            )
            values = {
                "competitor_id": competitor_ids.get(lead.competitor),
                "title": lead.title,
                "summary": lead.summary,
                "why_it_matters": lead.why_it_matters,
                "category": lead.category,
                "importance": importance,
                "sentiment": sum(m.sentiment for m in group) / len(group),
                "score": score,
                "signal_count": len(group),
                "source_types": source_types,
                "first_seen": first_seen,
                "last_seen": last_seen,
                "updated_at": now,
            }
            if insight_id is None:
                insight = Insight(**values)
                session.add(insight)
                await session.flush()
                insight_id = insight.id
                created += 1
            else:
                await session.execute(update(Insight).where(Insight.id == insight_id).values(**values))
                updated += 1
            signal_ids = [m.signal_id for m in group]
            await session.execute(delete(InsightSignal).where(InsightSignal.signal_id.in_(signal_ids)))
            await session.execute(
                insert(InsightSignal), [{"insight_id": insight_id, "signal_id": sid} for sid in signal_ids]
            )
            claimed.add(insight_id)

        # Insights in the window that no cluster claimed have lost all their signals to other clusters.
        orphaned = window_insight_ids - claimed
        if orphaned:
            await session.execute(delete(Insight).where(Insight.id.in_(orphaned)))
        await session.commit()

    run.items_out = len(plan)
    run.details.update(created=created, updated=updated, removed=len(orphaned), window_days=(now - since).days)
