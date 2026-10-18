#!/bin/bash
set -euo pipefail

# Restore only LaunchAgents declared by the active registry. Never
# bulk-bootstrap every plist found under ~/Library/LaunchAgents.
cd "$(dirname "${BASH_SOURCE[0]}")/.."
uid="$(id -u)"
# The production checkout has its virtualenv; elsewhere (CI, offline tests)
# fall back to $PYTHON or python3 so `--list` still works.
if [[ -x ./.venv/bin/python3 ]]; then python=./.venv/bin/python3; else python="${PYTHON:-python3}"; fi

labels_file="$(mktemp)"
trap 'rm -f "$labels_file"' EXIT

"$python" - <<'PY' >"$labels_file"
import json
from pathlib import Path
from flow_registry import active_entries, launchagent_labels, registry_path

payload = json.loads(registry_path('.').read_text(encoding='utf-8'))
for label in launchagent_labels(active_entries(payload)):
    print(label)
PY

if [[ "${1:-}" == "--list" ]]; then
  cat "$labels_file"
  exit 0
fi

# Validate the entire restore plan before touching any service.
while IFS= read -r label; do
  plist="$HOME/Library/LaunchAgents/${label}.plist"
  if [[ ! -f "$plist" ]]; then
    echo "Missing active plist: $plist" >&2
    exit 1
  fi
done <"$labels_file"

# launchd cannot open a StandardOutPath/StandardErrorPath in a missing
# directory (the job then fails before its own `mkdir -p` runs), so create the
# log directories of every job first.
"$python" - "$labels_file" <<'PY'
import plistlib
import sys
from pathlib import Path

for label in Path(sys.argv[1]).read_text(encoding='utf-8').split():
    plist = plistlib.loads((Path.home() / 'Library/LaunchAgents' / f'{label}.plist').read_bytes())
    for key in ('StandardOutPath', 'StandardErrorPath'):
        if isinstance(plist.get(key), str):
            Path(plist[key]).parent.mkdir(parents=True, exist_ok=True)
PY

# Old fixed-answer jobs reference removed scripts and overlap with the six
# account-bound Xuangu monitors. Keep them disabled across fleet restores.
while IFS= read -r retired; do
  launchctl disable "gui/$uid/$retired"
  if launchctl print "gui/$uid/$retired" >/dev/null 2>&1; then
    launchctl bootout "gui/$uid/$retired"
  fi
done < <("$python" - <<'PY'
from flow_registry import retired_xuangu_launchagent_labels
for label in retired_xuangu_launchagent_labels():
    print(label)
PY
)

while IFS= read -r label; do
  plist="$HOME/Library/LaunchAgents/${label}.plist"
  launchctl enable "gui/$uid/$label" 2>/dev/null || true
  launchctl bootout "gui/$uid/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$uid" "$plist"
  echo "Restored $label"
done <"$labels_file"

# Assert retired duplicate duel schedulers cannot coexist with the canonical
# global rotation label.
for account in tg9100000005 tg9100000003 tg9100000004 tg9100000002 tg9100000006; do
  retired="ai.hermes.xiuxian.${account}.doufa_demo_player_02.send"
  if launchctl print "gui/$uid/$retired" >/dev/null 2>&1; then
    echo "Retired duplicate scheduler loaded: $retired" >&2
    exit 2
  fi
done

echo 'Xiuxian active-registry restore complete'
