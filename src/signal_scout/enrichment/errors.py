"""Enrichment errors. The pipeline catches these; none of them should crash a worker."""

from __future__ import annotations


class LlmError(Exception):
    """Base class for enrichment failures."""


class LlmOutputError(LlmError):
    """The model kept returning output that does not parse or validate. Mark the signal enrichment_failed."""


class LlmRateLimitedError(LlmError):
    """Short-term rate limit (per-minute). Safe to wait and retry."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class LlmQuotaExhaustedError(LlmError):
    """Daily quota or budget used up. Stop the run; resume after the reset."""
