#!/usr/bin/env bash
# Create .env from .env.example (if missing) and fill any EMPTY local-infrastructure passwords with
# random values. Never overwrites a value that is already set, and never touches API keys.
set -euo pipefail

cd "$(dirname "$0")/.."
[[ -f .env ]] || cp .env.example .env

for var in POSTGRES_PASSWORD S3_SECRET_ACCESS_KEY ELASTIC_PASSWORD GRAFANA_ADMIN_PASSWORD; do
  if grep -qE "^${var}=$" .env; then
    value="$(openssl rand -hex 16)"
    # Portable in-place edit (BSD and GNU sed differ on -i).
    tmp="$(mktemp)"
    sed "s/^${var}=\$/${var}=${value}/" .env > "$tmp" && mv "$tmp" .env
    echo "generated ${var} in .env"
  fi
done
chmod 600 .env
