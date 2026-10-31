"""The pipeline end to end on real containers: feed -> S3 + Postgres -> enrichment -> insights -> ES -> alerts.

Uses testcontainers Postgres, MinIO and Elasticsearch 8.19.5, a real Notion release-notes feed saved on
2026-10-30 (replayed with respx), and FakeLLMClient instead of a real LLM, so no API key is needed.

Covers the spec's PipelineEndToEndIT, ReindexFromPostgresIT, WebhookDeliveryIT and CostGuardIT.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx
from elasticsearch import AsyncElasticsearch
from pydantic import SecretStr
from sqlalchemy import func, select
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import HttpWaitStrategy

from signal_scout.alerts.signing import IDEMPOTENCY_HEADER, SIGNATURE_HEADER, verify
from signal_scout.config import Settings
from signal_scout.db.models import (
    Competitor,
    DeliveryStatus,
    Enrichment,
    Insight,
    InsightSignal,
    LlmCall,
    Signal,
    SignalStatus,
    Source,
    SourceType,
    WebhookDelivery,
    WebhookSubscription,
)
from signal_scout.db.session import SessionFactory
from signal_scout.enrichment.fake import FakeLLMClient
from signal_scout.enrichment.models import Enrichment as EnrichmentModel
from signal_scout.enrichment.models import SignalInput
from signal_scout.ingest.fetch_source import IngestContext, fetch_source
from signal_scout.ingest.http import PoliteHttpClient, RetryPolicy
from signal_scout.ingest.ratelimit import DomainRateLimiter
from signal_scout.jobruns import RunStats, track_job_run
from signal_scout.pipeline.alerts import deliver_webhook, send_alerts
from signal_scout.pipeline.enrich import enrich_signals
from signal_scout.pipeline.index import index_insights
from signal_scout.pipeline.insights import build_insights
from signal_scout.search.mapping import INDEX_NAME, versioned_index_name
from signal_scout.search.query import SearchParams, build_search
from signal_scout.storage import S3BlobStore

FEEDS = Path(__file__).resolve().parents[1] / "fixtures" / "feeds"
NOTION_FEED = "https://www.notion.com/releases/rss.xml"
SLACK_URL = "https://hooks.slack.test/services/T000/B000/XXXX"  # fake; respx intercepts it
ES_IMAGE = "docker.elastic.co/elasticsearch/elasticsearch:8.19.5"  # same as docker-compose.yml
ALL_HISTORY = {"window_days": 3650}


@pytest.fixture(scope="session")
def es_url() -> Iterator[str]:
    container = (
        DockerContainer(ES_IMAGE)
        .with_env("discovery.type", "single-node")
        .with_env("xpack.security.enabled", "false")
        .with_env("ES_JAVA_OPTS", "-Xms512m -Xmx512m")
        .with_exposed_ports(9200)
        .waiting_for(HttpWaitStrategy(9200, "/_cluster/health").for_status_code(200))
    )
    with container:
        yield f"http://{container.get_container_host_ip()}:{container.get_exposed_port(9200)}"


@pytest.fixture
async def es(es_url: str) -> AsyncIterator[AsyncElasticsearch]:
    client = AsyncElasticsearch(es_url)
    await client.indices.delete(index=versioned_index_name(), ignore_unavailable=True)
    yield client
    await client.close()


async def _no_sleep(_: float) -> None:
    return None


@pytest.fixture
async def ctx(settings: Settings, sessions: SessionFactory, blobs: S3BlobStore) -> AsyncIterator[IngestContext]:
    settings = settings.model_copy(update={"slack_webhook_url": SecretStr(SLACK_URL)})
    client = PoliteHttpClient.build_client(settings.http_user_agent, 5)
    http = PoliteHttpClient(
        client, DomainRateLimiter(1000, 1000), RetryPolicy(max_retries=1, base_s=0.01), sleep=_no_sleep
    )
    yield IngestContext(settings=settings, sessions=sessions, blobs=blobs, http=http)
    await client.aclose()


def _fake_llm() -> FakeLLMClient:
    """Agent-related releases are important (4); everything else is a minor update (2)."""

    def respond(signal: SignalInput) -> EnrichmentModel:
        important = "agent" in signal.title.lower()
        return EnrichmentModel(
            is_about_tracked_competitor=True,
            competitors=signal.matched_competitors,
            category="product_launch" if important else "feature_update",
            summary=f"Notion shipped: {signal.title}"[:600],
            why_it_matters="Test enrichment from FakeLLMClient, not a real analysis.",
            importance=4 if important else 2,
            sentiment=0.2,
            confidence=0.8,
        )

    return FakeLLMClient(respond)


async def _run(ctx: IngestContext, job: str, handler, payload: dict, **kwargs) -> RunStats:  # type: ignore[no-untyped-def]
    async with track_job_run(ctx.sessions, job) as run:
        await handler(ctx, payload, run, **kwargs)
    return run


async def _ingest_notion(ctx: IngestContext) -> int:
    async with ctx.sessions() as session:
        comp = Competitor(name="Notion", aliases=["Notion"], domains=["notion.com"])
        session.add(comp)
        await session.flush()
        source = Source(
            name="Notion releases",
            type=SourceType.changelog_rss,
            url=NOTION_FEED,
            competitor_id=comp.id,
            poll_interval=timedelta(hours=1),
        )
        session.add(source)
        await session.commit()
        source_id = source.id
    with respx.mock(assert_all_called=False) as mock:
        mock.get(NOTION_FEED).mock(
            return_value=httpx.Response(200, content=(FEEDS / "notion_releases.xml").read_bytes())
        )
        await _run(ctx, "fetch_source", fetch_source, {"source_id": source_id})
    return source_id


async def _search_ids(es: AsyncElasticsearch, params: SearchParams) -> list[str]:
    result = await es.search(index=INDEX_NAME, **build_search(params))
    return [hit["_id"] for hit in result["hits"]["hits"]]


async def test_pipeline_end_to_end_and_reindex_from_postgres(ctx: IngestContext, es: AsyncElasticsearch) -> None:
    await _ingest_notion(ctx)
    async with ctx.sessions() as session:
        ingested = (await session.execute(select(func.count()).select_from(Signal))).scalar_one()
    assert ingested == 154  # every item in the saved feed

    # Enrichment: every signal goes through the (fake) LLM, with an llm_calls row each.
    llm = _fake_llm()
    run = await _run(ctx, "enrich_signals", enrich_signals, {"batch": 500}, client=llm)
    async with ctx.sessions() as session:
        enriched = (
            await session.execute(select(func.count()).where(Signal.status == SignalStatus.enriched))
        ).scalar_one()
        calls = (await session.execute(select(func.count()).select_from(LlmCall))).scalar_one()
        enrichments = (await session.execute(select(func.count()).select_from(Enrichment))).scalar_one()
    assert run.items_in == 154
    assert enriched == enrichments == calls == len(llm.calls)
    assert enriched > 100  # a few titles may be near-duplicates of others

    # Insights: clustered and ranked; every enriched signal belongs to exactly one insight.
    run = await _run(ctx, "build_insights", build_insights, ALL_HISTORY)
    async with ctx.sessions() as session:
        insights = (await session.execute(select(func.count()).select_from(Insight))).scalar_one()
        links = (await session.execute(select(func.count()).select_from(InsightSignal))).scalar_one()
    assert 0 < insights <= enriched
    assert links == enriched

    # Rebuilding insights is idempotent: same ids, no duplicates.
    async with ctx.sessions() as session:
        before = set((await session.execute(select(Insight.id))).scalars())
    await _run(ctx, "build_insights", build_insights, ALL_HISTORY)
    async with ctx.sessions() as session:
        assert set((await session.execute(select(Insight.id))).scalars()) == before

    # Search: indexed from Postgres, served by Elasticsearch.
    run = await _run(ctx, "index_insights", index_insights, {"full": True}, es=es)
    assert run.items_out == insights and run.errors == 0
    feed_ids = await _search_ids(es, SearchParams(size=50))
    agent_ids = await _search_ids(es, SearchParams(q="agents", competitor="Notion", size=50))
    assert feed_ids and agent_ids

    # ReindexFromPostgres: delete the index entirely, rebuild, get identical results.
    await es.indices.delete(index=versioned_index_name())
    await _run(ctx, "index_insights", index_insights, {"full": True}, es=es)
    assert await _search_ids(es, SearchParams(size=50)) == feed_ids
    assert await _search_ids(es, SearchParams(q="agents", competitor="Notion", size=50)) == agent_ids

    # Alerts: each important insight is posted to Slack once, never twice.
    with respx.mock(assert_all_called=False) as mock:
        slack = mock.post(SLACK_URL).mock(return_value=httpx.Response(200, text="ok"))
        alerts_payload = {"fresh_within_days": 3650}
        first = await _run(ctx, "send_alerts", send_alerts, alerts_payload)
        sent = slack.call_count
        await _run(ctx, "send_alerts", send_alerts, alerts_payload)
        assert slack.call_count == sent  # second run sends nothing new
    async with ctx.sessions() as session:
        important = (await session.execute(select(func.count()).where(Insight.importance >= 4))).scalar_one()
    assert sent == important == first.details["slack_sent"] > 0
    body = json.loads(slack.calls[0].request.content)
    assert body["text"].startswith("Notion: ") and body["blocks"][0]["type"] == "header"


async def test_webhook_delivery_retries_until_success_and_is_signed(ctx: IngestContext) -> None:
    await _ingest_notion(ctx)
    await _run(ctx, "enrich_signals", enrich_signals, {"batch": 500}, client=_fake_llm())
    await _run(ctx, "build_insights", build_insights, ALL_HISTORY)
    target = "https://receiver.example.test/hooks/scout"
    async with ctx.sessions() as session:
        session.add(
            WebhookSubscription(
                name="test receiver",
                url=target,
                secret="whsec_test",
                filters={"min_importance": 4, "categories": ["product_launch"]},
            )
        )
        await session.commit()

    with respx.mock(assert_all_called=False) as mock:
        mock.post(SLACK_URL).mock(return_value=httpx.Response(200))
        await _run(ctx, "send_alerts", send_alerts, {"fresh_within_days": 3650})
    async with ctx.sessions() as session:
        delivery_ids = list((await session.execute(select(WebhookDelivery.id))).scalars())
    assert delivery_ids
    delivery_id = delivery_ids[0]

    # The receiver fails twice, then accepts: 3 attempts, delivered exactly once.
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(target).mock(side_effect=[httpx.Response(500), httpx.Response(500), httpx.Response(200)])
        for _ in range(2):
            with pytest.raises(RuntimeError, match="failed"):
                await _run(ctx, "deliver_webhook", deliver_webhook, {"delivery_id": delivery_id})
        await _run(ctx, "deliver_webhook", deliver_webhook, {"delivery_id": delivery_id})
        await _run(ctx, "deliver_webhook", deliver_webhook, {"delivery_id": delivery_id})  # already delivered: no-op
    assert route.call_count == 3
    async with ctx.sessions() as session:
        delivery = await session.get(WebhookDelivery, delivery_id)
    assert delivery is not None
    assert (delivery.status, delivery.attempts, delivery.last_status_code) == (DeliveryStatus.delivered, 3, 200)

    request = route.calls[-1].request
    verify("whsec_test", request.content, request.headers[SIGNATURE_HEADER])
    assert request.headers[IDEMPOTENCY_HEADER] == f"delivery-{delivery_id}"


async def test_costguard_defers_signals_once_the_daily_cap_is_reached(ctx: IngestContext) -> None:
    await _ingest_notion(ctx)
    async with ctx.sessions() as session:
        session.add(LlmCall(provider="claude", model="claude-haiku-5-5", cost_usd=Decimal("0.50")))
        await session.commit()
    llm = _fake_llm()
    run = await _run(ctx, "enrich_signals", enrich_signals, {"batch": 50}, client=llm)
    assert llm.calls == []
    assert "daily LLM spend" in run.details["stopped"]
    async with ctx.sessions() as session:
        deferred = (
            await session.execute(select(func.count()).where(Signal.status == SignalStatus.deferred))
        ).scalar_one()
        enrichments = (await session.execute(select(func.count()).select_from(Enrichment))).scalar_one()
    assert deferred == 50 and enrichments == 0
