"""Throwaway Postgres, MinIO and Elasticsearch containers (testcontainers) for integration tests.

Every test in this directory is marked `integration`. If Docker is not reachable, they are all
skipped with a clear reason instead of erroring. Containers are started once per session; each
test gets freshly truncated tables.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import text
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import HttpWaitStrategy

from signal_scout.config import Settings
from signal_scout.db.base import Base
from signal_scout.db.session import SessionFactory, make_engine, make_session_factory
from signal_scout.storage import S3BlobStore

ROOT = Path(__file__).resolve().parents[2]
POSTGRES_IMAGE = "postgres:16.15-alpine"  # same image as docker-compose.yml
MINIO_IMAGE = "bitnamilegacy/minio:2025.7.23-debian-12-r5"
MINIO_USER = "test-access-key"
MINIO_PASSWORD = "test-secret-key-not-a-real-secret"  # throwaway container credential
BUCKET = "signal-scout-test"
ES_IMAGE = "docker.elastic.co/elasticsearch/elasticsearch:8.19.5"  # same as docker-compose.yml


def _docker_available() -> bool:
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:
        return False


DOCKER_AVAILABLE = _docker_available()
if not DOCKER_AVAILABLE and os.environ.get("REQUIRE_DOCKER") == "1":
    raise RuntimeError("REQUIRE_DOCKER=1 but Docker is not reachable; integration tests would be skipped")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    here = Path(__file__).parent
    skip = pytest.mark.skip(reason="Docker is not available; integration tests need testcontainers")
    for item in items:
        if Path(str(item.fspath)).is_relative_to(here):
            item.add_marker(pytest.mark.integration)
            if not DOCKER_AVAILABLE:
                item.add_marker(skip)


def alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["url"] = url
    cfg.attributes["configure_logger"] = False
    return cfg


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    with PostgresContainer(POSTGRES_IMAGE, username="test", password="test", dbname="test", driver="asyncpg") as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="session")
def migrated_url(postgres_url: str) -> str:
    command.upgrade(alembic_config(postgres_url), "head")
    return postgres_url


@pytest.fixture(scope="session")
def minio_endpoint() -> Iterator[str]:
    container = (
        DockerContainer(MINIO_IMAGE)
        .with_env("MINIO_ROOT_USER", MINIO_USER)
        .with_env("MINIO_ROOT_PASSWORD", MINIO_PASSWORD)
        .with_exposed_ports(9000)
        .waiting_for(HttpWaitStrategy(9000, "/minio/health/live").for_status_code(200))
    )
    with container:
        yield f"http://{container.get_container_host_ip()}:{container.get_exposed_port(9000)}"


@pytest.fixture(scope="session")
def es_url() -> Iterator[str]:
    """Single-node Elasticsearch, security off, 512m heap (keeps the laptop and CI runner happy)."""
    container = (
        DockerContainer(ES_IMAGE)
        .with_env("discovery.type", "single-node")
        .with_env("xpack.security.enabled", "false")
        .with_env("ES_JAVA_OPTS", "-Xms512m -Xmx512m")
        .with_env("cluster.routing.allocation.disk.threshold_enabled", "false")
        .with_exposed_ports(9200)
        .waiting_for(HttpWaitStrategy(9200, "/_cluster/health").for_status_code(200))
    )
    with container:
        yield f"http://{container.get_container_host_ip()}:{container.get_exposed_port(9200)}"


@pytest.fixture
def settings(migrated_url: str, minio_endpoint: str) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=SecretStr(migrated_url),
        s3_endpoint_url=minio_endpoint,
        s3_bucket=BUCKET,
        s3_access_key_id=SecretStr(MINIO_USER),
        s3_secret_access_key=SecretStr(MINIO_PASSWORD),
        circuit_breaker_threshold=3,
    )


@pytest.fixture
async def sessions(migrated_url: str) -> AsyncIterator[SessionFactory]:
    engine = make_engine(migrated_url)
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    yield make_session_factory(engine)
    await engine.dispose()


@pytest.fixture
async def blobs(settings: Settings) -> S3BlobStore:
    store = S3BlobStore(settings)
    await store.ensure_bucket()
    return store
