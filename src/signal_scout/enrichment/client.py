"""The `LLMClient` protocol and the provider-independent call loop.

Why a base class and not just a protocol: retry-on-bad-output, rate-limit backoff, usage
accounting, and validation are the same for every provider. Each adapter only implements
`_generate`, which makes one raw call and returns the parsed JSON (or None) plus usage.
That keeps provider code small and makes adapters easy to test with recorded responses.
"""

from __future__ import annotations

import asyncio
import random
import time
from abc import ABC, abstractmethod
from typing import Any, Protocol, runtime_checkable

from pydantic import ValidationError

from .errors import LlmOutputError, LlmTransientError
from .models import Enrichment, EnrichmentResult, LlmUsage, SignalInput
from .prompt import PROMPT_VERSION, build_system_prompt, render_signal
from .rate_limit import RateLimiter


@runtime_checkable
class LLMClient(Protocol):
    """Anything that can turn a signal into a validated Enrichment."""

    provider: str
    model: str

    async def enrich(self, signal: SignalInput) -> EnrichmentResult: ...


class BaseLLMClient(ABC):
    """Shared call loop. Subclasses implement `_generate` for one provider."""

    provider: str = "base"

    def __init__(
        self,
        model: str,
        competitors: list[str],
        rate_limiter: RateLimiter | None = None,
        max_output_attempts: int = 2,
        max_rate_limit_retries: int = 5,
        prompt_version: str = PROMPT_VERSION,
    ) -> None:
        self.model = model
        self.competitors = list(competitors)
        self.prompt_version = prompt_version
        self.system_prompt = build_system_prompt(self.competitors, prompt_version)
        self.rate_limiter = rate_limiter
        self.max_output_attempts = max_output_attempts
        self.max_rate_limit_retries = max_rate_limit_retries

    @abstractmethod
    async def _generate(self, system_prompt: str, user_message: str) -> tuple[dict[str, Any] | None, LlmUsage]:
        """Make one call and return (parsed JSON or None if unparseable, usage).

        Raise LlmTransientError (or its subclass LlmRateLimitedError) when retrying may help.
        """

    async def enrich(self, signal: SignalInput) -> EnrichmentResult:
        total = LlmUsage(provider=self.provider, model=self.model)
        last_error: Exception | None = None
        for attempt in range(1, self.max_output_attempts + 1):
            data, usage = await self._generate_with_backoff(render_signal(signal))
            _add_usage(total, usage)
            if data is None:
                last_error = ValueError("response was not valid JSON")
                continue
            try:
                enrichment = Enrichment.model_validate(data, context={"competitors": self.competitors})
            except ValidationError as exc:
                last_error = exc
                continue
            return EnrichmentResult(
                enrichment=enrichment, usage=total, prompt_version=self.prompt_version, attempts=attempt
            )
        raise LlmOutputError(f"{self.provider} output invalid after {self.max_output_attempts} attempts: {last_error}")

    async def _generate_with_backoff(self, user_message: str) -> tuple[dict[str, Any] | None, LlmUsage]:
        for retry in range(self.max_rate_limit_retries + 1):
            if self.rate_limiter:
                await self.rate_limiter.acquire()
            start = time.perf_counter()
            try:
                data, usage = await self._generate(self.system_prompt, user_message)
            except LlmTransientError as exc:
                if retry == self.max_rate_limit_retries:
                    raise
                # Honor the provider's hint if it gave one; otherwise exponential backoff with jitter.
                delay = exc.retry_after if exc.retry_after is not None else min(60.0, 2.0**retry)
                await asyncio.sleep(delay + random.uniform(0, 1))
                continue
            usage.latency_s = time.perf_counter() - start
            return data, usage
        raise AssertionError("unreachable")


def _add_usage(total: LlmUsage, part: LlmUsage) -> None:
    total.input_tokens += part.input_tokens
    total.output_tokens += part.output_tokens
    total.cache_write_tokens += part.cache_write_tokens
    total.cache_read_tokens += part.cache_read_tokens
    total.thinking_tokens += part.thinking_tokens
    total.cost_usd += part.cost_usd
    total.latency_s += part.latency_s
