"""`/api/v1` routes. Each handler parses parameters, calls one service function, and returns its model.

No query logic lives here: the MCP server calls the same services, so behaviour stays identical.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response

from ..alerts.signing import SIGNATURE_HEADER, SignatureError, verify
from ..log import get_logger
from ..search.query import MAX_PAGE_SIZE, SearchParams
from ..services import competitors, insights, runs, signals, webhooks
from ..services.insights import MAX_FEED_LIMIT, InsightFilters
from ..services.schemas import (
    Comparison,
    CompetitorBrief,
    CompetitorOut,
    DeliveryOut,
    InboundAccepted,
    InboundSignal,
    InsightDetail,
    InsightPage,
    InsightSources,
    RecentChanges,
    RunOut,
    SearchResult,
    SignalDetail,
    WebhookCreate,
    WebhookCreated,
    WebhookOut,
)
from .deps import Ctx, Session, require_api_key
from .problems import PROBLEM_RESPONSES, ProblemResponse, problem

log = get_logger(__name__)
MAX_INBOUND_BYTES = 1_000_000

router = APIRouter(prefix="/api/v1", responses=PROBLEM_RESPONSES)
write = [Depends(require_api_key)]

Days = Annotated[int, Query(ge=1, le=365, description="Look-back window in days.")]
Importance = Annotated[int | None, Query(ge=1, le=5)]


# --- Insights ---------------------------------------------------------------------------------------


@router.get("/insights", response_model=InsightPage, tags=["insights"])
async def list_insights(
    session: Session,
    competitor: str | None = None,
    category: str | None = None,
    min_importance: Importance = None,
    days: Annotated[int | None, Query(ge=1, le=365, description="Only insights active in the last N days.")] = None,
    cursor: str | None = Query(default=None, description="`next_cursor` from the previous page."),
    limit: Annotated[int, Query(ge=1, le=MAX_FEED_LIMIT)] = 20,
) -> InsightPage:
    """The ranked insight feed (score desc), cursor-paginated."""
    since = datetime.now(UTC) - timedelta(days=days) if days else None
    filters = InsightFilters(competitor=competitor, category=category, min_importance=min_importance, since=since)
    return await insights.list_insights(session, filters, cursor, limit)


@router.get("/insights/{insight_id}", response_model=InsightDetail, tags=["insights"])
async def get_insight(session: Session, insight_id: int) -> InsightDetail:
    """One insight with the signals behind it."""
    return await insights.get_insight(session, insight_id)


@router.get("/insights/{insight_id}/sources", response_model=InsightSources, tags=["insights"])
async def get_insight_sources(session: Session, insight_id: int) -> InsightSources:
    """The source articles for an insight, for citation."""
    return await insights.get_signal_sources(session, insight_id)


@router.get("/search", response_model=SearchResult, tags=["insights"])
async def search(
    ctx: Ctx,
    q: str = Query(default="", max_length=500, description="Free text; empty returns the ranked feed."),
    competitor: str | None = None,
    category: str | None = None,
    date_from: Annotated[datetime | None, Query(alias="from", description="last_seen >= this (ISO 8601).")] = None,
    date_to: Annotated[datetime | None, Query(alias="to", description="last_seen <= this (ISO 8601).")] = None,
    min_importance: Importance = None,
    page: Annotated[int, Query(ge=1, le=200)] = 1,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 20,
) -> SearchResult:
    """Full-text insight search (Elasticsearch) with highlights."""
    params = SearchParams(
        q=q,
        competitor=competitor,
        category=category,
        date_from=date_from,
        date_to=date_to,
        min_importance=min_importance,
        page=page,
        size=size,
    )
    return await insights.search_insights(ctx.es, params)


# --- Competitors ------------------------------------------------------------------------------------


@router.get("/competitors", response_model=list[CompetitorOut], tags=["competitors"])
async def list_competitors(session: Session) -> list[CompetitorOut]:
    """Every tracked competitor."""
    return await competitors.list_competitors(session)


@router.get("/competitors/{competitor_id}/brief", response_model=CompetitorBrief, tags=["competitors"])
async def competitor_brief(session: Session, competitor_id: int, days: Days = 30) -> CompetitorBrief:
    """Top insights, counts by category and notable changes for one competitor."""
    return await competitors.competitor_brief(session, competitor_id, days)


@router.get("/changes", response_model=RecentChanges, tags=["competitors"])
async def recent_changes(
    session: Session,
    competitor: str | None = None,
    change_type: str | None = Query(default=None, description="pricing_change, product_launch or webpage_change."),
    days: Days = 7,
) -> RecentChanges:
    """Recent pricing changes, launches and watched-page diffs."""
    return await competitors.list_recent_changes(session, competitor, change_type, days)


@router.get("/compare", response_model=Comparison, tags=["competitors"])
async def compare(
    session: Session,
    competitor: Annotated[list[str], Query(min_length=1, max_length=10, description="Repeat per name.")],
    days: Days = 30,
) -> Comparison:
    """Side-by-side counts, average importance and top insight."""
    return await competitors.compare_competitors(session, competitor, days)


# --- Signals ----------------------------------------------------------------------------------------


@router.get("/signals/{signal_id}", response_model=SignalDetail, tags=["signals"])
async def get_signal(ctx: Ctx, session: Session, signal_id: int) -> SignalDetail:
    """Signal metadata and a presigned URL for its raw object."""
    return await signals.get_signal(session, ctx.blobs, signal_id, ctx.settings.presign_expires_s)


@router.post(
    "/ingest/webhook",
    response_model=InboundAccepted,
    status_code=202,
    tags=["signals"],
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": InboundSignal.model_json_schema()}},
        },
        "parameters": [
            {
                "name": SIGNATURE_HEADER,
                "in": "header",
                "required": True,
                "schema": {"type": "string"},
                "description": "t=<unix>,v1=<hex HMAC-SHA256 of '<t>.<raw body>'>",
            }
        ],
    },
)
async def ingest_webhook(request: Request, ctx: Ctx, session: Session) -> InboundAccepted | ProblemResponse:
    """Accept a signal from an external tool. The raw body must be signed with INBOUND_WEBHOOK_SECRET."""
    secret = ctx.settings.inbound_webhook_secret.get_secret_value()
    if not secret:
        return problem(503, "inbound webhooks are disabled: INBOUND_WEBHOOK_SECRET is not configured")
    body = await request.body()
    if len(body) > MAX_INBOUND_BYTES:
        return problem(413, f"body larger than {MAX_INBOUND_BYTES} bytes")
    try:
        verify(secret, body, request.headers.get(SIGNATURE_HEADER))
    except SignatureError as exc:
        log.warning("inbound_signature_rejected", reason=str(exc))
        return problem(401, str(exc), slug="invalid-signature", title="Invalid signature")
    # Parsed only after the signature check; a pydantic ValidationError becomes a 422 problem.
    payload = InboundSignal.model_validate_json(body)
    return await signals.ingest_inbound_signal(session, ctx.blobs, payload, body, ctx.settings.excerpt_chars)


# --- Webhooks ---------------------------------------------------------------------------------------


@router.post("/webhooks", response_model=WebhookCreated, status_code=201, dependencies=write, tags=["webhooks"])
async def create_webhook(session: Session, data: WebhookCreate) -> WebhookCreated:
    """Create an outbound webhook. The response is the only time the signing secret is returned."""
    return await webhooks.create_webhook(session, data)


@router.get("/webhooks", response_model=list[WebhookOut], tags=["webhooks"])
async def list_webhooks(session: Session) -> list[WebhookOut]:
    """Every outbound webhook (without secrets)."""
    return await webhooks.list_webhooks(session)


@router.get("/webhooks/{webhook_id}", response_model=WebhookOut, tags=["webhooks"])
async def get_webhook(session: Session, webhook_id: int) -> WebhookOut:
    """One outbound webhook (without its secret)."""
    return await webhooks.get_webhook(session, webhook_id)


@router.delete("/webhooks/{webhook_id}", status_code=204, dependencies=write, tags=["webhooks"])
async def delete_webhook(session: Session, webhook_id: int) -> Response:
    """Delete an outbound webhook and its delivery log."""
    await webhooks.delete_webhook(session, webhook_id)
    return Response(status_code=204)


@router.get("/webhooks/{webhook_id}/deliveries", response_model=list[DeliveryOut], tags=["webhooks"])
async def list_deliveries(
    session: Session, webhook_id: int, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> list[DeliveryOut]:
    """Delivery attempts for one webhook, newest first."""
    return await webhooks.list_deliveries(session, webhook_id, limit)


# --- Runs -------------------------------------------------------------------------------------------


@router.get("/runs", response_model=list[RunOut], tags=["ops"])
async def list_runs(
    session: Session,
    job_type: str | None = None,
    status: str | None = Query(default=None, description="running, success or failed."),
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[RunOut]:
    """Recent pipeline job runs from `job_runs`."""
    return await runs.list_runs(session, job_type, status, limit)
