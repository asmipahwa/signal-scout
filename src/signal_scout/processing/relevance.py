"""Cheap relevance filtering that runs before any LLM call.

Two checks, both deliberately simple:

1. **Competitor matching.** A signal must mention a tracked competitor by one of its aliases.
   Matching is case-sensitive on word boundaries, because several competitor names are ordinary
   words ("the notion that...", "a linear process", "on Monday"). Lowercase mentions don't count.
2. **Low-value patterns.** Comparison posts, pricing explainers, reviews, and how-tos mention a
   competitor without saying anything new about it. The prototype eval showed these were the bulk of
   what the LLM later marked irrelevant, so we drop them by title before spending quota.

Both are tuned against the labeled eval set (see docs/EVALS.md); any false positives here are
caught later by the LLM's own `is_about_tracked_competitor` judgment.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


class CompetitorMatcher:
    """Finds which tracked competitors a piece of text mentions."""

    def __init__(self, aliases: Mapping[str, Sequence[str]]) -> None:
        self._patterns = {
            name: re.compile(r"\b(" + "|".join(re.escape(a) for a in sorted(names, key=len, reverse=True)) + r")\b")
            for name, names in aliases.items()
        }

    def match(self, text: str) -> list[str]:
        return sorted(name for name, pattern in self._patterns.items() if pattern.search(text))


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


def decide(
    matcher: CompetitorMatcher, title: str, text: str, known_competitors: Sequence[str] = ()
) -> RelevanceDecision:
    """Decide whether a signal is worth sending to the LLM.

    `known_competitors` is for sources that are about one company by definition (its own changelog),
    which skip alias matching.
    """
    competitors = sorted(set(known_competitors)) or matcher.match(f"{title}\n{text}")
    if not competitors:
        return RelevanceDecision(False, [], "no_competitor")
    if not known_competitors and (reason := low_value_reason(title)):
        return RelevanceDecision(False, competitors, f"low_value:{reason}")
    return RelevanceDecision(True, competitors, "ok")
