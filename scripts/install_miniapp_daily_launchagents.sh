#!/usr/bin/env bash
# Install (or reinstall) the MiniApp daily LaunchAgents and the QYZ battle report:
#   ai.hermes.xiuxian.star-observatory.send   星宫灵圃: 一键安抚 / 一键收集 / 牵引天雷星 (demo_player_04, demo_player_05)
#   ai.hermes.xiuxian.tower-miniapp.daily     琉璃问心塔: every business account, every identity, once per day
#   ai.hermes.xiuxian.journey-miniapp.daily   游历·野外历练 谨慎 x8 from 00:00 Asia/Shanghai (demo_player_01: 示例绑定角色 only)
#   ai.hermes.xiuxian.qyz-battle-report       青元子: one fleet battle report per event through Hermes
#   ai.hermes.xiuxian.pavilion-loop.send      藏娇阁: 双侍妾 远航/代卜/寻图/心劫/拼图 continuous loop
#   ai.hermes.xiuxian.sect-daily              宗门日常: 点卯 / 军报 / 支援慕兰 from 01:00 Asia/Shanghai, staggered per account
#   ai.hermes.xiuxian.fate-cards.daily        天机命脉: draw / choose / quest / settle for every identity from 03:00 Asia/Shanghai, then every 噬金虫 run left today
#
# Usage: scripts/install_miniapp_daily_launchagents.sh [install|uninstall|status]
set -euo pipefail

PROJECT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"
LABELS=(
  ai.hermes.xiuxian.star-observatory.send
  ai.hermes.xiuxian.tower-miniapp.daily
  ai.hermes.xiuxian.journey-miniapp.daily
  ai.hermes.xiuxian.qyz-battle-report
  ai.hermes.xiuxian.sect-daily
  ai.hermes.xiuxian.pavilion-loop.send
  ai.hermes.xiuxian.fate-cards.daily
)
action="${1:-install}"

mkdir -p "$LAUNCH_AGENTS" \
  "$PROJECT/state/star_observatory" "$PROJECT/state/tower_miniapp" "$PROJECT/state/journey_miniapp" \
  "$PROJECT/qingyuanzi-new-source/state/qyz_battle_report" "$PROJECT/state/sect_daily" "$PROJECT/state/pavilion_loop" "$PROJECT/state/fate_cards"

case "$action" in
  install)
    for label in "${LABELS[@]}"; do
      src="$PROJECT/scripts/$label.plist"
      dst="$LAUNCH_AGENTS/$label.plist"
      plutil -lint "$src" >/dev/null
      launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
      install -m 0644 "$src" "$dst"
      launchctl bootstrap "$DOMAIN" "$dst"
      launchctl enable "$DOMAIN/$label"
      echo "installed $label"
    done
    ;;
  uninstall)
    for label in "${LABELS[@]}"; do
      launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
      rm -f "$LAUNCH_AGENTS/$label.plist"
      echo "removed $label"
    done
    ;;
  status)
    for label in "${LABELS[@]}"; do
      if launchctl print "$DOMAIN/$label" >/dev/null 2>&1; then
        state="$(launchctl print "$DOMAIN/$label" | awk -F'= ' '/^\tstate = /{print $2} /last exit code/{print "exit=" $2}' | tr '\n' ' ')"
        echo "$label: loaded ${state}"
      else
        echo "$label: not loaded"
      fi
    done
    ;;
  *)
    echo "usage: $0 [install|uninstall|status]" >&2
    exit 64
    ;;
esac
