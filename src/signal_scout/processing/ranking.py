"""The insight ranking score.

    rank_score = importance_weight × recency_decay × source_weight × corroboration × novelty

- importance_weight: from the LLM's 1-5 importance, non-linear so a 5 clearly beats a 3.
- recency_decay: 0.5 ** (age / half_life). The prototype showed 48 h was far too steep for a feed that
  spans months (a minor post from yesterday beat a major pricing change from last week), so the
  default half-life is 7 days.
- source_weight: official sources (press releases, changelogs, a competitor's own pricing page) count
  more than aggregators and forum posts.
- corroboration: 1 + ln(number of signals in the cluster). Independent coverage means it matters, but
  with diminishing returns so a story syndicated 40 times doesn't drown everything else.
- novelty: lower when the same competitor already has a recent insight in the same category, so the
  feed doesn't fill up with ten versions of one theme.

Every factor is configurable; docs/DESIGN.md explains the defaults with real examples.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class RankingConfig:
    half_life_hours: float = 168.0
    importance_weight: Mapping[int, float] = field(default_factory=lambda: {1: 0.1, 2: 0.25, 3: 0.5, 4: 0.8, 5: 1.0})
    source_weight: Mapping[str, float] = field(
        default_factory=lambda: {
            "press_release": 1.0,
            "changelog": 1.0,
            "webpage_change": 1.0,
            "blog": 0.95,
            "news": 0.9,
            "inbound_webhook": 0.9,
            "hn": 0.6,
        }
    )
    default_source_weight: float = 0.7
    repeat_theme_novelty: float = 0.7  # multiplier when the competitor already has a fresher same-category insight
    min_importance: int = 2  # importance-1 items are noise by the rubric and never become insights


@dataclass(frozen=True)
class RankInput:
    importance: int  # max over the cluster
    last_seen: datetime
    source_types: frozenset[str]
    signal_count: int
    repeats_recent_theme: bool = False


DEFAULT_CONFIG = RankingConfig()


def rank_score(item: RankInput, now: datetime, config: RankingConfig = DEFAULT_CONFIG) -> float:
    age_hours = max(0.0, (now - item.last_seen).total_seconds() / 3600)
    recency = math.pow(0.5, age_hours / config.half_life_hours)
    importance = config.importance_weight[item.importance]
    source = max((config.source_weight.get(t, config.default_source_weight) for t in item.source_types), default=0.0)
    corroboration = 1 + math.log(max(1, item.signal_count))
    novelty = config.repeat_theme_novelty if item.repeats_recent_theme else 1.0
    return importance * recency * source * corroboration * novelty
