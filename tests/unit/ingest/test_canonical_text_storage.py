"""Canonical URLs, content hashing, excerpts, and the S3 key layout."""

from __future__ import annotations

import gzip
import hashlib
from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st
from hypothesis.provisional import urls

from signal_scout.ingest.canonical import canonical_url, domain_of
from signal_scout.ingest.text import content_hash, excerpt, extract_text, normalize_text, strip_html
from signal_scout.storage import MemoryBlobStore, gzip_bytes, raw_key, sha256_hex, text_key

# --- canonical URLs ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "a,b",
    [
        ("https://www.Example.com/post/", "https://example.com/post"),
        ("https://example.com/post?utm_source=x&utm_medium=rss", "https://example.com/post"),
        ("https://example.com/post#comments", "https://example.com/post"),
        ("https://example.com:443/post", "https://example.com/post"),
        ("https://example.com/p?b=2&a=1", "https://example.com/p?a=1&b=2"),
        ("https://example.com/p?fbclid=abc&id=7&gclid=z", "https://example.com/p?id=7"),
        ("  https://example.com/p  ", "https://example.com/p"),
    ],
)
def test_equivalent_urls_share_a_canonical_form(a: str, b: str) -> None:
    assert canonical_url(a) == canonical_url(b)


def test_meaningful_differences_are_kept() -> None:
    assert canonical_url("https://example.com/A") != canonical_url("https://example.com/a")  # path case
    assert canonical_url("https://example.com/p?id=1") != canonical_url("https://example.com/p?id=2")
    assert canonical_url("https://example.com:8443/p") == "https://example.com:8443/p"


@given(urls())
def test_canonical_url_is_idempotent(url: str) -> None:
    once = canonical_url(url)
    assert canonical_url(once) == once


def test_domain_of_strips_www_and_case() -> None:
    assert domain_of("https://WWW.Bing.com/news/search?q=x") == "bing.com"


# --- text and hashing --------------------------------------------------------------------------


def test_strip_html_removes_tags_entities_and_extra_whitespace() -> None:
    assert strip_html("<p>Asana&nbsp;launches <b>AI</b>\n\n Studio</p>") == "Asana launches AI Studio"


def test_content_hash_ignores_whitespace_and_markup_differences() -> None:
    a = content_hash("Notion 3.8", extract_text("<p>Context for   your agents</p>"))
    b = content_hash(" Notion 3.8 ", extract_text("<div>Context for your\nagents</div>"))
    assert a == b
    assert len(a) == 64
    assert a != content_hash("Notion 3.9", "Context for your agents")


@given(st.text(), st.text())
def test_content_hash_is_stable_under_normalization(title: str, text: str) -> None:
    assert content_hash(title, text) == content_hash(normalize_text(title), normalize_text(text))


def test_extract_text_uses_trafilatura_for_full_pages() -> None:
    body = " ".join(f"Sentence number {i} about the Linear changelog and its new features." for i in range(60))
    page = (
        "<html><head><title>t</title></head><body><nav>Home | Pricing</nav>"
        f"<article><p>{body}</p></article></body></html>"
    )
    text = extract_text(page)
    assert "Sentence number 59" in text
    assert "<p>" not in text


def test_excerpt_cuts_on_a_word_boundary() -> None:
    assert excerpt("short text", 500) == "short text"
    cut = excerpt("alpha beta gamma delta", 12)
    assert cut == "alpha beta…"
    assert len(cut) <= 12


# --- S3 key layout -----------------------------------------------------------------------------


def test_s3_keys_are_content_addressed_and_date_partitioned() -> None:
    sha = sha256_hex(b"<p>hello</p>")
    assert raw_key(sha, datetime(2026, 3, 7, 23, 59, tzinfo=UTC)) == f"raw/2026/03/07/{sha}.html.gz"
    assert text_key(sha) == f"text/{sha}.txt.gz"
    assert sha == hashlib.sha256(b"<p>hello</p>").hexdigest()


def test_gzip_is_deterministic_and_round_trips() -> None:
    assert gzip_bytes(b"same") == gzip_bytes(b"same")
    assert gzip.decompress(gzip_bytes(b"same")) == b"same"


async def test_memory_store_skips_existing_keys() -> None:
    store = MemoryBlobStore()
    assert await store.put_if_absent("text/a.txt.gz", b"one", "text/plain")
    assert not await store.put_if_absent("text/a.txt.gz", b"two", "text/plain")
    assert await store.get("text/a.txt.gz") == b"one"
    assert store.puts == 1
