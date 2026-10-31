"""Builds the long-lived resources (DB engine, S3 store, HTTP client) a process needs, in one place.

The worker, scheduler and seed command all start from here, so they share connection settings
and close everything the same way on shutdown.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from .config import Settings
from .db.session import make_engine, make_session_factory
from .ingest.fetch_source import IngestContext
from .ingest.http import PoliteHttpClient, RetryPolicy
from .ingest.ratelimit import DomainRateLimiter
from .storage import S3BlobStore


@asynccontextmanager
async def ingest_context(settings: Settings) -> AsyncIterator[IngestContext]:
    """Yield an IngestContext and dispose of its engine and HTTP connections afterwards."""
    engine = make_engine(settings.sqlalchemy_url)
    client = PoliteHttpClient.build_client(settings.http_user_agent, settings.http_timeout_s)
    http = PoliteHttpClient(
        client,
        DomainRateLimiter(settings.http_rate_per_domain, settings.http_burst_per_domain),
        RetryPolicy(max_retries=settings.http_max_retries),
    )
    try:
        yield IngestContext(
            settings=settings, sessions=make_session_factory(engine), blobs=S3BlobStore(settings), http=http
        )
    finally:
        await client.aclose()
        await engine.dispose()
