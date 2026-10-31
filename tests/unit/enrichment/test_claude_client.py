"""ClaudeClient against recorded Messages API responses (no network)."""

from __future__ import annotations

import anthropic
import pytest

from signal_scout.enrichment.claude import TOOL_NAME, ClaudeClient
from signal_scout.enrichment.errors import LlmOutputError, LlmQuotaExhaustedError, LlmRequestError, LlmTransientError
from signal_scout.enrichment.models import SignalInput

from .conftest import COMPETITORS, FakeAnthropic, anthropic_error


def make(fake: FakeAnthropic) -> ClaudeClient:
    return ClaudeClient(COMPETITORS, client=fake)  # type: ignore[arg-type]


async def test_returns_validated_enrichment(signal: SignalInput) -> None:
    result = await make(FakeAnthropic("claude_ok.json")).enrich(signal)

    e = result.enrichment
    assert e.category == "outage_or_incident"
    assert e.competitors == ["Asana"]
    assert e.importance == 4
    assert result.attempts == 1
    assert result.prompt_version == "v3"  # current default


async def test_request_uses_forced_strict_tool_and_cached_system_prompt(signal: SignalInput) -> None:
    fake = FakeAnthropic("claude_ok.json")
    await make(fake).enrich(signal)

    req = fake.requests[0]
    assert req["model"] == "claude-haiku-5-5"
    assert req["tool_choice"] == {"type": "tool", "name": TOOL_NAME}
    (tool,) = req["tools"]
    assert tool["strict"] is True
    assert tool["input_schema"]["additionalProperties"] is False
    assert tool["input_schema"]["properties"]["competitors"]["items"]["enum"] == COMPETITORS
    (system_block,) = req["system"]
    assert system_block["cache_control"] == {"type": "ephemeral"}
    assert "Asana, ClickUp, Notion" in system_block["text"]
    # Volatile per-signal content goes in the user turn, after the cached prefix.
    assert signal.title in req["messages"][0]["content"]
    assert signal.title not in system_block["text"]


async def test_usage_and_cost_from_cache_read(signal: SignalInput) -> None:
    usage = (await make(FakeAnthropic("claude_ok.json")).enrich(signal)).usage

    assert (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens) == (212, 128, 1630)
    # 212 * $0.10 + 128 * $0.50 + 1630 * $0.01 per MTok
    assert usage.cost_usd == pytest.approx((212 * 0.10 + 128 * 0.50 + 1630 * 0.01) / 1e6)
    assert usage.provider == "claude"


async def test_cache_write_is_priced_higher_than_cache_read(signal: SignalInput) -> None:
    cold = (await make(FakeAnthropic("claude_ok_cache_write.json")).enrich(signal)).usage
    warm = (await make(FakeAnthropic("claude_ok.json")).enrich(signal)).usage
    assert cold.cache_write_tokens == 1630
    assert cold.cost_usd > warm.cost_usd


@pytest.mark.parametrize(
    "bad", ["claude_bad_range.json", "claude_untracked_competitor.json", "claude_no_tool_use.json"]
)
async def test_retries_once_after_invalid_output_then_succeeds(signal: SignalInput, bad: str) -> None:
    fake = FakeAnthropic(bad, "claude_ok.json")
    result = await make(fake).enrich(signal)

    assert result.attempts == 2
    assert len(fake.requests) == 2
    assert result.usage.output_tokens == 128 * 2  # both calls are billed and counted


async def test_gives_up_after_two_invalid_outputs(signal: SignalInput) -> None:
    fake = FakeAnthropic("claude_bad_range.json", "claude_untracked_competitor.json")
    with pytest.raises(LlmOutputError, match="claude output invalid after 2 attempts"):
        await make(fake).enrich(signal)


async def test_rate_limit_waits_for_retry_after_then_retries(signal: SignalInput, no_sleep: list[float]) -> None:
    fake = FakeAnthropic(anthropic_error(anthropic.RateLimitError, 429, {"retry-after": "3"}), "claude_ok.json")
    result = await make(fake).enrich(signal)

    assert result.enrichment.importance == 4
    assert len(no_sleep) == 1 and 3 <= no_sleep[0] <= 4  # retry-after plus up to 1s jitter


async def test_rate_limit_gives_up_after_max_retries(signal: SignalInput, no_sleep: list[float]) -> None:
    errors = [anthropic_error(anthropic.RateLimitError, 429) for _ in range(3)]
    client = ClaudeClient(COMPETITORS, client=FakeAnthropic(*errors), max_rate_limit_retries=2)  # type: ignore[arg-type]
    with pytest.raises(Exception, match="429"):
        await client.enrich(signal)
    assert len(no_sleep) == 2


async def test_permission_denied_means_out_of_credit(signal: SignalInput) -> None:
    fake = FakeAnthropic(anthropic_error(anthropic.PermissionDeniedError, 403))
    with pytest.raises(LlmQuotaExhaustedError):
        await make(fake).enrich(signal)


async def test_model_is_configurable(signal: SignalInput) -> None:
    fake = FakeAnthropic("claude_ok.json")
    client = ClaudeClient(COMPETITORS, model="claude-sonnet-5-5", client=fake)  # type: ignore[arg-type]
    result = await client.enrich(signal)
    assert fake.requests[0]["model"] == "claude-sonnet-5-5"
    assert result.usage.model == "claude-sonnet-5-5"


async def test_server_error_backs_off_and_retries(signal: SignalInput, no_sleep: list[float]) -> None:
    fake = FakeAnthropic(anthropic_error(anthropic.InternalServerError, 529), "claude_ok.json")
    result = await make(fake).enrich(signal)
    assert result.enrichment.importance == 4
    assert len(no_sleep) == 1


async def test_persistent_server_error_surfaces_as_transient(signal: SignalInput, no_sleep: list[float]) -> None:
    errors = [anthropic_error(anthropic.InternalServerError, 500) for _ in range(3)]
    client = ClaudeClient(COMPETITORS, client=FakeAnthropic(*errors), max_rate_limit_retries=2)  # type: ignore[arg-type]
    with pytest.raises(LlmTransientError):
        await client.enrich(signal)


async def test_bad_request_fails_without_retry(signal: SignalInput, no_sleep: list[float]) -> None:
    fake = FakeAnthropic(anthropic_error(anthropic.BadRequestError, 400), "claude_ok.json")
    with pytest.raises(LlmRequestError, match="400"):
        await make(fake).enrich(signal)
    assert no_sleep == [] and len(fake.requests) == 1
