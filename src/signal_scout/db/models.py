"""ORM models: the system of record for competitors, sources, signals, enrichments and operations.

Postgres holds metadata and short excerpts only. Raw bodies and full extracted text live in S3,
addressed by content hash, so the database stays small and cheap to back up.

Schema changes go through Alembic migrations in `migrations/`; never edit a table by hand.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Interval,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, CreatedAtMixin


class SourceType(enum.StrEnum):
    """How a source is fetched and parsed."""

    news_rss = "news_rss"
    press_release_rss = "press_release_rss"
    blog_rss = "blog_rss"
    changelog_rss = "changelog_rss"
    webpage_watch = "webpage_watch"
    hn_search = "hn_search"
    inbound_webhook = "inbound_webhook"


RSS_SOURCE_TYPES = frozenset(
    {SourceType.news_rss, SourceType.press_release_rss, SourceType.blog_rss, SourceType.changelog_rss}
)


class SignalStatus(enum.StrEnum):
    """Where a signal is in the pipeline. Only `new` signals are sent to the LLM."""

    new = "new"
    filtered_out = "filtered_out"
    duplicate = "duplicate"
    enriched = "enriched"
    not_relevant = "not_relevant"
    enrichment_failed = "enrichment_failed"
    deferred = "deferred"


class JobRunStatus(enum.StrEnum):
    """Outcome of one job execution recorded in `job_runs`."""

    running = "running"
    success = "success"
    failed = "failed"


class DeliveryStatus(enum.StrEnum):
    """State of one webhook delivery."""

    pending = "pending"
    delivered = "delivered"
    failed = "failed"


def _pg_enum(cls: type[enum.StrEnum], name: str) -> Enum:
    return Enum(cls, name=name, values_callable=lambda e: [m.value for m in e])


TextArray = ARRAY(Text)


class Competitor(CreatedAtMixin, Base):
    """A company we track. Aliases are matched case-sensitively ("Notion" the app, not "a notion")."""

    __tablename__ = "competitors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    aliases: Mapped[list[str]] = mapped_column(TextArray, server_default=text("'{}'"))
    domains: Mapped[list[str]] = mapped_column(TextArray, server_default=text("'{}'"))

    sources: Mapped[list[Source]] = relationship(back_populates="competitor")


class Source(CreatedAtMixin, Base):
    """A feed, page or API query we poll. Holds the HTTP caching and circuit-breaker state for it."""

    __tablename__ = "sources"
    __table_args__ = (UniqueConstraint("type", "url"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    type: Mapped[SourceType] = mapped_column(_pg_enum(SourceType, "source_type"))
    url: Mapped[str] = mapped_column(Text)
    poll_interval: Mapped[timedelta] = mapped_column(Interval, server_default=text("interval '1 hour'"))
    # NULL for general feeds (TechCrunch, PR Newswire) whose items must mention a competitor to count.
    competitor_id: Mapped[int | None] = mapped_column(ForeignKey("competitors.id", ondelete="SET NULL"))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    disabled_reason: Mapped[str | None] = mapped_column(Text)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    etag: Mapped[str | None] = mapped_column(Text)
    last_modified: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, server_default=text("0"))

    competitor: Mapped[Competitor | None] = relationship(back_populates="sources")


class Signal(Base):
    """One item from a source (article, release note, HN story). Full text is in S3, not here."""

    __tablename__ = "signals"
    __table_args__ = (UniqueConstraint("source_id", "canonical_url"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    url: Mapped[str] = mapped_column(Text)
    canonical_url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    s3_key: Mapped[str] = mapped_column(Text)
    excerpt: Mapped[str] = mapped_column(Text, server_default=text("''"))
    matched_competitors: Mapped[list[str]] = mapped_column(TextArray, server_default=text("'{}'"))
    status: Mapped[SignalStatus] = mapped_column(
        _pg_enum(SignalStatus, "signal_status"), server_default=SignalStatus.new.value, index=True
    )
    # Added in migration 2 (schema evolution): detected language, why a signal was filtered, and which
    # signal a near-duplicate copies.
    language: Mapped[str | None] = mapped_column(String(10))
    filter_reason: Mapped[str | None] = mapped_column(String(100))
    duplicate_of_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id", ondelete="SET NULL"))

    enrichment: Mapped[Enrichment | None] = relationship(back_populates="signal")


class PageSnapshot(Base):
    """A fetched version of a watched web page, for change detection (webpage_watch sources)."""

    __tablename__ = "page_snapshots"
    __table_args__ = (Index("ix_page_snapshots_source_id_fetched_at", "source_id", text("fetched_at DESC")),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    url: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    content_hash: Mapped[str] = mapped_column(String(64))
    s3_key: Mapped[str] = mapped_column(Text)


class Enrichment(CreatedAtMixin, Base):
    """The validated LLM output for a signal (1:1), plus which provider, model and prompt produced it."""

    __tablename__ = "enrichments"

    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id", ondelete="CASCADE"), primary_key=True)
    is_about_tracked_competitor: Mapped[bool] = mapped_column(Boolean)
    category: Mapped[str] = mapped_column(String(50), index=True)
    summary: Mapped[str] = mapped_column(Text)
    why_it_matters: Mapped[str] = mapped_column(Text)
    importance: Mapped[int] = mapped_column(SmallInteger)
    sentiment: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    competitors: Mapped[list[str]] = mapped_column(TextArray, server_default=text("'{}'"))
    provider: Mapped[str] = mapped_column(String(50))
    model: Mapped[str] = mapped_column(String(100))
    prompt_version: Mapped[str] = mapped_column(String(50))
    input_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    cache_read_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    cache_write_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    thinking_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))

    signal: Mapped[Signal] = relationship(back_populates="enrichment")


class Insight(CreatedAtMixin, Base):
    """A ranked, deduplicated finding built from one or more enriched signals."""

    __tablename__ = "insights"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    competitor_id: Mapped[int | None] = mapped_column(ForeignKey("competitors.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(50))
    importance: Mapped[int] = mapped_column(SmallInteger)
    score: Mapped[float] = mapped_column(Float, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Added in migration 2: what the ranked feed, search index and alerts need without re-joining signals.
    why_it_matters: Mapped[str] = mapped_column(Text, server_default=text("''"))
    sentiment: Mapped[float] = mapped_column(Float, server_default=text("0"))
    signal_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    source_types: Mapped[list[str]] = mapped_column(TextArray, server_default=text("'{}'"))
    first_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    slack_alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class InsightSignal(Base):
    """Links an insight to the signals that support it."""

    __tablename__ = "insight_signals"

    insight_id: Mapped[int] = mapped_column(ForeignKey("insights.id", ondelete="CASCADE"), primary_key=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id", ondelete="CASCADE"), primary_key=True)


class WebhookSubscription(CreatedAtMixin, Base):
    """An outbound webhook (e.g. Slack) and the filters deciding which insights it receives."""

    __tablename__ = "webhook_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(Text)
    filters: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    # Added in migration 2: HMAC signing secret shared with the receiver.
    secret: Mapped[str] = mapped_column(Text, server_default=text("''"))


class WebhookDelivery(CreatedAtMixin, Base):
    """One insight sent to one subscription. The unique pair makes sending idempotent."""

    __tablename__ = "webhook_deliveries"
    __table_args__ = (UniqueConstraint("insight_id", "subscription_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    insight_id: Mapped[int] = mapped_column(ForeignKey("insights.id", ondelete="CASCADE"))
    subscription_id: Mapped[int] = mapped_column(ForeignKey("webhook_subscriptions.id", ondelete="CASCADE"))
    status: Mapped[DeliveryStatus] = mapped_column(
        _pg_enum(DeliveryStatus, "delivery_status"), server_default=DeliveryStatus.pending.value
    )
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_status_code: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobRun(Base):
    """One execution of a job, written at start and updated at finish: the pipeline's audit log."""

    __tablename__ = "job_runs"
    __table_args__ = (Index("ix_job_runs_job_type_started_at", "job_type", text("started_at DESC")),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_type: Mapped[str] = mapped_column(String(100))
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[JobRunStatus] = mapped_column(
        _pg_enum(JobRunStatus, "job_run_status"), server_default=JobRunStatus.running.value
    )
    items_in: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    items_out: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    errors: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)
    tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), server_default=text("0"))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))


class LlmCall(CreatedAtMixin, Base):
    """Every LLM request with its token usage and cost, for the CostGuard and spend reporting."""

    __tablename__ = "llm_calls"
    __table_args__ = (Index("ix_llm_calls_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    provider: Mapped[str] = mapped_column(String(50))
    model: Mapped[str] = mapped_column(String(100))
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id", ondelete="SET NULL"))
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    input_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    cache_read_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    cache_write_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    thinking_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), server_default=text("0"))


class Job(CreatedAtMixin, Base):
    """A unit of background work in the Postgres queue (see `signal_scout.queue`).

    A job is claimable when `locked_at IS NULL AND run_after <= now()`. A job that used up its
    attempts keeps `locked_at` set and gets `failed_at`, which parks it as a dead letter.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_claimable", "run_after", postgresql_where=text("locked_at IS NULL")),
        Index("ix_jobs_kind", "kind"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, server_default=text("5"))
    locked_by: Mapped[str | None] = mapped_column(String(200))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
