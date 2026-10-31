"""Per-domain token buckets, so we never hit one host faster than a polite rate.

Several sources share a host (all Bing News queries, all HN queries); limiting per *domain* rather
than per source keeps the total request rate to each host bounded. Clock and sleep are injectable
so tests run instantly.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable


class TokenBucket:
    """Classic token bucket: `rate` tokens per second refill, up to `capacity`; each request costs one."""

    def __init__(
        self,
        rate: float,
        capacity: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate <= 0 or capacity < 1:
            raise ValueError("rate must be > 0 and capacity >= 1")
        self.rate = rate
        self.capacity = capacity
        self._clock = clock
        self._sleep = sleep
        self._tokens = capacity
        self._updated = clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
        self._updated = now

    def try_acquire(self) -> bool:
        """Take a token if one is available right now."""
        self._refill()
        if self._tokens >= 1:
            self._tokens -= 1
            return True
        return False

    def wait_time(self) -> float:
        """Seconds until the next token is available (0 if one is available now)."""
        self._refill()
        return max(0.0, (1 - self._tokens) / self.rate)

    async def acquire(self) -> None:
        """Wait until a token is available, then take it."""
        async with self._lock:
            while not self.try_acquire():
                await self._sleep(self.wait_time())


class DomainRateLimiter:
    """One TokenBucket per domain, created on first use."""

    def __init__(
        self,
        rate: float,
        capacity: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._rate, self._capacity, self._clock, self._sleep = rate, capacity, clock, sleep
        self._buckets: dict[str, TokenBucket] = {}

    def bucket(self, domain: str) -> TokenBucket:
        if domain not in self._buckets:
            self._buckets[domain] = TokenBucket(self._rate, self._capacity, clock=self._clock, sleep=self._sleep)
        return self._buckets[domain]

    async def acquire(self, domain: str) -> None:
        await self.bucket(domain).acquire()
