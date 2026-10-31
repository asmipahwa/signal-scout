"""A polite HTTP client for polling feeds.

Wraps httpx with what a well-behaved crawler needs: an identifying User-Agent, conditional requests
(ETag / If-Modified-Since, so unchanged feeds cost the publisher a 304), a per-domain token bucket,
timeouts, a response size cap, and retries with exponential backoff and full jitter on network
errors, 429 and 5xx (honouring Retry-After). Other 4xx responses are not retried: they will not fix
themselves, and the circuit breaker handles sources that keep failing.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from ..log import get_logger
from .canonical import domain_of
from .ratelimit import DomainRateLimiter

log = get_logger(__name__)

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
MAX_BODY_BYTES = 10 * 1024 * 1024
MAX_RETRY_AFTER_S = 120.0


class FetchError(Exception):
    """A fetch that failed for good (after retries, or with a non-retryable status)."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True)
class FetchResult:
    """A completed fetch. `not_modified` means 304: `body` is empty and cached headers still apply."""

    url: str
    status: int
    body: bytes
    etag: str | None
    last_modified: str | None
    content_type: str | None
    attempts: int

    @property
    def not_modified(self) -> bool:
        return self.status == 304


@dataclass(frozen=True)
class RetryPolicy:
    """Retry `max_retries` times after the first try; delay ~ U(0, min(cap, base * 2^n)) (full jitter)."""

    max_retries: int = 3
    base_s: float = 1.0
    cap_s: float = 30.0

    def delay(self, retry_number: int, rng: Callable[[], float] = random.random) -> float:
        return float(rng()) * min(self.cap_s, self.base_s * 2.0**retry_number)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if value and value.strip().isdigit():
        return min(MAX_RETRY_AFTER_S, float(value))
    return None


class PoliteHttpClient:
    """GET with conditional headers, per-domain rate limiting and retries. One instance per process."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        limiter: DomainRateLimiter,
        retry: RetryPolicy | None = None,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._client = client
        self._limiter = limiter
        self._retry = retry or RetryPolicy()
        self._sleep = sleep
        self._rng = rng

    @staticmethod
    def build_client(user_agent: str, timeout_s: float) -> httpx.AsyncClient:
        timeout = httpx.Timeout(timeout_s, connect=min(10.0, timeout_s))
        return httpx.AsyncClient(
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=timeout,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

    async def get(self, url: str, *, etag: str | None = None, last_modified: str | None = None) -> FetchResult:
        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        domain = domain_of(url)
        attempts = 0
        while True:
            attempts += 1
            await self._limiter.acquire(domain)
            wait: float | None = None
            try:
                response = await self._client.get(url, headers=headers)
            except httpx.TransportError as exc:  # timeouts, DNS, connection resets
                error = FetchError(f"{type(exc).__name__} fetching {url}", retryable=True)
            else:
                if response.status_code in (200, 304):
                    return self._result(url, response, attempts)
                retryable = response.status_code in RETRYABLE_STATUS
                error = FetchError(
                    f"HTTP {response.status_code} from {url}", status=response.status_code, retryable=retryable
                )
                wait = _retry_after_seconds(response)
            if not error.retryable or attempts > self._retry.max_retries:
                raise error
            delay = wait if wait is not None else self._retry.delay(attempts - 1, self._rng)
            log.warning("http_retry", url=url, attempt=attempts, delay_s=round(delay, 2), error=str(error))
            await self._sleep(delay)

    @staticmethod
    def _result(url: str, response: httpx.Response, attempts: int) -> FetchResult:
        body = response.content if response.status_code == 200 else b""
        if len(body) > MAX_BODY_BYTES:
            raise FetchError(f"response from {url} is {len(body)} bytes (limit {MAX_BODY_BYTES})")
        return FetchResult(
            url=str(response.url),
            status=response.status_code,
            body=body,
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
            content_type=response.headers.get("content-type"),
            attempts=attempts,
        )
