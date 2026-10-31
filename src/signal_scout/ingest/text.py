"""Text extraction, normalization and content hashing.

The content hash is computed over *normalized* text (Unicode NFC, collapsed whitespace), so the
same story re-served with different markup or spacing still dedupes exactly.
"""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata

import trafilatura

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
# Below this size an HTML fragment is a feed summary, not a page; trafilatura tends to drop it.
_MIN_PAGE_CHARS = 2000


def strip_html(raw: str) -> str:
    """Drop tags and entities from a small HTML fragment."""
    return normalize_text(html.unescape(_TAG.sub(" ", raw or "")))


def normalize_text(text: str) -> str:
    return _WS.sub(" ", unicodedata.normalize("NFC", text)).strip()


def extract_text(raw_html: str) -> str:
    """Main text of a page (trafilatura) or of a feed fragment (tag stripping)."""
    if len(raw_html) >= _MIN_PAGE_CHARS:
        extracted = trafilatura.extract(raw_html, include_comments=False, include_tables=False)
        if extracted:
            return normalize_text(extracted)
    return strip_html(raw_html)


def content_hash(title: str, text: str) -> str:
    """SHA-256 of normalized title + text: the exact-duplicate key stored in `signals.content_hash`."""
    return hashlib.sha256(f"{normalize_text(title)}\n{normalize_text(text)}".encode()).hexdigest()


def excerpt(text: str, limit: int) -> str:
    """At most `limit` characters, cut on a word boundary. Postgres only keeps this much text."""
    text = normalize_text(text)
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0]
    return cut + "…"
