"""Provider-neutral data types for LLM enrichment.

Every LLM adapter (Gemini, Claude, fakes) takes a `SignalInput` and returns an `EnrichmentResult`
holding the same validated `Enrichment`. The rest of the pipeline only ever sees these types, which
is what lets us swap providers with an environment variable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationInfo, field_validator

Category = Literal[
    "pricing_change",
    "product_launch",
    "feature_update",
    "funding",
    "acquisition",
    "partnership",
    "leadership_change",
    "hiring_signal",
    "customer_win",
    "outage_or_incident",
    "legal_regulatory",
    "other",
]
CATEGORIES: tuple[str, ...] = Category.__args__  # type: ignore[attr-defined]

SourceType = Literal["news", "press_release", "changelog", "blog", "webpage_change", "hn", "inbound_webhook"]


class SignalInput(BaseModel):
    """The slice of a signal an LLM needs to see. Text is the article body, snippet, or page diff."""

    source_type: SourceType
    source: str
    title: str
    url: str
    published_at: datetime
    text: str
    matched_competitors: list[str] = Field(default_factory=list)


class Enrichment(BaseModel):
    """Validated LLM output for one signal.

    The JSON schema sent to the providers guarantees the shape; this model enforces the parts
    schemas can't (numeric ranges, string lengths, competitors limited to the tracked list).
    Pass the tracked list as validation context: `Enrichment.model_validate(data, context={"competitors": [...]})`.
    """

    is_about_tracked_competitor: bool
    competitors: list[str]
    category: Category
    summary: str = Field(min_length=10, max_length=600)
    why_it_matters: str = Field(min_length=10, max_length=400)
    importance: int = Field(ge=1, le=5)
    sentiment: float = Field(ge=-1, le=1)
    confidence: float = Field(ge=0, le=1)

    @field_validator("competitors")
    @classmethod
    def only_tracked(cls, v: list[str], info: ValidationInfo) -> list[str]:
        tracked = (info.context or {}).get("competitors")
        if tracked is not None and (unknown := set(v) - set(tracked)):
            raise ValueError(f"untracked competitors: {sorted(unknown)}")
        return sorted(set(v))


class LlmUsage(BaseModel):
    """Token usage and cost for one LLM call, normalized across providers."""

    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    thinking_tokens: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0


class EnrichmentResult(BaseModel):
    enrichment: Enrichment
    usage: LlmUsage
    prompt_version: str
    attempts: int = 1


def enrichment_json_schema(competitors: list[str]) -> dict[str, Any]:
    """JSON schema shared by every provider.

    Kept deliberately simple (no min/max, no string lengths) because providers support different
    subsets of JSON Schema; `Enrichment` validates the rest after the call.
    """
    return {
        "type": "object",
        "properties": {
            "is_about_tracked_competitor": {"type": "boolean"},
            "competitors": {"type": "array", "items": {"type": "string", "enum": list(competitors)}},
            "category": {"type": "string", "enum": list(CATEGORIES)},
            "summary": {"type": "string"},
            "why_it_matters": {"type": "string"},
            "importance": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
            "sentiment": {"type": "number"},
            "confidence": {"type": "number"},
        },
        "required": [
            "is_about_tracked_competitor",
            "competitors",
            "category",
            "summary",
            "why_it_matters",
            "importance",
            "sentiment",
            "confidence",
        ],
        "additionalProperties": False,
    }


__all__ = [
    "CATEGORIES",
    "Category",
    "Enrichment",
    "EnrichmentResult",
    "LlmUsage",
    "SignalInput",
    "SourceType",
    "enrichment_json_schema",
]
