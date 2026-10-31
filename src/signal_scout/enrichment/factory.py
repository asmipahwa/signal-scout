"""Build the configured LLM client from environment variables. This is the only place that knows
which providers exist, so switching providers is a config change, not a code change:

    LLM_PROVIDER=gemini|claude   (default gemini)
    LLM_MODEL=<model id>         (default per provider)
    LLM_RPM, LLM_RPD             (client-side request limits; default per provider)
    LLM_STATE_DIR                (where the daily request counter is persisted)
    LLM_PROMPT_VERSION           (default: the current best version in prompt.py)
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

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
    rpm = int(env.get("LLM_RPM", default_rpm))
    rpd_raw = env.get("LLM_RPD")
    rpd = int(rpd_raw) if rpd_raw else default_rpd
    state_dir = Path(env.get("LLM_STATE_DIR", ".state"))
    limiter = RateLimiter(rpm=rpm, rpd=rpd, state_path=state_dir / f"llm-quota-{provider}.json")

    model = env.get("LLM_MODEL", "").strip()
    prompt_version = env.get("LLM_PROMPT_VERSION", "").strip() or PROMPT_VERSION
    if provider == "claude":
        return ClaudeClient(
            competitors, model=model or DEFAULT_CLAUDE_MODEL, rate_limiter=limiter, prompt_version=prompt_version
        )
    return GeminiClient(
        competitors, model=model or DEFAULT_GEMINI_MODEL, rate_limiter=limiter, prompt_version=prompt_version
    )
