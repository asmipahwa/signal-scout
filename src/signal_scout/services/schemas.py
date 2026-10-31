"""Pydantic models the services return and accept.

They are the REST API's response models (so they appear in OpenAPI) and the MCP tools' structured
output (so agents get a JSON schema), which keeps both interfaces' contracts identical. ORM objects
never leave the services layer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- Competitors ------------------------------------------------------------------------------------


class CompetitorOut(_Model):
    """A tracked competitor."""

    id: int
    name: str
    aliases: list[str]
    domains: list[str]


# --- Insights ---------------------------------------------------------------------------------------


class InsightSummary(_Model):
    """One row of the ranked feed."""

    id: int
    competitor: str | None
    title: str
    summary: str
    why_it_matters: str
    category: str
    importance: int
    score: float
    sentiment: float
    signal_count: int
    source_types: list[str]
    first_seen: datetime | None
    last_seen: datetime | None


class InsightPage(BaseModel):
    """A page of the ranked feed. Pass `next_cursor` back as `cursor` for the next page; null means the end."""

    items: list[InsightSummary]
    next_cursor: str | None


class SignalRef(BaseModel):
    """A source article behind an insight, with enough to cite it."""

    id: int
    title: str
    url: str
    source_type: str
    source_name: str
    published_at: datetime | None


class InsightDetail(InsightSummary):
    """An insight with the signals that support it."""

    signals: list[SignalRef]


class InsightSources(BaseModel):
    """The articles behind one insight, for an agent to cite."""

    insight_id: int
    title: str
    competitor: str | None
    sources: list[SignalRef]


# --- Search -----------------------------------------------------------------------------------------


class SearchHit(BaseModel):
    """One Elasticsearch hit. `highlights` maps a field to fragments with `<mark>` tags."""

    insight_id: str
    score: float | None
    headline: str
    summary: str
    why_it_matters: str
    competitor: str | None
    category: str | None
    importance: int | None
    rank_score: float | None
    signal_count: int | None
    source_types: list[str]
    source_urls: list[str]
    first_seen: datetime | None
    last_seen: datetime | None
    highlights: dict[str, list[str]]


class SearchResult(BaseModel):
    """A page of search results."""

    total: int
    took_ms: int
    page: int
    size: int
    hits: list[SearchHit]


# --- Competitor analysis ----------------------------------------------------------------------------


class ChangeItem(BaseModel):
    """A recent pricing change, launch, or watched-page change, with the diff text when we have it."""

    signal_id: int
    competitor: str | None
    change_type: str
    title: str
    url: str
    source_type: str
    published_at: datetime | None
    summary: str | None
    diff: str | None
    insight_id: int | None


class CompetitorBrief(BaseModel):
    """What a competitor has been doing over the last `days` days."""

    competitor: CompetitorOut
    days: int
    insight_count: int
    counts_by_category: dict[str, int]
    top_insights: list[InsightSummary]
    notable_changes: list[ChangeItem]


class RecentChanges(BaseModel):
    """Changes across competitors, newest first."""

    days: int
    items: list[ChangeItem]


class CompetitorComparison(BaseModel):
    """One competitor's numbers over the window, for side-by-side comparison."""

    competitor: str
    insight_count: int
    counts_by_category: dict[str, int]
    avg_importance: float | None
    top_insight: InsightSummary | None


class Comparison(BaseModel):
    """Side-by-side numbers. `unknown` lists requested names that are not tracked competitors."""

    days: int
    competitors: list[CompetitorComparison]
    unknown: list[str]


# --- Signals ----------------------------------------------------------------------------------------


class SignalDetail(BaseModel):
    """A signal's metadata, its enrichment if any, and a short-lived link to the raw object in S3."""

    id: int
    title: str
    url: str
    canonical_url: str
    source_id: int
    source_name: str
    source_type: str
    published_at: datetime | None
    fetched_at: datetime
    status: str
    filter_reason: str | None
    language: str | None
    duplicate_of_id: int | None
    matched_competitors: list[str]
    excerpt: str
    content_hash: str
    s3_key: str
    raw_url: str | None = Field(description="Presigned GET URL for the raw object; expires.")
    raw_url_expires_s: int
    category: str | None
    summary: str | None
    importance: int | None


class InboundSignal(BaseModel):
    """A signal pushed to us by an external tool (signed with INBOUND_WEBHOOK_SECRET)."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=1000)
    url: AnyHttpUrl
    text: str = Field(default="", max_length=200_000, description="Article text or change description.")
    published_at: datetime | None = None
    competitors: list[str] = Field(default_factory=list, max_length=20, description="Tracked competitor names.")


class InboundAccepted(BaseModel):
    """Result of an inbound signal. `created` is false when the same URL was already ingested."""

    signal_id: int
    created: bool
    s3_key: str
    status: str


# --- Webhooks ---------------------------------------------------------------------------------------


class WebhookCreate(BaseModel):
    """Create an outbound webhook. Leave `secret` empty to have one generated."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    url: AnyHttpUrl
    filters: dict[str, Any] = Field(
        default_factory=dict, description='e.g. {"min_importance": 4, "competitors": ["Notion"]}'
    )
    enabled: bool = True
    secret: str | None = Field(default=None, min_length=16, max_length=200)


class WebhookOut(_Model):
    """An outbound webhook. The signing secret is never returned after creation."""

    id: int
    name: str
    url: str
    filters: dict[str, Any]
    enabled: bool
    created_at: datetime


class WebhookCreated(WebhookOut):
    """Returned once, at creation: includes the signing secret. Store it now; it is not shown again."""

    secret: str


class DeliveryOut(_Model):
    """One attempt to deliver one insight to one webhook."""

    id: int
    insight_id: int
    subscription_id: int
    status: str
    attempts: int
    last_status_code: int | None
    last_error: str | None
    delivered_at: datetime | None
    created_at: datetime


# --- Job runs ---------------------------------------------------------------------------------------


class RunOut(_Model):
    """One row of `job_runs`."""

    id: int
    job_type: str
    run_id: str
    started_at: datetime
    finished_at: datetime | None
    status: str
    items_in: int
    items_out: int
    errors: int
    last_error: str | None
    tokens: int
    cost_usd: float
    details: dict[str, Any]
