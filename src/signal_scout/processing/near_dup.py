"""Near-duplicate detection with MinHash LSH.

Exact dedupe (same URL or same content hash) misses the common case in news: one press release
syndicated to dozens of sites with different boilerplate, or the same wire story lightly edited.
MinHash estimates the Jaccard similarity of word shingles in constant memory per document, and LSH
finds candidate matches without comparing every pair, so this stays fast at hundreds of thousands
of signals.
"""

from __future__ import annotations

import re

from datasketch import MinHash, MinHashLSH

DEFAULT_THRESHOLD = 0.8  # estimated Jaccard similarity of 5-word shingles
SHINGLE_WORDS = 5
NUM_PERM = 128


def shingles(text: str, size: int = SHINGLE_WORDS) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    if len(words) < size:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i : i + size]) for i in range(len(words) - size + 1)}


def minhash(text: str, num_perm: int = NUM_PERM) -> MinHash:
    m = MinHash(num_perm=num_perm)
    for shingle in shingles(text):
        m.update(shingle.encode("utf-8"))
    return m


class NearDuplicateIndex:
    """Remembers documents and reports when a new one is a near-copy of something already seen."""

    def __init__(self, threshold: float = DEFAULT_THRESHOLD, num_perm: int = NUM_PERM) -> None:
        self.threshold = threshold
        self.num_perm = num_perm
        self._lsh = MinHashLSH(threshold=threshold, num_perm=num_perm)
        self._hashes: dict[str, MinHash] = {}

    def __len__(self) -> int:
        return len(self._hashes)

    def find_duplicate(self, text: str) -> str | None:
        """Return the id of the most similar indexed document above the threshold, if any."""
        m = minhash(text, self.num_perm)
        best_id, best_sim = None, 0.0
        for candidate in self._lsh.query(m):
            sim = m.jaccard(self._hashes[candidate])
            if sim >= self.threshold and sim > best_sim:
                best_id, best_sim = candidate, sim
        return best_id

    def add(self, doc_id: str, text: str) -> str | None:
        """Index a document unless it duplicates one already indexed. Returns the duplicate's id, if any."""
        if doc_id in self._hashes:
            return None
        if (dup := self.find_duplicate(text)) is not None:
            return dup
        m = minhash(text, self.num_perm)
        self._lsh.insert(doc_id, m)
        self._hashes[doc_id] = m
        return None
