"""Record one real API response per provider as a test fixture.

The adapter tests replay hand-built responses for deterministic logic checks. This script adds
`<provider>_recorded.json`, a response captured from the live API for the same test signal, and the
contract test in tests/unit/enrichment replays it through the real adapter. That proves the adapter
still matches what the provider actually returns. One call per run, so it costs at most a fraction
of a cent on Claude and nothing on the Gemini free tier.

Usage (LLM_MODEL optionally overrides the provider's default model):
    scripts/with-secrets.sh uv run python scripts/record_llm_fixture.py gemini
    scripts/with-secrets.sh uv run python scripts/record_llm_fixture.py claude
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anthropic
from google import genai

from signal_scout.enrichment.claude import DEFAULT_CLAUDE_MODEL, ClaudeClient
from signal_scout.enrichment.gemini import DEFAULT_GEMINI_MODEL, GeminiClient
from signal_scout.enrichment.models import SignalInput

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "unit" / "enrichment" / "fixtures"
COMPETITORS = ["Asana", "ClickUp", "Notion"]

# Same real Bing News item the unit tests use.
SIGNAL = SignalInput(
    source_type="news",
    source="bing:Hindustan Times on MSN",
    title="Asana, popular work management platform, down for thousands of users; what we know so far",
    url="https://www.msn.com/en-in/news/other/asana-popular-work-management-platform-down-for-thousands-of-users-what-we-know-so-far/ar-AA2fL04U",  # noqa: E501
    published_at=datetime(2026, 10, 21, 17, 18, tzinfo=UTC),
    text="Work-management platform Asana is experiencing an outage, with nearly 2,300 U.S. users reporting problems on Down Detector.",  # noqa: E501
    matched_competitors=["Asana"],
)


async def record(provider: str) -> dict[str, Any]:
    captured: list[Any] = []
    if provider == "claude":
        sdk = anthropic.AsyncAnthropic()
        original = sdk.messages.create

        async def create(**kwargs: Any) -> Any:
            captured.append(await original(**kwargs))
            return captured[-1]

        sdk.messages.create = create  # type: ignore[method-assign]
        model = os.environ.get("LLM_MODEL") or DEFAULT_CLAUDE_MODEL
        result = await ClaudeClient(COMPETITORS, model=model, client=sdk, max_output_attempts=1).enrich(SIGNAL)
        body: dict[str, Any] = captured[-1].model_dump(mode="json")
    elif provider == "gemini":
        client = genai.Client()
        original_gen = client.aio.models.generate_content

        async def generate_content(**kwargs: Any) -> Any:
            captured.append(await original_gen(**kwargs))
            return captured[-1]

        client.aio.models.generate_content = generate_content  # type: ignore[method-assign]
        model = os.environ.get("LLM_MODEL") or DEFAULT_GEMINI_MODEL
        result = await GeminiClient(COMPETITORS, model=model, client=client, max_output_attempts=1).enrich(SIGNAL)
        body = captured[-1].model_dump(mode="json", exclude_none=True)
        body.pop("sdk_http_response", None)  # transport headers; not part of the response contract
    else:
        raise SystemExit("provider must be 'claude' or 'gemini'")
    print(f"{provider}: {result.enrichment.model_dump()}\nusage: {result.usage.model_dump()}", file=sys.stderr)
    return body


def main() -> None:
    provider = sys.argv[1] if len(sys.argv) > 1 else ""
    body = asyncio.run(record(provider))
    out = FIXTURES / f"{provider}_recorded.json"
    out.write_text(json.dumps(body, indent=2) + "\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
