"""Migrations apply, match the ORM models, round-trip down and up, and evolve the schema safely."""

from __future__ import annotations

import asyncio

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.engine import Connection

from signal_scout.db.base import Base
from signal_scout.db.session import make_engine

from .conftest import alembic_config

EXPECTED_TABLES = {
    "competitors",
    "sources",
    "signals",
    "page_snapshots",
    "enrichments",
    "insights",
    "insight_signals",
    "webhook_subscriptions",
    "webhook_deliveries",
    "job_runs",
    "llm_calls",
    "jobs",
    "alembic_version",
}


async def test_migration_creates_every_table_and_matches_models(migrated_url: str) -> None:
    def check(conn: Connection) -> None:
        insp = inspect(conn)
        assert set(insp.get_table_names()) == EXPECTED_TABLES
        assert "language" in {c["name"] for c in insp.get_columns("signals")}  # added by migration 2
        claimable = next(i for i in insp.get_indexes("jobs") if i["name"] == "ix_jobs_claimable")
        assert "locked_at IS NULL" in str(claimable.get("dialect_options", {}).get("postgresql_where"))
        uniques = {tuple(u["column_names"]) for u in insp.get_unique_constraints("webhook_deliveries")}
        assert ("insight_id", "subscription_id") in uniques
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
        assert diff == [], f"models and migrations disagree: {diff}"

    engine = make_engine(migrated_url)
    async with engine.connect() as conn:
        await conn.run_sync(check)
    await engine.dispose()


def test_downgrade_to_base_and_upgrade_again(migrated_url: str) -> None:
    cfg = alembic_config(migrated_url)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


def _columns(url: str, table: str) -> set[str]:
    """Column names of a table, read through the app's async engine (the test itself stays sync because
    Alembic's async env runs its own event loop)."""

    async def read() -> set[str]:
        engine = make_engine(url)
        try:
            async with engine.connect() as conn:
                return await conn.run_sync(lambda c: {col["name"] for col in inspect(c).get_columns(table)})
        finally:
            await engine.dispose()

    return asyncio.run(read())


def test_migration_2_is_additive_and_reversible(migrated_url: str) -> None:
    """Downgrading one step removes exactly the evolved columns; upgrading restores them."""
    cfg = alembic_config(migrated_url)
    evolved = {"language", "filter_reason", "duplicate_of_id"}
    command.downgrade(cfg, "06fa916cc7de")
    assert not evolved & _columns(migrated_url, "signals")
    command.upgrade(cfg, "head")
    assert evolved <= _columns(migrated_url, "signals")
