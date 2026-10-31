"""Liveness, readiness and Prometheus metrics, plus the middleware that times every request.

`/healthz` only says the process is up (so an orchestrator doesn't restart it for a database
blip); `/readyz` checks each dependency and returns 503 with per-dependency status when any fails,
so traffic is held back until the API can actually answer.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel
from sqlalchemy import text
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..log import get_logger
from ..metrics import HTTP_REQUEST_SECONDS, REGISTRY
from .deps import Ctx

log = get_logger(__name__)
CHECK_TIMEOUT_S = 3.0

router = APIRouter(tags=["ops"])


class Health(BaseModel):
    """Liveness: the process is serving requests."""

    status: Literal["ok"] = "ok"


class DependencyStatus(BaseModel):
    """One dependency's readiness."""

    ok: bool
    latency_ms: float
    error: str | None = None


class Readiness(BaseModel):
    """Readiness of every dependency; `status` is `ok` only if all are."""

    status: Literal["ok", "unavailable"]
    checks: dict[str, DependencyStatus]


@router.get("/healthz", response_model=Health)
async def healthz() -> Health:
    """Liveness probe; touches no dependency."""
    return Health()


async def _check(check: Callable[[], Awaitable[object]]) -> DependencyStatus:
    started = time.perf_counter()
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_S):
            await check()
    except Exception as exc:
        return DependencyStatus(
            ok=False, latency_ms=round((time.perf_counter() - started) * 1000, 1), error=type(exc).__name__
        )
    return DependencyStatus(ok=True, latency_ms=round((time.perf_counter() - started) * 1000, 1))


@router.get("/readyz", response_model=Readiness, responses={503: {"model": Readiness}})
async def readyz(ctx: Ctx) -> JSONResponse:
    """Readiness probe: Postgres `SELECT 1`, Elasticsearch ping, S3 HEAD bucket."""

    async def postgres() -> None:
        async with ctx.sessions() as session:
            await session.execute(text("SELECT 1"))

    async def elasticsearch() -> None:
        if not await ctx.es.ping():
            raise ConnectionError("ping failed")

    names = ("postgres", "elasticsearch", "s3")
    results = await asyncio.gather(_check(postgres), _check(elasticsearch), _check(ctx.blobs.ping))
    checks = dict(zip(names, results, strict=True))
    ok = all(c.ok for c in results)
    if not ok:
        log.warning("not_ready", **{n: c.error for n, c in checks.items() if not c.ok})
    body = Readiness(status="ok" if ok else "unavailable", checks=checks)
    return JSONResponse(body.model_dump(), status_code=200 if ok else 503)


@router.get("/metrics", response_class=Response, include_in_schema=False)
async def metrics() -> Response:
    """Prometheus exposition of `signal_scout.metrics.REGISTRY`."""
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)


class MetricsMiddleware:
    """Pure ASGI middleware: observe `http_request_seconds{route,method,status}` for every HTTP request.

    The label is the route template (`/api/v1/insights/{insight_id}`), never the raw path, so ids
    don't explode the number of time series.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route = scope.get("route")
            label = getattr(route, "path", None) or "unmatched"
            HTTP_REQUEST_SECONDS.labels(route=label, method=scope["method"], status=str(status)).observe(
                time.perf_counter() - started
            )
