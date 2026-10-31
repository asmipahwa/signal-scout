#!/bin/sh
# Pick the process role. Everything else (config, secrets) comes from environment variables.
set -e
role="${1:-api}"
shift || true
case "$role" in
  api)       exec python -m signal_scout.api "$@" ;;
  worker)    exec python -m signal_scout.worker "$@" ;;
  scheduler) exec python -m signal_scout.scheduler "$@" ;;
  mcp)       exec python -m signal_scout.mcp_server --transport http "$@" ;;
  migrate)   exec alembic upgrade head ;;
  seed)      exec python -m signal_scout.seed seeds/competitors.yaml ;;
  reindex)   exec python -m signal_scout.pipeline.index --full ;;
  backfill)  exec python -m signal_scout.backfill hn "$@" ;;
  report)    exec python -m signal_scout.reports "$@" ;;
  *)         exec "$role" "$@" ;;
esac
