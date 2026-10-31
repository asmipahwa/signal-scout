#!/usr/bin/env bash
# Run a command with API keys loaded from the macOS Keychain.
#
# Keys live only in the Keychain and in the environment of the command you run: they are never
# written to a file in the repo and never printed. Store a key once with:
#   security add-generic-password -a "$USER" -s signal-scout-gemini -w
#   security add-generic-password -a "$USER" -s signal-scout-anthropic -w
#
# Usage: scripts/with-secrets.sh <command> [args...]
set -euo pipefail

load_key() {
  local var=$1 service=$2 value
  if [[ -z "${!var:-}" ]] && value=$(security find-generic-password -a "$USER" -s "$service" -w 2>/dev/null); then
    export "$var=$value"
  fi
}

load_key GEMINI_API_KEY signal-scout-gemini
load_key ANTHROPIC_API_KEY signal-scout-anthropic
exec "$@"
