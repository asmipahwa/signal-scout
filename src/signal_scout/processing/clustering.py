"""Group enriched signals that describe the same event into one insight.

The prototype clustered on competitor + exact category + shared title words, and split one Notion
story (AI credits and 24-hour agents) into five insights across three categories. Here, category is
ignored: two signals join the same cluster when they are about the same competitor, published within
a time window, and their title + summary are similar by TF-IDF cosine. TF-IDF down-weights words that
appear everywhere (the competitor's own name, "AI", "new"), so the shared *specific* words drive the
match. Thresholds are measured on real data in docs/DESIGN.md.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

_STOPWORDS_TEXT = """
a an and are as at be by for from has have in into is it its of on or that the this to was were will with
new now how what why who your you their they our more says said after over about
"""
STOPWORDS = frozenset(_STOPWORDS_TEXT.split())


@dataclass(frozen=True)
class ClusterInput:
    """The parts of an enriched signal clustering needs."""

    id: str
    competitor: str
    published_at: datetime
    title: str
    summary: str


@dataclass
class Cluster:
    competitor: str
    members: list[ClusterInput] = field(default_factory=list)

    @property
    def first_seen(self) -> datetime:
        return min(m.published_at for m in self.members)

    @property
    def last_seen(self) -> datetime:
        return max(m.published_at for m in self.members)


def tokenize(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in STOPWORDS]


class TfidfVectorizer:
    """Tiny TF-IDF over a fixed corpus. Small enough not to need scikit-learn."""

    def __init__(self, documents: Sequence[str]) -> None:
        tokenized = [set(tokenize(d)) for d in documents]
        n = len(tokenized)
        df = Counter(word for doc in tokenized for word in doc)
        self.idf = {word: math.log((1 + n) / (1 + count)) + 1 for word, count in df.items()}

    def vector(self, text: str) -> dict[str, float]:
        counts = Counter(tokenize(text))
        vec = {w: c * self.idf.get(w, 1.0) for w, c in counts.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        return {w: v / norm for w, v in vec.items()}


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(w, 0.0) for w, v in a.items())


def cluster_signals(
    signals: Sequence[ClusterInput],
    window: timedelta = timedelta(hours=72),
    threshold: float = 0.2,
) -> list[Cluster]:
    """Greedy single-pass clustering in time order.

    A signal joins the most similar existing cluster of the same competitor whose newest member is
    within `window`, if the similarity (max over that cluster's members) reaches `threshold`.
    """
    vectorizer = TfidfVectorizer([f"{s.title} {s.summary}" for s in signals])
    vectors = {s.id: vectorizer.vector(f"{s.title} {s.summary}") for s in signals}
    clusters: list[Cluster] = []
    for s in sorted(signals, key=lambda s: s.published_at):
        best: Cluster | None = None
        best_sim = threshold
        for c in clusters:
            if c.competitor != s.competitor or s.published_at - c.last_seen > window:
                continue
            sim = max(cosine(vectors[s.id], vectors[m.id]) for m in c.members)
            if sim >= best_sim:
                best, best_sim = c, sim
        if best is None:
            best = Cluster(s.competitor)
            clusters.append(best)
        best.members.append(s)
    return clusters
