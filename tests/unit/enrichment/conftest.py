"""Fake SDK clients that replay recorded responses, one per adapter.

Each fake implements only the single SDK method its adapter calls, records the request kwargs, and
returns (or raises) the next queued item. The adapters run their real code against these.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest
from anthropic.types import Message
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from signal_scout.enrichment.models import SignalInput

FIXTURES = Path(__file__).parent / "fixtures"
COMPETITORS = ["Asana", "ClickUp", "Notion"]


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


class _Replay:
    def __init__(self, items: list[Any]) -> None:
        self.items = list(items)
        self.requests: list[dict[str, Any]] = []

    def next(self, kwargs: dict[str, Any]) -> Any:
        self.requests.append(kwargs)
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeAnthropic:
    """Stands in for anthropic.AsyncAnthropic: only `.messages.create` exists."""

    def __init__(self, *items: str | Exception) -> None:
        self._replay = _Replay([Message.model_validate(load(i)) if isinstance(i, str) else i for i in items])
        self.messages = self

    @property
    def requests(self) -> list[dict[str, Any]]:
        return self._replay.requests

    async def create(self, **kwargs: Any) -> Message:
        return self._replay.next(kwargs)  # type: ignore[no-any-return]


class FakeGenai:
    """Stands in for google.genai.Client: only `.aio.models.generate_content` exists."""

    def __init__(self, *items: str | Exception) -> None:
        self._replay = _Replay(
            [genai_types.GenerateContentResponse.model_validate(load(i)) if isinstance(i, str) else i for i in items]
        )
        self.aio = self
        self.models = self

    @property
    def requests(self) -> list[dict[str, Any]]:
        return self._replay.requests

    async def generate_content(self, **kwargs: Any) -> genai_types.GenerateContentResponse:
        return self._replay.next(kwargs)  # type: ignore[no-any-return]


def anthropic_error(
    cls: type[anthropic.APIStatusError], status: int, headers: dict[str, str] | None = None
) -> Exception:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls(f"HTTP {status}", response=httpx2.Response(status, headers=headers or {}, request=request), body=None)


def gemini_error(fixture: str) -> Exception:
    body = load(fixture)
    return genai_errors.ClientError(body["error"]["code"], body)


@pytest.fixture
def signal() -> SignalInput:
    # A real Bing News item (source, title, URL, snippet) fetched 2026-10-28.
    return SignalInput(
        source_type="news",
        source="bing:Hindustan Times on MSN",
        title="Asana, popular work management platform, down for thousands of users; what we know so far",
        url="https://www.msn.com/en-in/news/other/asana-popular-work-management-platform-down-for-thousands-of-users-what-we-know-so-far/ar-AA2fL04U",  # noqa: E501
        published_at=datetime(2026, 10, 21, 17, 18, tzinfo=UTC),
        text="Work-management platform Asana is experiencing an outage, with nearly 2,300 U.S. users reporting problems on Down Detector.",  # noqa: E501
        matched_competitors=["Asana"],
    )


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record backoff sleeps instead of waiting."""
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("signal_scout.enrichment.client.asyncio.sleep", fake_sleep)
    return slept
