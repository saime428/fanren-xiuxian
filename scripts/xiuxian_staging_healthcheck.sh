#!/usr/bin/env bash
set -euo pipefail

# This existing maintenance label validates the live checkout's source files.
# Runtime .env/session/state files are normal here; they must remain untracked.
HERMES_HOME="${HERMES_HOME:-/Users/operator/.hermes}"
PROJECT="$HERMES_HOME/workspace/projects/xiuxian"
STATE_DIR="$HERMES_HOME/workspace/state/xiuxian-staging"
mkdir -p "$STATE_DIR"
exec "$PROJECT/.venv/bin/python3" "$PROJECT/scripts/hermes_shadow_check.py" \
  --root "$PROJECT" --output "$STATE_DIR/healthcheck.json" >"$STATE_DIR/healthcheck.log" 2>&1
