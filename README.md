# Signal Scout: AI Competitor Insights Pipeline

Watches competitors' news, press releases, and website changes, uses an LLM (Claude or Gemini) to turn them into ranked insights, and serves them over search, Slack alerts, and an MCP server for AI agents.

> **Status: early development (Sprint 1, prototype).** Numbers will appear here only after they are measured, with evidence in `docs/results/`.

**LLM provider:** Claude adapter built and tested; production run on Gemini.

## Swapping providers

Enrichment goes through one `LLMClient` interface with two real adapters (Gemini via `google-genai`, Claude via `anthropic`). Both return the same validated `Enrichment`, so switching is one environment variable:

```sh
LLM_PROVIDER=claude   # or gemini (default); optionally LLM_MODEL=<model id>
```

## Running the pipeline locally (Sprint 2: foundation)

Needs Docker and [uv](https://docs.astral.sh/uv/).

```sh
make up        # creates .env with random local passwords, starts Postgres 16, Elasticsearch 8, MinIO
make migrate   # alembic upgrade head
make seed      # 8 competitors, 22 sources from seeds/competitors.yaml (idempotent)
make scheduler # enqueues fetch_source jobs as sources come due
make worker    # claims jobs from the Postgres queue and ingests feeds
make test      # unit tests;  make it  # integration tests (testcontainers);  make lint
make down      # stop the stack (data volumes are kept)
```

How ingestion works: the scheduler enqueues a `fetch_source` job per due source into a Postgres queue
(`SELECT ... FOR UPDATE SKIP LOCKED`). A worker fetches the feed politely (identifying User-Agent,
ETag/If-Modified-Since, per-domain token bucket, retries with backoff and jitter, a circuit breaker
that disables a source after repeated failures), stores the raw item and extracted text gzipped and
content-addressed in S3 (`raw/<yyyy>/<mm>/<dd>/<sha256>.html.gz`, `text/<sha256>.txt.gz`), and inserts
deduplicated signal rows (metadata and a short excerpt only). Every job writes a `job_runs` row, and its
JSON log lines carry the same `run_id`.

Local dev uses `bitnamilegacy/minio` because MinIO no longer publishes images to Docker Hub or Quay;
it is a frozen build, fine for development only.

## License

MIT
