"""The long-lived clients a request handler or MCP tool needs, built and closed in one place.

The API lifespan and the MCP server lifespan both use `service_context`, so they connect to
Postgres, Elasticsearch and S3 with the same settings and shut down the same way.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from elasticsearch import AsyncElasticsearch

from ..config import Settings
from ..db.session import SessionFactory, make_engine, make_session_factory
from ..storage import S3BlobStore


@dataclass
class ServiceContext:
    """Everything the services need besides the request itself."""

    settings: Settings
    sessions: SessionFactory
    es: AsyncElasticsearch
    blobs: S3BlobStore


def make_es_client(settings: Settings) -> AsyncElasticsearch:
    """Elasticsearch client; uses basic auth as user `elastic` when ELASTIC_PASSWORD is set (compose)."""
    password = settings.elastic_password.get_secret_value()
    return AsyncElasticsearch(
        settings.elasticsearch_url,
        basic_auth=("elastic", password) if password else None,
        request_timeout=10,
        retry_on_timeout=True,
        max_retries=2,
    )


@asynccontextmanager
async def service_context(settings: Settings) -> AsyncIterator[ServiceContext]:
    """Yield a ServiceContext and close the engine and the ES connection pool afterwards."""
    engine = make_engine(settings.sqlalchemy_url)
    es = make_es_client(settings)
    try:
        yield ServiceContext(
            settings=settings, sessions=make_session_factory(engine), es=es, blobs=S3BlobStore(settings)
        )
    finally:
        await es.close()
        await engine.dispose()
