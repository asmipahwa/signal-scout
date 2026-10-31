"""Seed data for the REST API and MCP integration tests.

EVERYTHING HERE IS INVENTED TEST FIXTURE DATA. The competitor names match the tracked set so the
data looks like production, but every headline, price, date and URL below is made up for these
tests (URLs point at example.com). None of it describes a real event.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from elasticsearch import AsyncElasticsearch
from elasticsearch.helpers import async_bulk
from sqlalchemy import select

from signal_scout.db.models import (
    Competitor,
    DeliveryStatus,
    Enrichment,
    Insight,
    InsightSignal,
    Signal,
    SignalStatus,
    Source,
    SourceType,
    WebhookDelivery,
    WebhookSubscription,
)
from signal_scout.db.session import SessionFactory
from signal_scout.jobruns import track_job_run
from signal_scout.search.mapping import INDEX_NAME, index_body, versioned_index_name
from signal_scout.storage import S3BlobStore, raw_key, sha256_hex

NOW = datetime.now(UTC)
PAGE_DIFF = "- Plus: $10 per seat/month\n+ Plus: $12 per seat/month"  # fixture diff, not a real price


@dataclass
class Seeded:
    """Ids of the seeded rows the tests assert on."""

    notion_id: int
    asana_id: int
    pricing_insight: int
    launch_insight: int
    outage_insight: int
    old_insight: int
    pricing_signal: int
    page_signal: int
    hidden_signal: int
    subscription_id: int
    raw_body: bytes


def _signal(source: Source, slug: str, title: str, days_ago: float, excerpt: str, competitors: list[str]) -> Signal:
    return Signal(
        source_id=source.id,
        url=f"https://example.com/fixture/{slug}",
        canonical_url=f"https://example.com/fixture/{slug}",
        title=title,
        published_at=NOW - timedelta(days=days_ago),
        content_hash=sha256_hex(slug.encode()),
        s3_key=raw_key(sha256_hex(slug.encode()), NOW),
        excerpt=excerpt,
        matched_competitors=competitors,
        status=SignalStatus.enriched,
    )


def _enrichment(signal: Signal, category: str, importance: int, summary: str, competitor: str) -> Enrichment:
    return Enrichment(
        signal_id=signal.id,
        is_about_tracked_competitor=True,
        category=category,
        summary=summary,
        why_it_matters="Fixture: reps should expect this to come up in deals.",
        importance=importance,
        sentiment=0.0,
        confidence=0.9,
        competitors=[competitor],
        provider="fake",
        model="fake",
        prompt_version="test",
    )


async def seed(sessions: SessionFactory, blobs: S3BlobStore) -> Seeded:
    async with sessions() as s:
        notion = Competitor(name="Notion", aliases=["Notion", "Notion AI"], domains=["notion.com"])
        asana = Competitor(name="Asana", aliases=["Asana"], domains=["asana.com"])
        clickup = Competitor(name="ClickUp", aliases=["ClickUp"], domains=["clickup.com"])
        s.add_all([notion, asana, clickup])
        await s.flush()
        releases = Source(
            name="Fixture: Notion releases",
            type=SourceType.changelog_rss,
            url="https://example.com/fixture/notion/rss",
            competitor_id=notion.id,
        )
        page = Source(
            name="Fixture: Notion pricing page",
            type=SourceType.webpage_watch,
            url="https://example.com/fixture/notion/pricing",
            competitor_id=notion.id,
        )
        news = Source(name="Fixture: Asana news", type=SourceType.news_rss, url="https://example.com/fixture/asana/rss")
        s.add_all([releases, page, news])
        await s.flush()

        pricing = _signal(
            releases, "notion-plus-price", "Notion raises the Plus plan price", 2, "Plus goes up.", ["Notion"]
        )
        page_change = _signal(page, "notion-pricing-page", "Notion pricing page changed", 1, PAGE_DIFF, ["Notion"])
        launch = _signal(releases, "notion-agents", "Notion launches background agents", 3, "Agents.", ["Notion"])
        outage = _signal(news, "asana-outage", "Asana outage leaves users unable to log in", 5, "Down.", ["Asana"])
        funding = _signal(news, "asana-old", "Asana announces a partner program", 60, "Old news.", ["Asana"])
        hidden = _signal(releases, "notion-dup", "Notion raises the Plus plan price (copy)", 2, "Dup.", ["Notion"])
        hidden.status = SignalStatus.duplicate
        s.add_all([pricing, page_change, launch, outage, funding, hidden])
        await s.flush()
        s.add_all(
            [
                _enrichment(pricing, "pricing_change", 5, "Notion's Plus plan price rises per seat.", "Notion"),
                _enrichment(page_change, "pricing_change", 5, "The pricing page shows a higher Plus price.", "Notion"),
                _enrichment(launch, "product_launch", 4, "Notion ships agents that run in the background.", "Notion"),
                _enrichment(outage, "outage_or_incident", 4, "Asana had a multi-hour login outage.", "Asana"),
                _enrichment(funding, "partnership", 2, "Asana starts a partner program.", "Asana"),
                _enrichment(hidden, "pricing_change", 5, "Copy of the pricing story.", "Notion"),
            ]
        )

        def insight(comp: Competitor, title: str, category: str, importance: int, score: float, days: float) -> Insight:
            return Insight(
                competitor_id=comp.id,
                title=title,
                summary=f"Fixture summary: {title}.",
                why_it_matters="Fixture: changes the price comparison in competitive deals.",
                category=category,
                importance=importance,
                score=score,
                sentiment=-0.2,
                signal_count=1,
                source_types=["changelog"],
                first_seen=NOW - timedelta(days=days),
                last_seen=NOW - timedelta(days=days),
            )

        i_pricing = insight(notion, "Notion raises Plus plan pricing", "pricing_change", 5, 0.9, 1)
        i_pricing.signal_count, i_pricing.source_types = 2, ["changelog", "webpage_change"]
        i_launch = insight(notion, "Notion launches background agents", "product_launch", 4, 0.7, 3)
        i_outage = insight(asana, "Asana outage blocks sign-in", "outage_or_incident", 4, 0.6, 5)
        i_old = insight(asana, "Asana partner program", "partnership", 2, 0.1, 60)
        s.add_all([i_pricing, i_launch, i_outage, i_old])
        await s.flush()
        s.add_all(
            [
                InsightSignal(insight_id=i_pricing.id, signal_id=pricing.id),
                InsightSignal(insight_id=i_pricing.id, signal_id=page_change.id),
                InsightSignal(insight_id=i_launch.id, signal_id=launch.id),
                InsightSignal(insight_id=i_outage.id, signal_id=outage.id),
                InsightSignal(insight_id=i_old.id, signal_id=funding.id),
            ]
        )
        sub = WebhookSubscription(name="Fixture hook", url="https://example.com/fixture/hook", secret="fixture-secret")
        s.add(sub)
        await s.flush()
        s.add(
            WebhookDelivery(
                insight_id=i_pricing.id,
                subscription_id=sub.id,
                status=DeliveryStatus.delivered,
                attempts=1,
                last_status_code=200,
                delivered_at=NOW,
            )
        )
        await s.commit()
        seeded = Seeded(
            notion_id=notion.id,
            asana_id=asana.id,
            pricing_insight=i_pricing.id,
            launch_insight=i_launch.id,
            outage_insight=i_outage.id,
            old_insight=i_old.id,
            pricing_signal=pricing.id,
            page_signal=page_change.id,
            hidden_signal=hidden.id,
            subscription_id=sub.id,
            raw_body=b"<html><body>Fixture raw body for the Notion pricing story.</body></html>",
        )
        pricing_key = pricing.s3_key
    await blobs.put_if_absent(pricing_key, seeded.raw_body, "text/html; charset=utf-8")
    async with track_job_run(sessions, "index_insights") as run:  # a job_runs row for /runs
        run.items_out = 4
    return seeded


async def index_insights(es: AsyncElasticsearch, sessions: SessionFactory) -> None:
    """Build the versioned index from search/mapping.py, alias it, and index every seeded insight."""
    index = versioned_index_name()
    await es.indices.delete(index=index, ignore_unavailable=True)
    await es.indices.create(index=index, **index_body())
    await es.indices.put_alias(index=index, name=INDEX_NAME)
    async with sessions() as s:
        rows = (
            await s.execute(select(Insight, Competitor.name).join(Competitor, Competitor.id == Insight.competitor_id))
        ).all()
    docs: list[dict[str, Any]] = [
        {
            "_index": index,
            "_id": str(i.id),
            "_source": {
                "insight_id": str(i.id),
                "headline": i.title,
                "summary": i.summary,
                "why_it_matters": i.why_it_matters,
                "signals_text": i.title,
                "competitor": name,
                "competitor_suggest": {"input": [name]},
                "category": i.category,
                "importance": i.importance,
                "sentiment": i.sentiment,
                "rank_score": i.score,
                "signal_count": i.signal_count,
                "source_types": i.source_types,
                "source_urls": [f"https://example.com/fixture/insight-{i.id}"],
                "first_seen": i.first_seen.isoformat() if i.first_seen else None,
                "last_seen": i.last_seen.isoformat() if i.last_seen else None,
            },
        }
        for i, name in rows
    ]
    await async_bulk(es, docs, refresh="wait_for")
