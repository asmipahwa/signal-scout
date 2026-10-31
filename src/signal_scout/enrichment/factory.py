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

# Client-side pacing. On Gemini's free tier the server enforces the real per-minute and per-day limits
# (shown in AI Studio) and can't bill us for going over: a per-minute 429 is retried after its
# retryDelay and a per-day 429 stops the run cleanly (LlmQuotaExhaustedError) to resume after the reset.
# So we pace at a moderate rate and set no client-side daily cap. A day with 262 requests to
# gemini-3.5-flash-lite was fully accepted (AI Studio usage page, 2026-10-30). Override with LLM_RPM / LLM_RPD.
DEFAULT_LIMITS: dict[str, tuple[int, int | None]] = {
    "gemini": (15, None),
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
