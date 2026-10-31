"""Alert jobs: Slack messages and signed outbound webhooks for important new insights.

`send_alerts` picks insights at or above the alert importance that are *fresh* (last seen within
ALERT_FRESHNESS), so loading months of history never floods Slack. Each insight is posted to Slack
at most once (`insights.slack_alerted_at`) and delivered to each webhook subscription at most once
(unique `webhook_deliveries(insight_id, subscription_id)`). Webhook POSTs run as separate
`deliver_webhook` queue jobs so a slow or failing receiver is retried with backoff without holding up
anything else.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from .. import queue
from ..alerts.signing import IDEMPOTENCY_HEADER, SIGNATURE_HEADER, sign
from ..alerts.slack import AlertInsight, build_slack_message
from ..db.models import (
    Competitor,
    DeliveryStatus,
    Insight,
    InsightSignal,
    Signal,
    WebhookDelivery,
    WebhookSubscription,
)
from ..ingest.fetch_source import IngestContext
from ..jobruns import RunStats

ALERT_MIN_IMPORTANCE = 4
ALERT_FRESHNESS = timedelta(days=3)
WEBHOOK_MAX_ATTEMPTS = 6
HTTP_TIMEOUT_S = 10.0


async def _alert_view(ctx: IngestContext, insight: Insight) -> AlertInsight:
    async with ctx.sessions() as session:
        competitor = (
            await session.execute(select(Competitor.name).where(Competitor.id == insight.competitor_id))
        ).scalar_one_or_none()
        sources = (
            await session.execute(
                select(Signal.title, Signal.url)
                .join(InsightSignal, InsightSignal.signal_id == Signal.id)
                .where(InsightSignal.insight_id == insight.id)
                .order_by(Signal.published_at.desc().nullslast())
            )
        ).all()
    return AlertInsight(
        insight_id=str(insight.id),
        competitor=competitor or "Unknown",
        category=insight.category,
        importance=insight.importance,
        headline=insight.title,
        summary=insight.summary,
        why_it_matters=insight.why_it_matters,
        sources=[(t, u) for t, u in sources],
    )


def _matches(filters: dict[str, Any], view: AlertInsight) -> bool:
    if view.importance < int(filters.get("min_importance", ALERT_MIN_IMPORTANCE)):
        return False
    if (cats := filters.get("categories")) and view.category not in cats:
        return False
    return not ((comps := filters.get("competitors")) and view.competitor not in comps)


async def send_alerts(ctx: IngestContext, payload: dict[str, Any], run: RunStats) -> None:
    now = datetime.now(UTC)
    min_importance = int(payload.get("min_importance", ALERT_MIN_IMPORTANCE))
    async with ctx.sessions() as session:
        fresh = list(
            (
                await session.execute(
                    select(Insight).where(
                        Insight.importance >= min_importance, Insight.last_seen >= now - ALERT_FRESHNESS
                    )
                )
            ).scalars()
        )
        subscriptions = list(
            (await session.execute(select(WebhookSubscription).where(WebhookSubscription.enabled.is_(True)))).scalars()
        )
    run.items_in = len(fresh)
    slack_url = ctx.settings.slack_webhook_url.get_secret_value()
    slack_sent = queued = 0
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as http:
        for insight in fresh:
            view = await _alert_view(ctx, insight)
            if slack_url and insight.slack_alerted_at is None:
                resp = await http.post(slack_url, json=build_slack_message(view))
                if resp.status_code // 100 == 2:
                    async with ctx.sessions() as session:
                        await session.execute(
                            update(Insight).where(Insight.id == insight.id).values(slack_alerted_at=now)
                        )
                        await session.commit()
                    slack_sent += 1
                else:  # leave slack_alerted_at empty so the next run retries
                    run.record_error(f"slack returned {resp.status_code} for insight {insight.id}")
            for sub in subscriptions:
                if not _matches(sub.filters or {}, view):
                    continue
                async with ctx.sessions() as session:
                    delivery_id = (
                        await session.execute(
                            insert(WebhookDelivery)
                            .values(insight_id=insight.id, subscription_id=sub.id)
                            .on_conflict_do_nothing(index_elements=["insight_id", "subscription_id"])
                            .returning(WebhookDelivery.id)
                        )
                    ).scalar_one_or_none()
                    if delivery_id is not None:
                        await queue.enqueue(
                            session, "deliver_webhook", {"delivery_id": delivery_id}, max_attempts=WEBHOOK_MAX_ATTEMPTS
                        )
                        queued += 1
                    await session.commit()
    run.items_out = slack_sent + queued
    run.details.update(slack_sent=slack_sent, slack_enabled=bool(slack_url), webhooks_queued=queued)


def webhook_body(view: AlertInsight, delivery_id: int) -> bytes:
    return json.dumps(
        {
            "event": "insight.important",
            "delivery_id": delivery_id,
            "insight": {
                "id": view.insight_id,
                "competitor": view.competitor,
                "category": view.category,
                "importance": view.importance,
                "headline": view.headline,
                "summary": view.summary,
                "why_it_matters": view.why_it_matters,
                "sources": [{"title": t, "url": u} for t, u in view.sources],
            },
        },
        sort_keys=True,
    ).encode()


async def deliver_webhook(
    ctx: IngestContext, payload: dict[str, Any], run: RunStats, http: httpx.AsyncClient | None = None
) -> None:
    """POST one signed delivery. Raising makes the queue retry it with exponential backoff."""
    delivery_id = int(payload["delivery_id"])
    async with ctx.sessions() as session:
        delivery = await session.get(WebhookDelivery, delivery_id)
        if delivery is None or delivery.status == DeliveryStatus.delivered:
            return
        sub = await session.get(WebhookSubscription, delivery.subscription_id)
        insight = await session.get(Insight, delivery.insight_id)
    if sub is None or insight is None:
        return
    body = webhook_body(await _alert_view(ctx, insight), delivery_id)
    headers = {
        "Content-Type": "application/json",
        SIGNATURE_HEADER: sign(sub.secret, body),
        IDEMPOTENCY_HEADER: f"delivery-{delivery_id}",
    }
    run.items_in = 1
    client = http or httpx.AsyncClient(timeout=HTTP_TIMEOUT_S)
    try:
        status_code: int | None = None
        error: str | None = None
        try:
            resp = await client.post(sub.url, content=body, headers=headers)
            status_code = resp.status_code
        except httpx.HTTPError as exc:
            error = f"{type(exc).__name__}: {exc}"
        ok = status_code is not None and status_code // 100 == 2
        attempts = delivery.attempts + 1
        final_status = (
            DeliveryStatus.delivered if ok else DeliveryStatus.failed if attempts >= WEBHOOK_MAX_ATTEMPTS else None
        )
        async with ctx.sessions() as session:
            values: dict[str, Any] = {
                "attempts": attempts,
                "last_status_code": status_code,
                "last_error": None if ok else (error or f"HTTP {status_code}"),
            }
            if final_status is not None:
                values["status"] = final_status
            if ok:
                values["delivered_at"] = datetime.now(UTC)
            await session.execute(update(WebhookDelivery).where(WebhookDelivery.id == delivery_id).values(**values))
            await session.commit()
        if ok:
            run.items_out = 1
        elif final_status is None:
            raise RuntimeError(f"webhook delivery {delivery_id} failed: {error or f'HTTP {status_code}'}")
    finally:
        if http is None:
            await client.aclose()
