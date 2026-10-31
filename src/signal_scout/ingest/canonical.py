"""Canonical URLs, so the same article reached through different links dedupes to one signal.

Feeds and aggregators decorate links with tracking parameters, fragments, `www.`, default ports and
trailing slashes. We strip those and sort the remaining query parameters. Path case is preserved
because many servers treat paths case-sensitively.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAM = re.compile(
    r"^(utm_.*|fbclid|gclid|dclid|msclkid|mc_cid|mc_eid|_hsenc|_hsmi|mkt_tok|ref|ref_src|ocid|cmpid|igshid|spm)$",
    re.I,
)
DEFAULT_PORTS = {"http": 80, "https": 443}


def canonical_url(url: str) -> str:
    """Normalize `url` for equality checks. Idempotent: canonical_url(canonical_url(u)) == canonical_url(u)."""
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower().removeprefix("www.")
    port = parts.port
    netloc = host if port is None or DEFAULT_PORTS.get(scheme) == port else f"{host}:{port}"
    path = parts.path.rstrip("/")
    query = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING_PARAM.match(k))
    return urlunsplit((scheme, netloc, path, urlencode(query), ""))


def domain_of(url: str) -> str:
    """The host used for per-domain rate limiting (lowercase, without `www.`)."""
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")
