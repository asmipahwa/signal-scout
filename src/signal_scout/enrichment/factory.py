"""Build the configured LLM client from environment variables. This is the only place that knows
which providers exist, so switching providers is a config change, not a code change:

    LLM_PROVIDER=gemini|claude   (default gemini)
    LLM_MODEL=<model id>         (default per provider)
    LLM_RPM, LLM_RPD             (client-side request limits; default per provider)
    LLM_STATE_DIR                (where the daily request counter is persisted)
    LLM_PROMPT_VERSION           (default: the current best version in prompt.py)
    GEMINI_API_KEY / ANTHROPIC_API_KEY  (taken from the same mapping, so a process can pass its settings in)
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

import anthropic
from google import genai

from .claude import DEFAULT_CLAUDE_MODEL, ClaudeClient
from .client import BaseLLMClient
from .gemini import DEFAULT_GEMINI_MODEL, GeminiClient
from .prompt import PROMPT_VERSION
from .rate_limit import RateLimiter

# Client-side pacing at ~80-90% of the provider's limits, so we never touch the ceiling.
# Gemini free tier for this project (AI Studio rate-limit page, 2026-10-30):
#   gemini-3.5-flash-lite: 15 RPM, 250K TPM, 500 RPD   <- default model
#   gemini-3.8-flash:       5 RPM, 250K TPM,  20 RPD   <- too small for a pipeline; set LLM_RPM=4 LLM_RPD=18 to use it
# Server 429s are still handled: per-minute ones are retried, per-day ones stop the run cleanly.
DEFAULT_LIMITS: dict[str, tuple[int, int | None]] = {
    "gemini": (12, 450),
    "claude": (40, None),  # paid; the CostGuard caps spend instead of a request count
}


def llm_client_from_env(competitors: list[str], env: Mapping[str, str] | None = None) -> BaseLLMClient:
    env = os.environ if env is None else env
    provider = env.get("LLM_PROVIDER", "gemini").strip().lower()
    if provider not in DEFAULT_LIMITS:
        raise ValueError(f"unknown LLM_PROVIDER {provider!r}; expected one of {sorted(DEFAULT_LIMITS)}")

    default_rpm, default_rpd = DEFAULT_LIMITS[provider]
    rpm_raw, rpd_raw = env.get("LLM_RPM", "").strip(), env.get("LLM_RPD", "").strip()
    rpm = int(rpm_raw) if rpm_raw else default_rpm
    rpd = int(rpd_raw) if rpd_raw else default_rpd
    state_dir = Path(env.get("LLM_STATE_DIR", ".state"))
    limiter = RateLimiter(rpm=rpm, rpd=rpd, state_path=state_dir / f"llm-quota-{provider}.json")

    model = env.get("LLM_MODEL", "").strip()
    prompt_version = env.get("LLM_PROMPT_VERSION", "").strip() or PROMPT_VERSION
    if provider == "claude":
        claude_sdk = anthropic.AsyncAnthropic(api_key=env.get("ANTHROPIC_API_KEY") or None, max_retries=2)
        return ClaudeClient(
            competitors,
            model=model or DEFAULT_CLAUDE_MODEL,
            client=claude_sdk,
            rate_limiter=limiter,
            prompt_version=prompt_version,
        )
    return GeminiClient(
        competitors,
        model=model or DEFAULT_GEMINI_MODEL,
        client=genai.Client(api_key=env.get("GEMINI_API_KEY") or None),
        rate_limiter=limiter,
        prompt_version=prompt_version,
    )
