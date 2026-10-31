"""Enrichment validation, provider selection from env vars, the rate limiter, and the fake client."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from signal_scout.enrichment import LLMClient
from signal_scout.enrichment.claude import ClaudeClient
from signal_scout.enrichment.errors import LlmOutputError, LlmQuotaExhaustedError
from signal_scout.enrichment.factory import llm_client_from_env
from signal_scout.enrichment.fake import FakeLLMClient
from signal_scout.enrichment.gemini import GeminiClient
from signal_scout.enrichment.models import Enrichment, SignalInput, enrichment_json_schema
from signal_scout.enrichment.rate_limit import RateLimiter

from .conftest import COMPETITORS, load

VALID: dict[str, Any] = load("claude_ok.json")["content"][0]["input"]

# --- Enrichment validation ------------------------------------------------------------------


def test_valid_enrichment_passes() -> None:
    assert Enrichment.model_validate(VALID, context={"competitors": COMPETITORS}).importance == 4


@pytest.mark.parametrize(
    "field,value",
    [("sentiment", 1.5), ("confidence", -0.1), ("importance", 0), ("category", "rumor"), ("summary", "short")],
)
def test_out_of_range_values_are_rejected(field: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        Enrichment.model_validate({**VALID, field: value}, context={"competitors": COMPETITORS})


def test_untracked_competitor_rejected_and_duplicates_collapsed() -> None:
    with pytest.raises(ValidationError, match="untracked"):
        Enrichment.model_validate({**VALID, "competitors": ["Trello"]}, context={"competitors": COMPETITORS})
    e = Enrichment.model_validate(
        {**VALID, "competitors": ["Notion", "Asana", "Asana"]}, context={"competitors": COMPETITORS}
    )
    assert e.competitors == ["Asana", "Notion"]


def test_schema_requires_exactly_the_enrichment_fields() -> None:
    schema = enrichment_json_schema(COMPETITORS)
    assert set(schema["required"]) == set(schema["properties"]) == set(Enrichment.model_fields)


# --- Provider selection ------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return {"LLM_STATE_DIR": str(tmp_path)}


@pytest.fixture(autouse=True)
def fake_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")


def test_default_provider_is_gemini_flash_lite(env: dict[str, str]) -> None:
    client = llm_client_from_env(COMPETITORS, env)
    assert isinstance(client, GeminiClient)
    assert client.model == "gemini-3.5-flash-lite"
    assert isinstance(client, LLMClient)


def test_switch_to_claude_with_env_only(env: dict[str, str]) -> None:
    client = llm_client_from_env(COMPETITORS, {**env, "LLM_PROVIDER": "claude"})
    assert isinstance(client, ClaudeClient)
    assert client.model == "claude-haiku-5-5"


def test_model_and_limits_from_env(env: dict[str, str]) -> None:
    client = llm_client_from_env(
        COMPETITORS,
        {**env, "LLM_PROVIDER": "gemini", "LLM_MODEL": "gemini-3.8-flash", "LLM_RPM": "4", "LLM_RPD": "18"},
    )
    assert client.model == "gemini-3.8-flash"
    assert client.rate_limiter is not None
    assert (client.rate_limiter.rpm, client.rate_limiter.rpd) == (4, 18)


def test_unknown_provider_fails_fast(env: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="unknown LLM_PROVIDER"):
        llm_client_from_env(COMPETITORS, {**env, "LLM_PROVIDER": "openai"})


# --- Rate limiter -----------------------------------------------------------------------------


async def test_daily_budget_raises_instead_of_waiting(tmp_path: Path) -> None:
    limiter = RateLimiter(rpm=100, rpd=2, state_path=tmp_path / "q.json")
    await limiter.acquire()
    await limiter.acquire()
    with pytest.raises(LlmQuotaExhaustedError):
        await limiter.acquire()


async def test_daily_count_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "q.json"
    await RateLimiter(rpm=100, rpd=3, state_path=path).acquire()
    restarted = RateLimiter(rpm=100, rpd=3, state_path=path)
    assert restarted.used_today == 1


async def test_per_minute_limit_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("signal_scout.enrichment.rate_limit.asyncio.sleep", fake_sleep)
    limiter = RateLimiter(rpm=2)
    for _ in range(3):
        await limiter.acquire()
    assert len(slept) == 1 and 59 <= slept[0] <= 60


# --- Fake client -------------------------------------------------------------------------------


async def test_fake_client_satisfies_protocol_and_can_fail(signal: SignalInput) -> None:
    fake = FakeLLMClient()
    assert isinstance(fake, LLMClient)
    assert (await fake.enrich(signal)).enrichment.competitors == ["Asana"]
    with pytest.raises(LlmOutputError):
        await FakeLLMClient(responder=lambda s: None).enrich(signal)


# --- Contract: real recorded responses still parse through the adapters ----------------------------


@pytest.mark.parametrize("provider", ["claude", "gemini"])
async def test_recorded_live_response_parses(provider: str, signal: SignalInput) -> None:
    from .conftest import FIXTURES, FakeAnthropic, FakeGenai

    name = f"{provider}_recorded.json"
    if not (FIXTURES / name).exists():
        pytest.skip(f"no live recording yet; run scripts/record_llm_fixture.py {provider}")
    if provider == "claude":
        client: ClaudeClient | GeminiClient = ClaudeClient(COMPETITORS, client=FakeAnthropic(name))  # type: ignore[arg-type]
    else:
        client = GeminiClient(COMPETITORS, client=FakeGenai(name))  # type: ignore[arg-type]
    result = await client.enrich(signal)
    assert result.enrichment.competitors == ["Asana"]
    assert result.usage.input_tokens > 0


# --- Prompt versions ---------------------------------------------------------------------------


def test_v2_is_v1_plus_targeted_edits_and_both_stay_selectable(env: dict[str, str]) -> None:
    from signal_scout.enrichment.prompt import build_system_prompt

    v1, v2 = build_system_prompt(COMPETITORS, "v1"), build_system_prompt(COMPETITORS, "v2")
    assert v1 != v2 and len(v2) > len(v1)
    assert "When torn between two levels, choose the lower one." in v2 and "torn" not in v1
    with pytest.raises(ValueError, match="unknown prompt version"):
        build_system_prompt(COMPETITORS, "v9")
    client = llm_client_from_env(COMPETITORS, {**env, "LLM_PROMPT_VERSION": "v2"})
    assert client.prompt_version == "v2" and client.system_prompt == v2
