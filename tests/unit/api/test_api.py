"""REST API behaviour that needs no database: problem+json errors, write auth, inbound signature checks, ops.

The app gets a stub ServiceContext whose session is a bare object, so any handler that reaches the
database fails loudly (a 500); the paths tested here must be decided before that.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from signal_scout.alerts.signing import SIGNATURE_HEADER, sign
from signal_scout.api.app import create_app
from signal_scout.config import Settings
from signal_scout.services.context import ServiceContext
from signal_scout.storage import MemoryBlobStore

API_KEY = "unit-test-api-key"
INBOUND_SECRET = "unit-test-inbound-secret"
PROBLEM = "application/problem+json"


@asynccontextmanager
async def _no_db() -> AsyncIterator[Any]:
    yield object()  # any attribute access (session.execute) raises AttributeError -> 500


def _app(**overrides: Any) -> FastAPI:
    values: dict[str, Any] = {"api_key": SecretStr(API_KEY), "inbound_webhook_secret": SecretStr(INBOUND_SECRET)}
    values.update(overrides)
    settings = Settings(_env_file=None, **values)  # type: ignore[call-arg]
    app = create_app(settings)
    app.state.ctx = ServiceContext(settings=settings, sessions=_no_db, es=None, blobs=MemoryBlobStore())  # type: ignore[arg-type]
    return app


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _assert_problem(resp: httpx.Response, status: int) -> dict[str, Any]:
    assert resp.status_code == status
    assert resp.headers["content-type"].startswith(PROBLEM)
    body: dict[str, Any] = resp.json()
    assert body["status"] == status
    assert {"type", "title", "status"} <= body.keys()
    return body


# --- problem+json -----------------------------------------------------------------------------------


async def test_unknown_route_is_a_404_problem(client: httpx.AsyncClient) -> None:
    body = _assert_problem(await client.get("/api/v1/nope"), 404)
    assert body["type"] == "about:blank" and body["title"] == "Not Found"


async def test_query_validation_error_is_a_422_problem_with_field_errors(client: httpx.AsyncClient) -> None:
    body = _assert_problem(await client.get("/api/v1/search", params={"size": 999, "min_importance": 9}), 422)
    assert body["type"] == "urn:signal-scout:problem:validation-error"
    locs = {tuple(e["loc"]) for e in body["errors"]}
    assert ("query", "size") in locs and ("query", "min_importance") in locs
    assert all("input" not in e for e in body["errors"])  # never echo the input back


async def test_service_error_maps_to_its_problem_type(client: httpx.AsyncClient) -> None:
    body = _assert_problem(await client.get("/api/v1/insights", params={"cursor": "not-a-cursor"}), 422)
    assert body["type"] == "urn:signal-scout:problem:invalid-request"
    assert body["detail"] == "invalid cursor"


async def test_unexpected_error_is_a_500_problem_without_internals(client: httpx.AsyncClient) -> None:
    body = _assert_problem(await client.get("/api/v1/competitors"), 500)
    assert body["detail"] == "unexpected server error"
    assert "AttributeError" not in json.dumps(body)


def test_openapi_documents_problem_responses() -> None:
    spec = _app().openapi()
    op = spec["paths"]["/api/v1/insights/{insight_id}"]["get"]
    assert PROBLEM in op["responses"]["404"]["content"]
    assert "X-API-Key" in json.dumps(spec["components"]["securitySchemes"])


# --- API-key auth on writes -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [("POST", "/api/v1/webhooks"), ("DELETE", "/api/v1/webhooks/1")],
)
@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}])
async def test_writes_require_the_api_key(
    client: httpx.AsyncClient, method: str, path: str, headers: dict[str, str]
) -> None:
    body = {"name": "x", "url": "https://example.com/hook"}
    resp = await client.request(method, path, headers=headers, json=body if method == "POST" else None)
    _assert_problem(resp, 401)
    assert resp.headers["www-authenticate"] == "ApiKey"


async def test_writes_are_disabled_without_a_configured_key() -> None:
    transport = httpx.ASGITransport(app=_app(api_key=SecretStr("")), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.delete("/api/v1/webhooks/1", headers={"X-API-Key": ""})
    _assert_problem(resp, 503)


async def test_reads_do_not_require_the_api_key(client: httpx.AsyncClient) -> None:
    # Reaches the (stub) database instead of stopping at auth: 500 here, not 401.
    assert (await client.get("/api/v1/webhooks")).status_code == 500


# --- inbound webhook signatures ---------------------------------------------------------------------

INBOUND = json.dumps({"title": "Acme raises Pro plan price", "url": "https://example.com/acme-pricing"}).encode()


async def _post_inbound(client: httpx.AsyncClient, body: bytes, header: str | None) -> httpx.Response:
    headers = {"content-type": "application/json"}
    if header is not None:
        headers[SIGNATURE_HEADER] = header
    return await client.post("/api/v1/ingest/webhook", content=body, headers=headers)


@pytest.mark.parametrize(
    "header,reason",
    [
        (None, "missing"),
        ("not-a-signature", "malformed"),
        (sign("some-other-secret", INBOUND), "mismatch"),
        (sign(INBOUND_SECRET, INBOUND, timestamp=int(time.time()) - 3600), "timestamp"),
        (sign(INBOUND_SECRET, INBOUND + b" "), "mismatch"),  # body altered after signing
    ],
)
async def test_inbound_rejects_bad_signatures(client: httpx.AsyncClient, header: str | None, reason: str) -> None:
    body = _assert_problem(await _post_inbound(client, INBOUND, header), 401)
    assert body["type"] == "urn:signal-scout:problem:invalid-signature"
    assert reason in body["detail"]


async def test_inbound_validates_the_body_after_the_signature(client: httpx.AsyncClient) -> None:
    bad = json.dumps({"title": "", "url": "not a url", "extra": 1}).encode()
    body = _assert_problem(await _post_inbound(client, bad, sign(INBOUND_SECRET, bad)), 422)
    assert {e["loc"][0] for e in body["errors"]} == {"title", "url", "extra"}


async def test_inbound_is_disabled_without_a_secret() -> None:
    transport = httpx.ASGITransport(app=_app(inbound_webhook_secret=SecretStr("")), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await _post_inbound(c, INBOUND, sign("", INBOUND))
    _assert_problem(resp, 503)


# --- ops --------------------------------------------------------------------------------------------


async def test_healthz_and_metrics(client: httpx.AsyncClient) -> None:
    assert (await client.get("/healthz")).json() == {"status": "ok"}
    await client.get("/api/v1/insights/123/sources")  # a templated route, to check the label
    text = (await client.get("/metrics")).text
    assert 'http_request_seconds_count{method="GET",route="/healthz",status="200"}' in text
    assert 'route="/api/v1/insights/{insight_id}/sources"' in text
    assert "search_request_seconds" in text
