# Backfill notes (2026-10-31)

Historical Hacker News stories loaded with `make backfill SINCE=2010-01-01` (Algolia HN Search API,
`typoTolerance=false`, time windows under the 1,000-hit cap). Every story goes through the normal
ingestion path, so it is stored in S3, deduplicated, and alias-matched like a live signal.

## Data correction

The Sprint 2 seed used the bare word `Linear` as the HN query for Linear (the issue tracker). With exact
matching that still returns stories about linear algebra, linear regression, etc.: 3,914 stories, of
which only 68 mention `linear.app` / `Linear app` (the case-sensitive aliases). The other **3,329
signals (3,173 filtered_out + 156 duplicate) were deleted** so they don't inflate ingestion counts, and
the source now queries `linear.app` exactly. The deletion was done with plain SQL against the local
database; the 68 matching signals were kept.

## GDELT

The GDELT DOC API (historical news) answered HTTP 429 to every request from this network, including
requests spaced well beyond its documented 1 request / 5 s, so it was not used.
