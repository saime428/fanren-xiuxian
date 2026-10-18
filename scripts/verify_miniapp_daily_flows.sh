#!/usr/bin/env bash
# Offline + read-only verification for the three MiniApp daily flows.
# Safe to run any time: no game mutation, no launchd change.
#   scripts/verify_miniapp_daily_flows.sh            # unit tests, lint, offline checks
#   scripts/verify_miniapp_daily_flows.sh --live     # also read-only live dry-runs per flow
set -uo pipefail

PROJECT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$PROJECT/.venv/bin/python3"
cd "$PROJECT"
status=0
step() { printf '\n=== %s ===\n' "$1"; }

step "unit tests (new MiniApp packages)"
"$PY" -m unittest \
  miniapp_shared.test_miniapp_shared miniapp_shared.test_notify \
  star_observatory.test_protocol star_observatory.test_runtime star_observatory.test_adapter \
  star_observatory.test_runner star_observatory.test_coordinator star_observatory.test_entrypoint \
  star_observatory.test_state sect_daily.test_sect_daily pavilion_loop.test_pavilion_loop pavilion_loop.test_heart_trial tianji_trial.test_report \
  tower_miniapp.test_protocol tower_miniapp.test_adapter tower_miniapp.test_runtime \
  tower_miniapp.test_runner tower_miniapp.test_coordinator tower_miniapp.test_state \
  journey_miniapp.test_protocol journey_miniapp.test_runtime journey_miniapp.test_runner \
  2>&1 | tail -n 40 || status=1
[[ ${PIPESTATUS[0]} -eq 0 ]] || status=1

step "repository regression suite (root test_*.py)"
"$PY" -m unittest discover -s . -p 'test_*.py' 2>&1 | tail -n 15
[[ ${PIPESTATUS[0]} -eq 0 ]] || status=1

step "fishing notification tests"
( cd fishing && ../.venv/bin/python3 -m unittest test_fishing_notification test_run_miniapp_fishing_cycle 2>&1 | tail -n 8 )
[[ ${PIPESTATUS[0]} -eq 0 ]] || status=1

step "plist lint + install script syntax"
for label in ai.hermes.xiuxian.star-observatory.send ai.hermes.xiuxian.tower-miniapp.daily ai.hermes.xiuxian.journey-miniapp.daily ai.hermes.xiuxian.qyz-battle-report ai.hermes.xiuxian.sect-daily ai.hermes.xiuxian.pavilion-loop.send; do
  plutil -lint "scripts/$label.plist" || status=1
done
bash -n scripts/install_miniapp_daily_launchagents.sh && echo "install script: syntax ok" || status=1

step "qyz battle report + v20 verification tests"
( cd qingyuanzi-new-source && ../.venv/bin/python3 -m unittest test_qyz_battle_report test_qyz_verification_fallback test_qyz_verification test_qyz_host_runtime test_qyz_capabilities 2>&1 | tail -n 12 )
[[ ${PIPESTATUS[0]} -eq 0 ]] || status=1

step "offline configuration checks"
"$PY" -m star_observatory.run_cycle --check || status=1
"$PY" -m sect_daily.run_cycle --check || status=1
"$PY" -m pavilion_loop.run_cycle --check || status=1
"$PY" -m tower_miniapp.run_cycle --check || status=1
"$PY" -m journey_miniapp.run_cycle --check || status=1

step "active registry parses and names the three helpers"
"$PY" - <<'EOF' || status=1
import json
from flow_registry import active_entries, registry_path
entries = active_entries(json.load(open(registry_path("."))))
labels = [e["label"] for e in entries if e.get("kinds") == ["helper"]]
wanted = ["ai.hermes.xiuxian.star-observatory.send", "ai.hermes.xiuxian.tower-miniapp.daily",
          "ai.hermes.xiuxian.journey-miniapp.daily", "ai.hermes.xiuxian.qyz-battle-report"]
missing = [w for w in wanted if w not in labels]
print("helpers:", len(labels), "missing:", missing)
raise SystemExit(1 if missing else 0)
EOF

step "offline shadow check (compile/lint every tracked + untracked source)"
"$PY" scripts/hermes_shadow_check.py --root "$PROJECT" --output /tmp/xiuxian-shadow-check.json || status=1

if [[ "${1:-}" == "--live" ]]; then
  step "LIVE read-only dry-run: 星宫灵圃"
  "$PY" -m star_observatory.run_cycle --dry-run || status=1
  step "LIVE read-only dry-run: 琉璃问心塔"
  "$PY" -m tower_miniapp.run_cycle --dry-run || status=1
  step "LIVE read-only dry-run: 游历"
  "$PY" -m journey_miniapp.run_cycle --dry-run || status=1
fi

step "result"
if [[ $status -eq 0 ]]; then echo "ALL CHECKS PASSED"; else echo "SOME CHECKS FAILED (see above)"; fi
exit $status
