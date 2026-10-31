"""The initial migration applies, matches the ORM models, and round-trips down and up."""

from __future__ import annotations

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
        assert "language" not in {c["name"] for c in insp.get_columns("signals")}  # added by a later migration
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
