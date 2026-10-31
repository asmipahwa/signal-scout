"""Claude adapter: enrichment through the official `anthropic` SDK.

Structured output comes from a strict tool. The tool's input schema is the shared enrichment
schema, `strict: true` makes the API guarantee the input matches it, and `tool_choice` forces
that one tool. On Haiku 5.5 a forced tool call also skips thinking, which keeps calls cheap.

The long system prompt carries a `cache_control` breakpoint, so after the first call it is
read from cache at 10% of the input price.
"""

from __future__ import annotations

from typing import Any

import anthropic
from anthropic.types import ToolParam

from .client import BaseLLMClient
from .errors import LlmQuotaExhaustedError, LlmRateLimitedError, LlmRequestError, LlmTransientError
from .models import LlmUsage, enrichment_json_schema
from .pricing import paid_cost_usd

DEFAULT_CLAUDE_MODEL = "claude-haiku-5-5"
TOOL_NAME = "record_enrichment"


class ClaudeClient(BaseLLMClient):
    provider = "claude"

    def __init__(
        self,
        competitors: list[str],
        model: str = DEFAULT_CLAUDE_MODEL,
        client: anthropic.AsyncAnthropic | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(model=model, competitors=competitors, **kwargs)
        # The SDK retries connection errors, 429 and 5xx itself (honoring retry-after) before we see them.
        self._client = client or anthropic.AsyncAnthropic(max_retries=2)
        self._tool: ToolParam = {
            "name": TOOL_NAME,
            "description": "Record the structured analysis of one competitive signal.",
            "strict": True,
            "input_schema": enrichment_json_schema(self.competitors),
        }

    async def _generate(self, system_prompt: str, user_message: str) -> tuple[dict[str, Any] | None, LlmUsage]:
        try:
            resp = await self._client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
                tools=[self._tool],
                tool_choice={"type": "tool", "name": TOOL_NAME},
                output_config={"effort": "low"},
                messages=[{"role": "user", "content": user_message}],
            )
        except anthropic.RateLimitError as exc:
            retry_after = exc.response.headers.get("retry-after")
            raise LlmRateLimitedError(str(exc), float(retry_after) if retry_after else None) from exc
        except anthropic.PermissionDeniedError as exc:  # e.g. credit balance too low
            raise LlmQuotaExhaustedError(str(exc)) from exc
        except anthropic.APIConnectionError as exc:
            raise LlmTransientError(f"Claude connection failed: {exc}") from exc
        except anthropic.APIStatusError as exc:
            # 5xx and 529 (overloaded) survive the SDK's own retries only during real incidents; back off.
            if exc.status_code >= 500:
                raise LlmTransientError(f"Claude unavailable: {exc.status_code}") from exc
            raise LlmRequestError(f"Claude rejected the request: {exc.status_code} {exc.message}") from exc

        u = resp.usage
        usage = LlmUsage(
            provider=self.provider,
            model=self.model,
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_write_tokens=u.cache_creation_input_tokens or 0,
            cache_read_tokens=u.cache_read_input_tokens or 0,
        )
        usage.cost_usd = paid_cost_usd(usage)

        block = next((b for b in resp.content if b.type == "tool_use" and b.name == TOOL_NAME), None)
        data = block.input if block is not None and isinstance(block.input, dict) else None
        return data, usage
