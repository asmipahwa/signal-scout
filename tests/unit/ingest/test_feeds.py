"""Feed parsing against real feed bodies saved on 2026-10-30 (see tests/fixtures/README.md)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from signal_scout.db.models import SourceType
from signal_scout.ingest.feeds import FeedParseError, parse_hn, parse_items, parse_rss, unwrap_redirect

FEEDS = Path(__file__).resolve().parents[2] / "fixtures" / "feeds"


def body(name: str) -> bytes:
    return (FEEDS / name).read_bytes()


def test_notion_releases_including_its_javascript_style_dates() -> None:
    items = parse_rss(body("notion_releases.xml"))
    assert len(items) == 154
    first = items[0]
    assert first.title == "Agents, rebuilt to do more for less"
    assert first.url == "https://www.notion.so/releases/2026-10-19"
    assert first.published_at == datetime(2026, 10, 19, tzinfo=UTC)


def test_bing_news_links_are_unwrapped_to_the_publisher() -> None:
    items = parse_rss(body("bing_news_asana.xml"))
    assert len(items) == 10
    assert all("bing.com" not in i.url for i in items)
    assert items[0].url.startswith("https://www.msn.com/en-in/news/other/asana-popular-work-management-platform-down")
    assert "outage" in items[0].raw_html


def test_atom_feed() -> None:
    items = parse_rss(body("theverge_atom.xml"))
    assert len(items) == 10
    assert all(i.url.startswith("https://www.theverge.com/") for i in items)
    assert all(i.published_at is not None for i in items)


def test_full_content_is_preferred_over_summary() -> None:
    items = parse_rss(body("airtable_blog.xml"))
    assert len(items) == 15
    assert max(len(i.raw_html) for i in items) > 2000  # content:encoded, not a one-line summary


def test_hn_algolia_json() -> None:
    items = parse_hn(body("hn_algolia_asana.json"))
    assert len(items) == 50
    assert all(i.url.startswith("http") for i in items)
    assert all(i.published_at is not None and i.published_at.tzinfo is not None for i in items)


def test_html_page_is_not_a_feed() -> None:
    with pytest.raises(FeedParseError):
        parse_rss(b"<!doctype html><html><head><title>Blog</title></head><body>hi</body></html>")


def test_bad_hn_json() -> None:
    with pytest.raises(FeedParseError):
        parse_hn(b"<html>rate limited</html>")


def test_dispatch_by_source_type() -> None:
    assert len(parse_items(SourceType.changelog_rss, body("notion_releases.xml"))) == 154
    assert len(parse_items(SourceType.hn_search, body("hn_algolia_asana.json"))) == 50
    with pytest.raises(NotImplementedError):
        parse_items(SourceType.webpage_watch, b"")


def test_unwrap_leaves_ordinary_links_alone() -> None:
    assert unwrap_redirect("https://techcrunch.com/2026/10/30/x/") == "https://techcrunch.com/2026/10/30/x/"
