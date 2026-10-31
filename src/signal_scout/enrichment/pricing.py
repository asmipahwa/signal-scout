"""USD prices per million tokens, used by the CostGuard.

Prices change; each entry says where and when it was checked. A model missing from this table is
priced at the most expensive known rate rather than zero, so a typo can never make spend invisible.
Gemini on the free tier costs nothing, but we still record tokens so the paid-tier equivalent is
visible if we ever need to switch.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import LlmUsage


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_write: float
    cache_read: float


PAID_PRICES: dict[str, Price] = {
    # platform.claude.com pricing, checked 2026-10-28 (prompts <= 100K tokens).
    "claude-haiku-5-5": Price(input=0.10, output=0.50, cache_write=0.125, cache_read=0.01),
    # ai.google.dev/gemini-api/docs/pricing, checked 2026-10-30 (paid tier, rates through 2026-12-31).
    "gemini-3.8-flash": Price(input=0.75, output=3.75, cache_write=0.75, cache_read=0.075),
    "gemini-3.5-flash-lite": Price(input=0.30, output=2.50, cache_write=0.30, cache_read=0.03),
}
_FALLBACK = Price(input=5.0, output=25.0, cache_write=6.25, cache_read=0.5)


def paid_cost_usd(usage: LlmUsage) -> float:
    """What this call costs (or would cost) on the paid tier. Thinking tokens bill as output."""
    p = PAID_PRICES.get(usage.model, _FALLBACK)
    return (
        usage.input_tokens * p.input
        + (usage.output_tokens + usage.thinking_tokens) * p.output
        + usage.cache_write_tokens * p.cache_write
        + usage.cache_read_tokens * p.cache_read
    ) / 1_000_000
