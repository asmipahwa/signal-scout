"""A deterministic in-memory LLMClient for pipeline tests. Never touches the network.

Adapter tests (Claude, Gemini) use recorded API responses instead; this fake is for testing
everything *around* enrichment (dedupe, clustering, ranking, alerts) without any provider.
"""

from __future__ import annotations

from collections.abc import Callable

from .errors import LlmOutputError
from .models import Enrichment, EnrichmentResult, LlmUsage, SignalInput
from .prompt import PROMPT_VERSION


def _default_responder(signal: SignalInput) -> Enrichment:
    return Enrichment(
        is_about_tracked_competitor=bool(signal.matched_competitors),
        competitors=signal.matched_competitors,
        category="other",
        summary=f"Fake summary of: {signal.title}"[:600],
        why_it_matters="Fake enrichment used in tests; not a real analysis.",
        importance=3,
        sentiment=0.0,
        confidence=0.5,
    )


class FakeLLMClient:
    provider = "fake"
    model = "fake-model"
    prompt_version = PROMPT_VERSION

    def __init__(self, responder: Callable[[SignalInput], Enrichment | None] = _default_responder) -> None:
        self.responder = responder
        self.calls: list[SignalInput] = []

    async def enrich(self, signal: SignalInput) -> EnrichmentResult:
        self.calls.append(signal)
        enrichment = self.responder(signal)
        if enrichment is None:
            raise LlmOutputError("fake client configured to fail for this signal")
        usage = LlmUsage(provider=self.provider, model=self.model, input_tokens=100, output_tokens=50)
        return EnrichmentResult(enrichment=enrichment, usage=usage, prompt_version=PROMPT_VERSION)
