"""Evolve the schema for the enrichment, insight, search and alerting stages.

A deliberately additive migration (only nullable columns or columns with defaults, no rewrites of
existing data), so it is safe to apply to a database that is already ingesting:

- signals.language: detected language of the extracted text (the planned schema-evolution column).
- signals.filter_reason / duplicate_of_id: why a signal was dropped before the LLM, and which signal
  a near-duplicate copies.
- insights: why_it_matters, sentiment, signal_count, source_types, first_seen/last_seen, and
  slack_alerted_at so a Slack alert is sent at most once per insight.
- webhook_subscriptions.secret: HMAC signing secret for outbound deliveries.

Revision ID: b7e2a4c19d3f
Revises: 06fa916cc7de
Create Date: 2026-10-31 04:10:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b7e2a4c19d3f"
down_revision: str | Sequence[str] | None = "06fa916cc7de"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("signals", sa.Column("language", sa.String(length=10), nullable=True))
    op.add_column("signals", sa.Column("filter_reason", sa.String(length=100), nullable=True))
    op.add_column("signals", sa.Column("duplicate_of_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_signals_duplicate_of_id_signals"),
        "signals",
        "signals",
        ["duplicate_of_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.add_column("insights", sa.Column("why_it_matters", sa.Text(), server_default=sa.text("''"), nullable=False))
    op.add_column("insights", sa.Column("sentiment", sa.Float(), server_default=sa.text("0"), nullable=False))
    op.add_column("insights", sa.Column("signal_count", sa.Integer(), server_default=sa.text("0"), nullable=False))
    op.add_column(
        "insights",
        sa.Column("source_types", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
    )
    op.add_column("insights", sa.Column("first_seen", sa.DateTime(timezone=True), nullable=True))
    op.add_column("insights", sa.Column("last_seen", sa.DateTime(timezone=True), nullable=True))
    op.add_column("insights", sa.Column("slack_alerted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(op.f("ix_insights_last_seen"), "insights", ["last_seen"], unique=False)

    op.add_column("webhook_subscriptions", sa.Column("secret", sa.Text(), server_default=sa.text("''"), nullable=False))


def downgrade() -> None:
    op.drop_column("webhook_subscriptions", "secret")

    op.drop_index(op.f("ix_insights_last_seen"), table_name="insights")
    for column in (
        "slack_alerted_at",
        "last_seen",
        "first_seen",
        "source_types",
        "signal_count",
        "sentiment",
        "why_it_matters",
    ):
        op.drop_column("insights", column)

    op.drop_constraint(op.f("fk_signals_duplicate_of_id_signals"), "signals", type_="foreignkey")
    for column in ("duplicate_of_id", "filter_reason", "language"):
        op.drop_column("signals", column)
