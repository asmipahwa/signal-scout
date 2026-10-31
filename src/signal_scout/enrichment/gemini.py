"""Gemini adapter: enrichment through the official `google-genai` SDK (the default provider).

Structured output comes from `response_mime_type="application/json"` plus `response_json_schema`,
which constrains generation to the shared enrichment schema. We still parse and validate the text,
because Gemini doesn't enforce every JSON Schema keyword (e.g. string lengths).

Free-tier notes (ai.google.dev, checked 2026-10-30): input and output are free; requests count
against per-minute and per-day limits shown in AI Studio; free-tier prompts may be used by Google
to improve its products, which is acceptable here because every signal is already public.
Gemini caches repeated prompt prefixes implicitly, so the stable system prompt needs no extra setup.
"""

from __future__ import annotations

import json
import re
from typing import Any

from google import genai
from google.genai import errors, types

from .client import BaseLLMClient
from .errors import LlmQuotaExhaustedError, LlmRateLimitedError, LlmRequestError, LlmTransientError
from .models import LlmUsage, enrichment_json_schema
from .pricing import paid_cost_usd

DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"


class GeminiClient(BaseLLMClient):
    provider = "gemini"

    def __init__(
        self,
        competitors: list[str],
        model: str = DEFAULT_GEMINI_MODEL,
        client: genai.Client | None = None,
        free_tier: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(model=model, competitors=competitors, **kwargs)
        self._client = client or genai.Client()  # reads GEMINI_API_KEY from the environment
        self.free_tier = free_tier
        self._schema = enrichment_json_schema(self.competitors)

    def _config(self, system_prompt: str) -> types.GenerateContentConfig:
        return types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            response_json_schema=self._schema,
            # Classification doesn't need deep reasoning; low thinking keeps latency and token use down.
            thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW),
            max_output_tokens=2048,
        )

    async def _generate(self, system_prompt: str, user_message: str) -> tuple[dict[str, Any] | None, LlmUsage]:
        try:
            resp = await self._client.aio.models.generate_content(
                model=self.model, contents=user_message, config=self._config(system_prompt)
            )
        except errors.ClientError as exc:
            if exc.code == 429:
                details = json.dumps(exc.details) if exc.details else str(exc)
                # A per-day quota won't recover by waiting a minute; stop and resume tomorrow.
                if "PerDay" in details:
                    raise LlmQuotaExhaustedError(f"Gemini daily quota exhausted: {exc.message}") from exc
                raise LlmRateLimitedError(str(exc), _retry_delay_seconds(details)) from exc
            raise LlmRequestError(f"Gemini rejected the request: {exc.code} {exc.message}") from exc
        except errors.ServerError as exc:
            # 5xx after the SDK's own retries, e.g. 503 "model is experiencing high demand".
            raise LlmTransientError(f"Gemini unavailable: {exc.code} {exc.message}") from exc

        m = resp.usage_metadata
        cached = (m.cached_content_token_count or 0) if m else 0
        usage = LlmUsage(
            provider=self.provider,
            model=self.model,
            # Gemini's prompt count includes cached tokens; split them out so they aren't double-counted.
            input_tokens=((m.prompt_token_count or 0) - cached) if m else 0,
            output_tokens=(m.candidates_token_count or 0) if m else 0,
            cache_read_tokens=cached,
            thinking_tokens=(m.thoughts_token_count or 0) if m else 0,
        )
        usage.cost_usd = 0.0 if self.free_tier else paid_cost_usd(usage)

        try:
            data = json.loads(resp.text or "")
        except json.JSONDecodeError:
            return None, usage
        return (data if isinstance(data, dict) else None), usage


def _retry_delay_seconds(details: str) -> float | None:
    """Gemini puts a RetryInfo hint like "retryDelay": "17s" in 429 error details."""
    match = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', details)
    return float(match.group(1)) if match else None
