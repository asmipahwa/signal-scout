"""Cheap relevance filtering that runs before any LLM call.

Ingestion already records which tracked competitors a signal mentions (case-sensitive alias matching
in `ingest/matching.py`; signals mentioning none are filtered there). This module adds the second
cheap check: **low-value title patterns**. Comparison posts, pricing explainers, reviews, how-tos, and
"I replaced X" stories mention a competitor without saying anything new about it. On the labeled eval
set these patterns drop 16 of 35 irrelevant signals and 0 of 66 relevant ones
(docs/results/2026-10-31-processing/relevance-and-clustering.txt). Anything that slips through is
caught later by the LLM's own `is_about_tracked_competitor` judgment.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

# Title patterns for posts that mention a product without news about it. Each one was checked
# against the labeled eval set; see docs/EVALS.md for hits and misses.
LOW_VALUE_TITLE_PATTERNS: dict[str, re.Pattern[str]] = {
    "comparison": re.compile(r"\bvs\.?\b|\bversus\b|\balternatives?\b|\bcompared?\b", re.IGNORECASE),
    "review": re.compile(r"\breview\b", re.IGNORECASE),
    "pricing_explainer": re.compile(
        r"\bpricing (plans? )?explained\b|\bhow much does\b|\bcost per user\b|\bpricing \d{4}\b"
        r"|everything you need to know",
        re.IGNORECASE,
    ),
    "how_to": re.compile(r"^how to\b|\bhow to use\b|\bi want to\b", re.IGNORECASE),
    "switching_story": re.compile(r"\bi (replaced|tried replacing|switched)\b", re.IGNORECASE),
    "listicle": re.compile(r"^\d+\s+(best|top)\b|\bbest\b.*\btools\b", re.IGNORECASE),
}


@dataclass(frozen=True)
class RelevanceDecision:
    keep: bool
    competitors: list[str]
    reason: str  # "ok", "no_competitor", or "low_value:<pattern>"


def low_value_reason(title: str) -> str | None:
    for name, pattern in LOW_VALUE_TITLE_PATTERNS.items():
        if pattern.search(title):
            return name
    return None


def decide(title: str, competitors: Sequence[str], official_source: bool = False) -> RelevanceDecision:
    """Decide whether a signal is worth sending to the LLM.

    `official_source` is for feeds that are about one company by definition (its own changelog or
    newsroom); their titles are never treated as low-value.
    """
    names = sorted(set(competitors))
    if not names:
        return RelevanceDecision(False, [], "no_competitor")
    if not official_source and (reason := low_value_reason(title)):
        return RelevanceDecision(False, names, f"low_value:{reason}")
    return RelevanceDecision(True, names, "ok")
