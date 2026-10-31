"""Every REST endpoint against real Postgres, MinIO and Elasticsearch (testcontainers), via httpx ASGITransport.

Includes InboundWebhookIT: a correctly signed inbound signal becomes a `signals` row plus an S3 object;
a bad signature or a stale timestamp is rejected with 401 and writes nothing. Seed data is invented
fixture data (see interfaces_data.py).
"""

from __future__ import annotations

import gzip
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from elasticsearch import AsyncElasticsearch
from pydantic import SecretStr
from sqlalchemy import func, select

from signal_scout.alerts.signing import SIGNATURE_HEADER, sign
from signal_scout.api.app import create_app
from signal_scout.config import Settings
from signal_scout.db.models import Signal, SignalStatus, Source, SourceType
from signal_scout.db.session import SessionFactory
from signal_scout.storage import S3BlobStore

from .interfaces_data import PAGE_DIFF, Seeded, index_insights, seed

API_KEY = "it-api-key"
INBOUND_SECRET = "it-inbound-secret"


@pytest.fixture
async def es(es_url: str) -> AsyncIterator[AsyncElasticsearch]:
    client = AsyncElasticsearch(es_url)
    yield client
    await client.close()


@pytest.fixture
async def seeded(sessions: SessionFactory, blobs: S3BlobStore, es: AsyncElasticsearch) -> Seeded:
    data = await seed(sessions, blobs)
    await index_insights(es, sessions)
    return data


def _api_settings(settings: Settings, es_url: str, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "elasticsearch_url": es_url,
        "api_key": SecretStr(API_KEY),
        "inbound_webhook_secret": SecretStr(INBOUND_SECRET),
    }
    values.update(overrides)
    return settings.model_copy(update=values)


@pytest.fixture
async def api(settings: Settings, es_url: str, seeded: Seeded) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(_api_settings(settings, es_url))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client


async def test_ops_endpoints(api: httpx.AsyncClient) -> None:
    assert (await api.get("/healthz")).json() == {"status": "ok"}
    ready = await api.get("/readyz")
    assert ready.status_code == 200, ready.text
    assert {k: v["ok"] for k, v in ready.json()["checks"].items()} == {
        "postgres": True,
        "elasticsearch": True,
        "s3": True,
    }
    assert (await api.get("/docs")).status_code == 200
    assert "/api/v1/search" in (await api.get("/openapi.json")).json()["paths"]


async def test_readyz_is_503_with_per_dependency_status(settings: Settings, seeded: Seeded) -> None:
    app = create_app(_api_settings(settings, "http://127.0.0.1:1"))  # nothing listens on port 1
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        resp = await client.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unavailable"
    assert body["checks"]["elasticsearch"]["ok"] is False and body["checks"]["elasticsearch"]["error"]
    assert body["checks"]["postgres"]["ok"] is True and body["checks"]["s3"]["ok"] is True


async def test_ranked_feed_paginates_with_a_cursor(api: httpx.AsyncClient, seeded: Seeded) -> None:
    first = (await api.get("/api/v1/insights", params={"limit": 3})).json()
    assert [i["id"] for i in first["items"]] == [seeded.pricing_insight, seeded.launch_insight, seeded.outage_insight]
    assert first["items"][0]["competitor"] == "Notion"
    second = (await api.get("/api/v1/insights", params={"limit": 3, "cursor": first["next_cursor"]})).json()
    assert [i["id"] for i in second["items"]] == [seeded.old_insight] and second["next_cursor"] is None

    notion = (await api.get("/api/v1/insights", params={"competitor": "notion", "min_importance": 5})).json()
    assert [i["id"] for i in notion["items"]] == [seeded.pricing_insight]
    recent = (await api.get("/api/v1/insights", params={"days": 30})).json()
    assert seeded.old_insight not in [i["id"] for i in recent["items"]]


async def test_insight_detail_and_sources(api: httpx.AsyncClient, seeded: Seeded) -> None:
    detail = (await api.get(f"/api/v1/insights/{seeded.pricing_insight}")).json()
    assert detail["title"] == "Notion raises Plus plan pricing" and detail["signal_count"] == 2
    assert {s["source_type"] for s in detail["signals"]} == {"changelog_rss", "webpage_watch"}
    assert all(s["url"].startswith("https://example.com/fixture/") for s in detail["signals"])
    sources = (await api.get(f"/api/v1/insights/{seeded.pricing_insight}/sources")).json()
    assert len(sources["sources"]) == 2 and sources["competitor"] == "Notion"

    missing = await api.get("/api/v1/insights/999999")
    assert missing.status_code == 404 and missing.headers["content-type"].startswith("application/problem+json")


async def test_search(api: httpx.AsyncClient, seeded: Seeded) -> None:
    result = (await api.get("/api/v1/search", params={"q": "pricing"})).json()
    assert result["total"] >= 1 and result["took_ms"] >= 0
    top = result["hits"][0]
    assert top["insight_id"] == str(seeded.pricing_insight)
    assert "<mark>" in json.dumps(top["highlights"])

    asana = (await api.get("/api/v1/search", params={"competitor": "Asana"})).json()
    assert {h["insight_id"] for h in asana["hits"]} == {str(seeded.outage_insight), str(seeded.old_insight)}
    important = (await api.get("/api/v1/search", params={"min_importance": 4, "size": 1, "page": 2})).json()
    assert important["total"] == 3 and len(important["hits"]) == 1 and important["page"] == 2
    since = (await api.get("/api/v1/search", params={"from": "2000-01-01T00:00:00Z", "category": "partnership"})).json()
    assert [h["insight_id"] for h in since["hits"]] == [str(seeded.old_insight)]

    metrics = (await api.get("/metrics")).text
    assert "search_request_seconds_count" in metrics and 'route="/api/v1/search"' in metrics


async def test_competitors_brief_changes_and_compare(api: httpx.AsyncClient, seeded: Seeded) -> None:
    names = [c["name"] for c in (await api.get("/api/v1/competitors")).json()]
    assert names == ["Asana", "ClickUp", "Notion"]

    brief = (await api.get(f"/api/v1/competitors/{seeded.notion_id}/brief", params={"days": 30})).json()
    assert brief["competitor"]["name"] == "Notion" and brief["insight_count"] == 2
    assert brief["counts_by_category"] == {"pricing_change": 1, "product_launch": 1}
    assert brief["top_insights"][0]["id"] == seeded.pricing_insight
    notable = {c["signal_id"]: c for c in brief["notable_changes"]}
    assert set(notable) == {seeded.pricing_signal, seeded.page_signal}  # the duplicate copy is hidden
    assert notable[seeded.page_signal]["diff"] == PAGE_DIFF
    assert (await api.get("/api/v1/competitors/999/brief")).status_code == 404

    changes = (await api.get("/api/v1/changes", params={"days": 7})).json()["items"]
    assert {c["change_type"] for c in changes} == {"pricing_change", "product_launch"}
    pages = (await api.get("/api/v1/changes", params={"change_type": "webpage_change"})).json()["items"]
    assert [c["signal_id"] for c in pages] == [seeded.page_signal]
    assert (await api.get("/api/v1/changes", params={"change_type": "rumour"})).status_code == 422

    compare = (
        await api.get(
            "/api/v1/compare", params=[("competitor", "Notion"), ("competitor", "Asana"), ("competitor", "Nope")]
        )
    ).json()
    by_name = {c["competitor"]: c for c in compare["competitors"]}
    assert compare["unknown"] == ["Nope"]
    assert by_name["Notion"]["avg_importance"] == 4.5 and by_name["Asana"]["insight_count"] == 1
    assert by_name["Asana"]["top_insight"]["id"] == seeded.outage_insight


async def test_signal_detail_has_a_working_presigned_url(api: httpx.AsyncClient, seeded: Seeded) -> None:
    detail = (await api.get(f"/api/v1/signals/{seeded.pricing_signal}")).json()
    assert detail["source_type"] == "changelog_rss" and detail["category"] == "pricing_change"
    async with httpx.AsyncClient() as plain:
        raw = await plain.get(detail["raw_url"])
    assert raw.status_code == 200
    body = raw.content if raw.content.startswith(b"<html") else gzip.decompress(raw.content)
    assert body == seeded.raw_body
    assert (await api.get("/api/v1/signals/999999")).status_code == 404


async def test_webhook_crud_and_deliveries(api: httpx.AsyncClient, seeded: Seeded) -> None:
    auth = {"X-API-Key": API_KEY}
    payload = {"name": "Sales channel", "url": "https://example.com/fixture/new-hook", "filters": {"min_importance": 4}}
    assert (await api.post("/api/v1/webhooks", json=payload)).status_code == 401
    created = await api.post("/api/v1/webhooks", json=payload, headers=auth)
    assert created.status_code == 201
    hook = created.json()
    assert len(hook["secret"]) == 64  # shown once, at creation

    listed = (await api.get("/api/v1/webhooks")).json()
    assert {h["id"] for h in listed} == {seeded.subscription_id, hook["id"]}
    assert all("secret" not in h for h in listed)
    assert "secret" not in (await api.get(f"/api/v1/webhooks/{hook['id']}")).json()

    deliveries = (await api.get(f"/api/v1/webhooks/{seeded.subscription_id}/deliveries")).json()
    assert len(deliveries) == 1 and deliveries[0]["status"] == "delivered"
    assert deliveries[0]["insight_id"] == seeded.pricing_insight
    assert (await api.get("/api/v1/webhooks/999/deliveries")).status_code == 404

    assert (await api.delete(f"/api/v1/webhooks/{hook['id']}", headers=auth)).status_code == 204
    assert (await api.delete(f"/api/v1/webhooks/{hook['id']}", headers=auth)).status_code == 404
    bad = await api.post("/api/v1/webhooks", json={"name": "x", "url": "ftp://nope"}, headers=auth)
    assert bad.status_code == 422 and bad.json()["errors"][0]["loc"] == ["body", "url"]


async def test_runs(api: httpx.AsyncClient, seeded: Seeded) -> None:
    runs = (await api.get("/api/v1/runs", params={"job_type": "index_insights", "status": "success"})).json()
    assert len(runs) == 1 and runs[0]["items_out"] == 4
    assert (await api.get("/api/v1/runs", params={"status": "exploded"})).status_code == 422


# --- InboundWebhookIT -------------------------------------------------------------------------------

INBOUND = {
    "title": "Fixture: Notion announces a new Enterprise add-on",
    "url": "https://example.com/fixture/inbound/notion-enterprise-addon?utm_source=test",
    "text": "Fixture text. Notion said the add-on is priced separately from Enterprise seats.",
    "published_at": "2026-10-29T12:00:00Z",
    "competitors": ["notion"],
}


async def _post(api: httpx.AsyncClient, body: bytes, header: str) -> httpx.Response:
    headers = {"content-type": "application/json", SIGNATURE_HEADER: header}
    return await api.post("/api/v1/ingest/webhook", content=body, headers=headers)


async def _signal_count(sessions: SessionFactory) -> int:
    async with sessions() as s:
        return int((await s.execute(select(func.count()).select_from(Signal))).scalar_one())


async def test_inbound_webhook_valid_signature_creates_signal_and_s3_object(
    api: httpx.AsyncClient, sessions: SessionFactory, blobs: S3BlobStore, seeded: Seeded
) -> None:
    body = json.dumps(INBOUND).encode()
    resp = await _post(api, body, sign(INBOUND_SECRET, body))
    assert resp.status_code == 202, resp.text
    accepted = resp.json()
    assert accepted["created"] is True and accepted["status"] == "new"

    async with sessions() as s:
        signal, source = (
            await s.execute(select(Signal, Source).join(Source).where(Signal.id == accepted["signal_id"]))
        ).one()
    assert source.type is SourceType.inbound_webhook
    assert signal.status is SignalStatus.new and signal.matched_competitors == ["Notion"]
    assert signal.canonical_url == "https://example.com/fixture/inbound/notion-enterprise-addon"
    assert await blobs.get(signal.s3_key) == body  # raw body stored in S3 as received

    # Same URL again: idempotent, and the inbound source row is reused.
    again = json.dumps({**INBOUND, "text": "updated"}).encode()
    second = (await _post(api, again, sign(INBOUND_SECRET, again))).json()
    assert second == {**accepted, "created": False}
    async with sessions() as s:
        n_sources = (
            await s.execute(select(func.count()).select_from(Source).where(Source.type == SourceType.inbound_webhook))
        ).scalar_one()
    assert n_sources == 1


async def test_inbound_webhook_rejects_bad_and_stale_signatures(
    api: httpx.AsyncClient, sessions: SessionFactory, seeded: Seeded
) -> None:
    body = json.dumps(INBOUND).encode()
    before = await _signal_count(sessions)
    bad = await _post(api, body, sign("not-the-secret", body))
    stale = await _post(api, body, sign(INBOUND_SECRET, body, timestamp=int(time.time()) - 600))
    assert bad.status_code == 401 and "mismatch" in bad.json()["detail"]
    assert stale.status_code == 401 and "timestamp" in stale.json()["detail"]
    assert await _signal_count(sessions) == before

    unknown = json.dumps({**INBOUND, "competitors": ["Globex"]}).encode()
    resp = await _post(api, unknown, sign(INBOUND_SECRET, unknown))
    assert resp.status_code == 422 and "Globex" in resp.json()["detail"]
