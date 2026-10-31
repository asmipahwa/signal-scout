"""Turn a fetched feed body into a list of `FeedItem`s, whatever the source format.

RSS 2.0 and Atom go through feedparser; the HN Algolia search API returns JSON. Source quirks
are handled here so the rest of the pipeline never sees them, e.g. Bing News wraps every link in
a click-tracking redirect whose real target is the `url` query parameter.
"""

from __future__ import annotations

import calendar
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import feedparser

from ..db.models import RSS_SOURCE_TYPES, SourceType


class FeedParseError(Exception):
    """The body is not a feed we can read (e.g. an HTML page served at a feed URL)."""


@dataclass(frozen=True)
class FeedItem:
    """One entry from a feed. `raw_html` is the entry's own markup (content, summary, or story text)."""

    url: str
    title: str
    published_at: datetime | None
    raw_html: str


def unwrap_redirect(link: str) -> str:
    """Return the publisher URL behind an aggregator click-tracking link (Bing News)."""
    parts = urlsplit(link)
    if parts.hostname and parts.hostname.endswith("bing.com") and parts.path.endswith("/apiclick.aspx"):
        target = parse_qs(parts.query).get("url")
        if target:
            return target[0]
    return link


def _entry_time(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            return datetime.fromtimestamp(calendar.timegm(value), UTC)
    for key in ("published", "updated"):
        value = entry.get(key)
        if value:
            try:
                return parsedate_to_datetime(value).astimezone(UTC)
            except (TypeError, ValueError):
                continue
    return None


def _entry_html(entry: Any) -> str:
    contents = entry.get("content") or []
    for content in contents:
        if content.get("value"):
            return str(content["value"])
    return str(entry.get("summary") or entry.get("description") or "")


def parse_rss(body: bytes) -> list[FeedItem]:
    """Parse RSS or Atom. Raises FeedParseError if the body is not a feed at all."""
    parsed = feedparser.parse(body)
    if not parsed.get("version") and not parsed.entries:
        raise FeedParseError(f"not an RSS/Atom feed: {parsed.get('bozo_exception') or 'no entries'}")
    items = []
    for entry in parsed.entries:
        link = entry.get("link") or ""
        if not link:
            continue
        items.append(
            FeedItem(
                url=unwrap_redirect(link),
                title=str(entry.get("title") or ""),
                published_at=_entry_time(entry),
                raw_html=_entry_html(entry),
            )
        )
    return items


def parse_hn(body: bytes) -> list[FeedItem]:
    """Parse an HN Algolia `search_by_date` response. Link posts point at the article; Ask HN at the thread."""
    try:
        hits = json.loads(body)["hits"]
    except (ValueError, KeyError, TypeError) as exc:
        raise FeedParseError(f"not an HN Algolia response: {exc}") from exc
    items = []
    for hit in hits:
        url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit['objectID']}"
        created = hit.get("created_at")
        published = datetime.fromisoformat(created.replace("Z", "+00:00")) if created else None
        items.append(
            FeedItem(
                url=url, title=hit.get("title") or "", published_at=published, raw_html=hit.get("story_text") or ""
            )
        )
    return items


def parse_items(source_type: SourceType, body: bytes) -> list[FeedItem]:
    if source_type in RSS_SOURCE_TYPES:
        return parse_rss(body)
    if source_type is SourceType.hn_search:
        return parse_hn(body)
    raise NotImplementedError(f"no parser for source type {source_type.value} yet")
