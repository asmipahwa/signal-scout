"""CostGuard: refuse LLM calls once paid spend reaches the daily or total cap.

Spend is computed from the `llm_calls` table (one row per call, cost from the provider's reported
token usage and the pricing table), so the caps survive restarts and count every process. On the
Gemini free tier every call costs $0 and the guard never trips; it exists for paid providers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import LlmCall
from ..enrichment.errors import LlmQuotaExhaustedError


class BudgetExceededError(LlmQuotaExhaustedError):
    """Paid LLM spend reached a cap. Signals wait (`deferred`) until the cap allows more."""


async def spend_usd(session: AsyncSession, since: datetime | None = None) -> Decimal:
    stmt = select(func.coalesce(func.sum(LlmCall.cost_usd), 0))
    if since is not None:
        stmt = stmt.where(LlmCall.created_at >= since)
    return Decimal(str((await session.execute(stmt)).scalar_one()))


async def check_budget(
    session: AsyncSession, daily_cap_usd: float, total_cap_usd: float, now: datetime | None = None
) -> None:
    """Raise BudgetExceededError if today's (UTC) or all-time paid spend has reached its cap."""
    current = now or datetime.now(UTC)
    day_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    today = await spend_usd(session, since=day_start)
    if today >= Decimal(str(daily_cap_usd)):
        raise BudgetExceededError(f"daily LLM spend ${today:.4f} reached the ${daily_cap_usd:.2f} cap")
    total = await spend_usd(session)
    if total >= Decimal(str(total_cap_usd)):
        raise BudgetExceededError(f"total LLM spend ${total:.4f} reached the ${total_cap_usd:.2f} cap")


def next_utc_midnight(now: datetime | None = None) -> datetime:
    current = now or datetime.now(UTC)
    return (current + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
