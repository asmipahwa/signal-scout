"""Outbound webhook subscriptions (CRUD) and their delivery log.

The signing secret is shown exactly once, in the create response, like an API token: listing or
fetching a subscription never returns it, so a leaked read-only view cannot forge our signatures.
"""

from __future__ import annotations

import secrets

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import WebhookDelivery, WebhookSubscription
from .errors import NotFoundError
from .schemas import DeliveryOut, WebhookCreate, WebhookCreated, WebhookOut

MAX_LIMIT = 200


def _out(sub: WebhookSubscription) -> WebhookOut:
    return WebhookOut(
        id=sub.id, name=sub.name, url=sub.url, filters=dict(sub.filters), enabled=sub.enabled, created_at=sub.created_at
    )


async def create_webhook(session: AsyncSession, data: WebhookCreate) -> WebhookCreated:
    """Create a subscription; generates a 256-bit secret unless one is given."""
    secret = data.secret or secrets.token_hex(32)
    sub = WebhookSubscription(
        name=data.name, url=str(data.url), filters=data.filters, enabled=data.enabled, secret=secret
    )
    session.add(sub)
    await session.commit()
    await session.refresh(sub)
    return WebhookCreated(**_out(sub).model_dump(), secret=secret)


async def list_webhooks(session: AsyncSession) -> list[WebhookOut]:
    rows = (await session.execute(select(WebhookSubscription).order_by(WebhookSubscription.id))).scalars()
    return [_out(s) for s in rows]


async def get_webhook(session: AsyncSession, webhook_id: int) -> WebhookOut:
    sub = await session.get(WebhookSubscription, webhook_id)
    if sub is None:
        raise NotFoundError(f"webhook {webhook_id} not found")
    return _out(sub)


async def delete_webhook(session: AsyncSession, webhook_id: int) -> None:
    """Delete a subscription and (by cascade) its delivery log."""
    deleted = (
        await session.execute(
            delete(WebhookSubscription).where(WebhookSubscription.id == webhook_id).returning(WebhookSubscription.id)
        )
    ).scalar_one_or_none()
    if deleted is None:
        raise NotFoundError(f"webhook {webhook_id} not found")
    await session.commit()


async def list_deliveries(session: AsyncSession, webhook_id: int, limit: int = 50) -> list[DeliveryOut]:
    """Newest deliveries first for one subscription."""
    await get_webhook(session, webhook_id)
    rows = (
        await session.execute(
            select(WebhookDelivery)
            .where(WebhookDelivery.subscription_id == webhook_id)
            .order_by(WebhookDelivery.created_at.desc(), WebhookDelivery.id.desc())
            .limit(max(1, min(limit, MAX_LIMIT)))
        )
    ).scalars()
    return [
        DeliveryOut(
            id=d.id,
            insight_id=d.insight_id,
            subscription_id=d.subscription_id,
            status=d.status.value,
            attempts=d.attempts,
            last_status_code=d.last_status_code,
            last_error=d.last_error,
            delivered_at=d.delivered_at,
            created_at=d.created_at,
        )
        for d in rows
    ]
