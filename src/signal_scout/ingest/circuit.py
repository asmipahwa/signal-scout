"""Circuit breaker for sources: stop polling a source that keeps failing.

A feed that moved or started blocking us would otherwise be retried forever, wasting requests and
filling logs. After `threshold` consecutive failed polls the source is disabled with a reason; a
human re-enables it after fixing the URL. Any successful poll (including 304 Not Modified) resets
the count. The logic is a pure function so it is trivial to test; `fetch_source` applies it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BreakerDecision:
    """The source state after one poll."""

    consecutive_failures: int
    enabled: bool
    tripped: bool  # True only on the poll that disabled the source
    disabled_reason: str | None


def after_poll(
    consecutive_failures: int, *, succeeded: bool, threshold: int, error: str | None = None
) -> BreakerDecision:
    """Return the new breaker state given the previous failure count and this poll's outcome."""
    if succeeded:
        return BreakerDecision(consecutive_failures=0, enabled=True, tripped=False, disabled_reason=None)
    failures = consecutive_failures + 1
    if failures >= threshold:
        reason = f"circuit breaker: {failures} consecutive failures; last error: {error or 'unknown'}"
        return BreakerDecision(failures, enabled=False, tripped=True, disabled_reason=reason[:1000])
    return BreakerDecision(failures, enabled=True, tripped=False, disabled_reason=None)
