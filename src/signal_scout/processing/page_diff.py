"""Turn two snapshots of a competitor's web page into a meaningful change, or nothing.

Pricing and plans pages change constantly in ways nobody cares about: rotating testimonials, "© 2026",
cookie banners, "updated 3 days ago", A/B-tested hero copy with tracking IDs. We normalize those away
before hashing, so the hash only changes when the substance does. When it changes, the diff (removed
and added lines) becomes a `webpage_change` signal, so the LLM explains *what changed* rather than
re-reading the whole page.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from dataclasses import dataclass

# Lines that are noise on any marketing page.
NOISE_LINE = re.compile(
    r"cookie|consent|accept all|privacy preferences|©|copyright|all rights reserved|skip to (main )?content",
    re.IGNORECASE,
)
# Volatile fragments inside otherwise meaningful lines.
VOLATILE = [
    (re.compile(r"\b\d{4}-\d{2}-\d{2}(T[\d:.]+Z?)?\b"), "<date>"),
    (
        re.compile(
            r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.? \d{1,2}(st|nd|rd|th)?,? \d{4}\b",
            re.IGNORECASE,
        ),
        "<date>",
    ),
    (re.compile(r"\b(updated|posted|published)\s+\d+\s+\w+\s+ago\b", re.IGNORECASE), "<relative-time>"),
    (re.compile(r"\b[0-9a-f]{16,}\b", re.IGNORECASE), "<id>"),
]


def _essence(line: str) -> str:
    """What a line says, ignoring case, spacing, and punctuation. Digits and currency always count:
    "$10" -> "$12" is one character and exactly the change we must catch."""
    return re.sub(r"[^\w$€£%]+", "", line.lower())


def normalize(text: str) -> list[str]:
    """Meaningful lines of a page's extracted text, with volatile fragments replaced."""
    lines = []
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if not line or NOISE_LINE.search(line):
            continue
        for pattern, replacement in VOLATILE:
            line = pattern.sub(replacement, line)
        lines.append(line)
    return lines


def content_hash(text: str) -> str:
    return hashlib.sha256("\n".join(normalize(text)).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PageChange:
    removed: list[str]
    added: list[str]

    def as_signal_text(self, max_lines: int = 60) -> str:
        """Diff text for the LLM: '-' for removed lines, '+' for added, like a unified diff without noise."""
        lines = [f"- {line}" for line in self.removed] + [f"+ {line}" for line in self.added]
        if len(lines) > max_lines:
            lines = lines[:max_lines] + [f"... ({len(lines) - max_lines} more changed lines)"]
        return "\n".join(lines)


def diff_pages(previous: str, current: str) -> PageChange | None:
    """Return the meaningful change between two page texts, or None if nothing meaningful changed."""
    before, after = normalize(previous), normalize(current)
    if before == after:
        return None
    removed, added = [], []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=before, b=after, autojunk=False).get_opcodes():
        if op in ("replace", "delete"):
            removed.extend(before[i1:i2])
        if op in ("replace", "insert"):
            added.extend(after[j1:j2])
    if [_essence(line) for line in removed] == [_essence(line) for line in added]:
        return None  # only punctuation, case, or spacing changed
    return PageChange(removed, added)
