"""The polite HTTP client, against respx-mocked responses (no network)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from signal_scout.config import USER_AGENT
from signal_scout.ingest.http import FetchError, PoliteHttpClient, RetryPolicy
from signal_scout.ingest.ratelimit import DomainRateLimiter

URL = "https://feeds.example.com/rss.xml"


class Recorder:
    def __init__(self) -> None:
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
async def client(recorder: Recorder) -> AsyncIterator[PoliteHttpClient]:
    http = PoliteHttpClient.build_client(USER_AGENT, timeout_s=5)
    limiter = DomainRateLimiter(rate=1000, capacity=1000, sleep=recorder.sleep)
    yield PoliteHttpClient(
        http, limiter, RetryPolicy(max_retries=2, base_s=1, cap_s=10), sleep=recorder.sleep, rng=lambda: 1.0
    )
    await http.aclose()


@respx.mock
async def test_sends_user_agent_and_conditional_headers(client: PoliteHttpClient) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, content=b"<rss/>", headers={"ETag": '"v2"'}))
    result = await client.get(URL, etag='"v1"', last_modified="Wed, 21 Oct 2026 07:28:00 GMT")
    sent = route.calls.last.request
    assert sent.headers["User-Agent"] == USER_AGENT
    assert sent.headers["If-None-Match"] == '"v1"'
    assert sent.headers["If-Modified-Since"] == "Wed, 21 Oct 2026 07:28:00 GMT"
    assert (result.status, result.body, result.etag, result.attempts) == (200, b"<rss/>", '"v2"', 1)


@respx.mock
async def test_not_modified(client: PoliteHttpClient) -> None:
    respx.get(URL).mock(return_value=httpx.Response(304))
    result = await client.get(URL, etag='"v1"')
    assert result.not_modified and result.body == b""


@respx.mock
async def test_retries_5xx_with_exponential_backoff(client: PoliteHttpClient, recorder: Recorder) -> None:
    respx.get(URL).mock(side_effect=[httpx.Response(503), httpx.Response(502), httpx.Response(200, content=b"ok")])
    result = await client.get(URL)
    assert result.attempts == 3
    assert recorder.sleeps == [1.0, 2.0]  # rng=1.0 -> the full (un-jittered) delay


@respx.mock
async def test_honours_retry_after(client: PoliteHttpClient, recorder: Recorder) -> None:
    respx.get(URL).mock(side_effect=[httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200)])
    await client.get(URL)
    assert recorder.sleeps == [7.0]


@respx.mock
async def test_gives_up_after_max_retries(client: PoliteHttpClient, recorder: Recorder) -> None:
    respx.get(URL).mock(return_value=httpx.Response(500))
    with pytest.raises(FetchError) as info:
        await client.get(URL)
    assert info.value.status == 500
    assert len(recorder.sleeps) == 2


@respx.mock
async def test_does_not_retry_client_errors(client: PoliteHttpClient, recorder: Recorder) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(404))
    with pytest.raises(FetchError, match="HTTP 404"):
        await client.get(URL)
    assert route.call_count == 1 and recorder.sleeps == []


@respx.mock
async def test_retries_network_errors(client: PoliteHttpClient) -> None:
    respx.get(URL).mock(side_effect=[httpx.ConnectTimeout("slow"), httpx.Response(200, content=b"ok")])
    assert (await client.get(URL)).attempts == 2


def test_full_jitter_stays_within_the_cap() -> None:
    policy = RetryPolicy(base_s=1, cap_s=10)
    assert policy.delay(0, lambda: 0.5) == 0.5
    assert policy.delay(20, lambda: 0.999) < 10
