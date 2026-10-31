# Deploying Signal Scout

How to run the whole stack on one small VM, and what would change at production scale. This guide
describes a deployment path; the project's measured runs so far were on a local machine (see
`docs/PERFORMANCE.md` for specs).

## Single VM with Docker Compose

A 4 vCPU / 8 GB VM (e.g. an AWS t3.large or a Hetzner CX32) runs everything. Elasticsearch is the
largest consumer (512 MB heap, ~1.5 GB resident).

1. **Install** Docker Engine with the Compose plugin and clone the repo.
2. **Object storage:** create an S3 bucket and an IAM user whose policy allows only that bucket
   (`s3:GetObject`, `s3:PutObject`, `s3:HeadObject`, `s3:ListBucket` on `arn:aws:s3:::<bucket>` and
   `arn:aws:s3:::<bucket>/*`). Set in `.env`:
   ```sh
   S3_ENDPOINT_URL=          # empty = real AWS S3
   S3_REGION=us-west-2
   S3_BUCKET=<bucket>
   S3_ACCESS_KEY_ID=<iam user key>
   S3_SECRET_ACCESS_KEY=<iam user secret>
   ```
   With real S3, the `minio` and `minio-init` services aren't needed.
3. **Secrets:** on a server there is no Keychain, so provide `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`,
   `SLACK_WEBHOOK_URL`, `API_KEY` and `INBOUND_WEBHOOK_SECRET` as environment variables from the host's
   secret store (or a root-only `.env` with `chmod 600`). Never bake them into the image.
4. **Start:**
   ```sh
   make env                                   # random local passwords for Postgres/ES/Grafana
   docker compose up -d --wait postgres elasticsearch
   docker compose --profile app run --rm migrate
   docker compose --profile app run --rm seed
   docker compose --profile app up -d         # api, worker x2, scheduler, mcp, prometheus, grafana
   ```
5. **HTTPS:** put Caddy in front of the API and MCP endpoints. Caddy obtains certificates automatically:
   ```
   scout.example.com {
       reverse_proxy /mcp* localhost:8001
       reverse_proxy localhost:8000
   }
   ```
   Keep Postgres, Elasticsearch, Prometheus and Grafana bound to localhost (as in `docker-compose.yml`)
   and reach Grafana over an SSH tunnel.
6. **Backups:** nightly `pg_dump` to the S3 bucket. Elasticsearch needs no backup: `make reindex`
   rebuilds it from Postgres.

## The image

`ghcr.io/asmipahwa/signal-scout` is built by CI from the multi-stage `Dockerfile` (uv-resolved
virtualenv, `python:3.12-slim`, non-root user). One image, one role per container:
`api | worker | scheduler | mcp | migrate | seed | reindex | backfill | report`.

## What would change at Klue's scale

| Here | At scale | Why |
|---|---|---|
| Postgres `jobs` table + `SKIP LOCKED` | SQS or Kafka | Millions of jobs/day, fan-out to many consumers, replay |
| One Postgres on the VM | Managed Postgres (RDS/Aurora) with read replicas | Durability, failover, read scaling for the API |
| Single-node Elasticsearch | Managed OpenSearch/Elasticsearch, multiple shards + replicas | Availability and index size |
| Two worker containers | Autoscaled workers per job type (enrichment separate from fetching) | LLM-bound and I/O-bound work scale differently |
| One customer workspace | Tenant id on every row, per-tenant indexes or filtered aliases, per-tenant LLM budgets | Isolation and cost attribution |
| Client-side LLM rate limiter in a state file | Central token bucket (Redis) shared by all workers | Many processes, one provider quota |
| Prometheus + Grafana on the box | Managed metrics + Alertmanager → PagerDuty | On-call routing |
