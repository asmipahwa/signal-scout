"""Decide which tracked competitors a signal mentions, before any LLM tokens are spent.

Matching is case-sensitive on word boundaries because several names are ordinary words:
"Notion" vs "the notion that", "Linear" vs "linear growth", "Monday" vs "on Monday". Aliases for
those are chosen to be specific (e.g. "monday.com"), and the LLM confirms relevance later.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence


class CompetitorMatcher:
    """Precompiled alias patterns for every competitor."""

    def __init__(self, aliases: Mapping[str, Sequence[str]]) -> None:
        self._patterns = {
            name: re.compile(r"(?<![\w.])(" + "|".join(re.escape(a) for a in names) + r")(?![\w])")
            for name, names in aliases.items()
            if names
        }

    def match(self, *texts: str) -> list[str]:
        """Names of competitors whose aliases appear in any of `texts`, sorted."""
        joined = "\n".join(texts)
        return sorted(name for name, pattern in self._patterns.items() if pattern.search(joined))
