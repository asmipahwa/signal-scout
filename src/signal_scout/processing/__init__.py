"""Pure processing steps between ingestion and alerts: relevance filtering, near-duplicate detection,
clustering, ranking, and webpage-change diffing.

Everything here is free of I/O (no database, network, or LLM) so it is fast to test and can run inside
any worker. The pipeline jobs load data, call these functions, and persist the results.
"""
