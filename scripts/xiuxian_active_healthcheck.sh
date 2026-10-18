#!/usr/bin/env bash
set -euo pipefail

workspace="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
python="$workspace/.venv/bin/python"
state_file="${XIU_HEALTHCHECK_OUTPUT:-/Users/operator/.hermes/workspace/state/xiuxian-healthcheck.json}"
notification_state="${XIU_HEALTHCHECK_NOTIFICATION_STATE:-/Users/operator/.hermes/workspace/state/xiuxian-healthcheck-notification.json}"
confirmation_snapshot="$(mktemp "${TMPDIR:-/tmp}/xiuxian-health-confirmation.XXXXXX")"
trap 'rm -f -- "$confirmation_snapshot"' EXIT

run_check() {
  XIU_HEALTHCHECK_OUTPUT="$1" "$python" "$workspace/scripts/xiuxian_healthcheck.py" >/dev/null
}

# Keep both snapshots so only the same failure observed twice can notify.
run_check "$confirmation_snapshot"
sleep 1
run_check "$state_file"

# Healthy snapshots are silent, but must still clear the notification journal
# so the same failure can alert again after a verified recovery.
"$python" "$workspace/scripts/xiuxian_health_alert.py" "$state_file" "$notification_state" \
  --confirmation-snapshot "$confirmation_snapshot"
