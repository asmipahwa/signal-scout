"""`create_app()`: the FastAPI application factory.

A factory (not a module-level `app`) so tests can build an app against throwaway containers, and
so importing the module never opens connections. The lifespan opens the DB engine, the
Elasticsearch client and the S3 store once per process and closes them on shutdown.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import version

from fastapi import FastAPI

from ..config import Settings, get_settings
from ..services.context import service_context
from . import ops, v1
from .problems import install_problem_handlers

DESCRIPTION = """Competitor insights from Signal Scout: the ranked feed, full-text search, competitor briefs,
signals with links to their raw objects, outbound webhooks and an HMAC-signed inbound endpoint.

Reads are public. Writes (`POST`/`DELETE /api/v1/webhooks`) need the `X-API-Key` header.
Errors are RFC 9457 `application/problem+json`."""


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the API. Uses `get_settings()` unless settings are given (tests)."""
    cfg = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with service_context(cfg) as ctx:
            app.state.ctx = ctx
            yield

    app = FastAPI(
        title="Signal Scout API",
        version=version("signal-scout"),
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    install_problem_handlers(app)
    app.add_middleware(ops.MetricsMiddleware)
    app.include_router(ops.router)
    app.include_router(v1.router)
    return app
