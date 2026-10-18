#!/bin/bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
uid="$(id -u)"
label='ai.hermes.xiuxian.doufa_demo_player_02.rotation'
source_plist="scripts/${label}.plist"
installed_plist="${HOME}/Library/LaunchAgents/${label}.plist"

# One explicit command starts a complete campaign. Clear only account-scoped
# pauses that are still below the hard 5/5 quota; the campaign will re-pause
# and stop immediately if the server returns the rolling 24-hour rejection.
./.venv/bin/python3 ./scripts/clear_doufa_manual_pauses.py
cp "$source_plist" "$installed_plist"
launchctl enable "gui/${uid}/${label}"
launchctl bootstrap "gui/${uid}" "$installed_plist" 2>/dev/null || true
# Never kill an already running campaign, including a concurrent activation.
launchctl kickstart "gui/${uid}/${label}"
printf '斗法手动激活完成：%s\n' "$label"
