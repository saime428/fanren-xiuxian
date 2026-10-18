#!/bin/bash
set -euo pipefail

if [[ "$#" -ne 1 ]]; then
  echo "Usage: $0 <business-account>" >&2
  exit 64
fi
account="$1"
case "$account" in
  tg9100000001|tg9100000002|tg9100000003|tg9100000004|tg9100000005|tg9100000006)
    ;;
  *)
    echo "Unsupported Tianji Trial business account: $account" >&2
    exit 64
    ;;
esac

cd "$(dirname "${BASH_SOURCE[0]}")/.."
exec ./.venv/bin/python3 -m tianji_trial.coordinator --account "$account"
