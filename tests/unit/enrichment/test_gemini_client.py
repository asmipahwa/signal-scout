"""GeminiClient against recorded generateContent responses (no network)."""

from __future__ import annotations

import pytest
from google.genai import types

from signal_scout.enrichment.errors import LlmOutputError, LlmQuotaExhaustedError, LlmRequestError, LlmTransientError
from signal_scout.enrichment.gemini import GeminiClient
from signal_scout.enrichment.models import SignalInput
from signal_scout.enrichment.pricing import paid_cost_usd

from .conftest import COMPETITORS, FakeGenai, gemini_error


def make(fake: FakeGenai, **kwargs: object) -> GeminiClient:
    return GeminiClient(COMPETITORS, client=fake, **kwargs)  # type: ignore[arg-type]


async def test_returns_validated_enrichment(signal: SignalInput) -> None:
    result = await make(FakeGenai("gemini_ok.json")).enrich(signal)

    assert result.enrichment.category == "outage_or_incident"
    assert result.enrichment.competitors == ["Asana"]
    assert result.attempts == 1


async def test_request_constrains_output_to_shared_schema(signal: SignalInput) -> None:
    fake = FakeGenai("gemini_ok.json")
    await make(fake).enrich(signal)

    req = fake.requests[0]
    assert req["model"] == "gemini-3.5-flash-lite"
    config: types.GenerateContentConfig = req["config"]
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema["properties"]["competitors"]["items"]["enum"] == COMPETITORS
    assert "Asana, ClickUp, Notion" in str(config.system_instruction)
    assert signal.title in req["contents"]


async def test_usage_splits_cached_tokens_and_free_tier_costs_nothing(signal: SignalInput) -> None:
    usage = (await make(FakeGenai("gemini_ok.json")).enrich(signal)).usage

    # prompt_token_count (1850) includes the 1600 cached tokens.
    assert (usage.input_tokens, usage.cache_read_tokens) == (250, 1600)
    assert (usage.output_tokens, usage.thinking_tokens) == (140, 60)
    assert usage.cost_usd == 0.0


async def test_paid_tier_is_priced(signal: SignalInput) -> None:
    usage = (await make(FakeGenai("gemini_ok.json"), free_tier=False).enrich(signal)).usage
    assert usage.cost_usd == pytest.approx(paid_cost_usd(usage))
    assert usage.cost_usd > 0


@pytest.mark.parametrize("bad", ["gemini_truncated_json.json", "gemini_bad_range.json"])
async def test_retries_once_after_invalid_output(signal: SignalInput, bad: str) -> None:
    fake = FakeGenai(bad, "gemini_ok.json")
    result = await make(fake).enrich(signal)
    assert result.attempts == 2
    assert len(fake.requests) == 2


async def test_gives_up_after_two_invalid_outputs(signal: SignalInput) -> None:
    with pytest.raises(LlmOutputError):
        await make(FakeGenai("gemini_truncated_json.json", "gemini_bad_range.json")).enrich(signal)


async def test_per_minute_429_waits_for_retry_delay(signal: SignalInput, no_sleep: list[float]) -> None:
    fake = FakeGenai(gemini_error("gemini_429_per_minute.json"), "gemini_ok.json")
    result = await make(fake).enrich(signal)

    assert result.enrichment.importance == 4
    assert len(no_sleep) == 1 and 17 <= no_sleep[0] <= 18


async def test_per_day_429_stops_without_retrying(signal: SignalInput, no_sleep: list[float]) -> None:
    fake = FakeGenai(gemini_error("gemini_429_per_day.json"), "gemini_ok.json")
    with pytest.raises(LlmQuotaExhaustedError, match="daily quota"):
        await make(fake).enrich(signal)
    assert no_sleep == []
    assert len(fake.requests) == 1


async def test_overloaded_model_503_backs_off_and_retries(signal: SignalInput, no_sleep: list[float]) -> None:
    fake = FakeGenai(gemini_error("gemini_503_overloaded.json"), "gemini_ok.json")
    result = await make(fake).enrich(signal)
    assert result.enrichment.importance == 4
    assert len(no_sleep) == 1


async def test_persistent_503_surfaces_as_transient(signal: SignalInput, no_sleep: list[float]) -> None:
    fake = FakeGenai(*[gemini_error("gemini_503_overloaded.json") for _ in range(3)])
    with pytest.raises(LlmTransientError, match="high demand"):
        await make(fake, max_rate_limit_retries=2).enrich(signal)


async def test_bad_request_fails_without_retry(signal: SignalInput, no_sleep: list[float]) -> None:
    fake = FakeGenai(gemini_error("gemini_400_bad_request.json"), "gemini_ok.json")
    with pytest.raises(LlmRequestError, match="400"):
        await make(fake).enrich(signal)
    assert no_sleep == [] and len(fake.requests) == 1
