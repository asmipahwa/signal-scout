"""Pure reliability logic: queue backoff math, the source circuit breaker, and token buckets."""

from __future__ import annotations

import random

import pytest
from hypothesis import given
from hypothesis import strategies as st

from signal_scout.ingest.circuit import after_poll
from signal_scout.ingest.ratelimit import DomainRateLimiter, TokenBucket
from signal_scout.queue import BACKOFF_CAP_S, backoff_seconds

# --- queue backoff -----------------------------------------------------------------------------


def test_backoff_doubles_from_the_base() -> None:
    assert [backoff_seconds(n, base=30) for n in (1, 2, 3, 4)] == [30, 60, 120, 240]


def test_backoff_is_capped() -> None:
    assert backoff_seconds(50) == BACKOFF_CAP_S
    assert backoff_seconds(10, base=30, cap=100) == 100


def test_backoff_rejects_zero_attempts() -> None:
    with pytest.raises(ValueError):
        backoff_seconds(0)


@given(st.integers(min_value=1, max_value=40), st.floats(min_value=0, max_value=1))
def test_jitter_only_shortens_the_delay(attempts: int, jitter: float) -> None:
    full = backoff_seconds(attempts)
    jittered = backoff_seconds(attempts, jitter=jitter, rng=random.Random(attempts))
    assert full * (1 - jitter) <= jittered <= full


# --- circuit breaker ---------------------------------------------------------------------------


def test_breaker_trips_on_the_threshold_failure_only() -> None:
    failures = 0
    decisions = []
    for _ in range(3):
        d = after_poll(failures, succeeded=False, threshold=3, error="HTTP 500")
        failures = d.consecutive_failures
        decisions.append(d)
    assert [d.enabled for d in decisions] == [True, True, False]
    assert [d.tripped for d in decisions] == [False, False, True]
    assert decisions[-1].disabled_reason is not None
    assert "3 consecutive failures" in decisions[-1].disabled_reason
    assert "HTTP 500" in decisions[-1].disabled_reason


def test_success_resets_the_count() -> None:
    d = after_poll(4, succeeded=True, threshold=5)
    assert (d.consecutive_failures, d.enabled, d.tripped) == (0, True, False)


# --- token bucket ------------------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_bucket_allows_a_burst_then_refills_at_the_rate() -> None:
    clock = FakeClock()
    bucket = TokenBucket(rate=0.5, capacity=2, clock=clock, sleep=clock.sleep)
    assert bucket.try_acquire() and bucket.try_acquire()
    assert not bucket.try_acquire()
    assert bucket.wait_time() == pytest.approx(2.0)
    clock.now += 2.0
    assert bucket.try_acquire()


async def test_acquire_waits_exactly_long_enough() -> None:
    clock = FakeClock()
    bucket = TokenBucket(rate=0.5, capacity=1, clock=clock, sleep=clock.sleep)
    for _ in range(3):
        await bucket.acquire()
    assert clock.sleeps == [pytest.approx(2.0), pytest.approx(2.0)]
    assert clock.now == pytest.approx(4.0)


async def test_buckets_are_per_domain() -> None:
    clock = FakeClock()
    limiter = DomainRateLimiter(rate=1, capacity=1, clock=clock, sleep=clock.sleep)
    await limiter.acquire("bing.com")
    await limiter.acquire("hn.algolia.com")
    assert clock.sleeps == []
    await limiter.acquire("bing.com")
    assert clock.sleeps == [pytest.approx(1.0)]


def test_bucket_rejects_bad_parameters() -> None:
    with pytest.raises(ValueError):
        TokenBucket(rate=0, capacity=1)
