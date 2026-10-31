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

## Throughput

`backfill-job-runs.txt`: 14,551 HN stories fetched and 12,595 new signals stored in 933.7 s of wall-clock
time across 12 backfill runs, i.e. **48,561 new signals/hour** through the full ingestion path (HTTP,
parsing, S3 put of raw + text, exact dedupe, alias matching, Postgres insert, `job_runs`), on the machine
described in `docs/PERFORMANCE.md`. The 12,595 includes the 3,329 off-topic `Linear` signals deleted
afterwards (above), so the net new signals kept from the backfill is 9,266. Throughput is bounded by the
polite per-domain rate limit (0.5 requests/s to hn.algolia.com with a burst of 2), not by our code.
