"""LLM enrichment: turn a raw signal into a validated, provider-neutral `Enrichment`.

Use `llm_client_from_env()` to get the configured client; switch providers with `LLM_PROVIDER`.
"""

from .client import BaseLLMClient, LLMClient
from .errors import LlmError, LlmOutputError, LlmQuotaExhaustedError, LlmRateLimitedError
from .factory import llm_client_from_env
from .models import CATEGORIES, Enrichment, EnrichmentResult, LlmUsage, SignalInput
from .prompt import PROMPT_VERSION

__all__ = [
    "CATEGORIES",
    "PROMPT_VERSION",
    "BaseLLMClient",
    "Enrichment",
    "EnrichmentResult",
    "LLMClient",
    "LlmError",
    "LlmOutputError",
    "LlmQuotaExhaustedError",
    "LlmRateLimitedError",
    "LlmUsage",
    "SignalInput",
    "llm_client_from_env",
]
