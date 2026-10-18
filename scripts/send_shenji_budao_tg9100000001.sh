#!/bin/bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

source ./config/accounts/tg9100000001.env
export XIU_FLOW_SLUG='shenji_budao'
source ./scripts/lib/account_flow_paths.sh
export TG_MONITOR_TARGETS="${TG_MONITOR_TARGETS:-@example_game_group|-1009400000005}"
export TG_MONITOR_ROOT_TARGETS="${TG_MONITOR_ROOT_TARGETS:--1009400000005}"

export TG_FLOW_SLUG="${XIU_FLOW_SLUG}"
export TG_AUTO_REPLY_MODE='fixed'
export TG_AUTO_REPLY_MESSAGE='.神迹 布道'
# 小世界浩劫: when @demo_player_01 is mentioned, immediately send .神迹 布道.
export TG_TRIGGER_MARKER_TEXT='小世界·天降浩劫|请速速查看 .小世界||小世界·天降浩劫|安抚信徒'
export TG_TRIGGER_TRUSTED_SENDER_IDS='7228205882,7965897083,8735907987,8816935632,8918867302,8944702077,8964348409,8980154525,8788895566,8917921351,8388633812,8861328042'
export TG_TRIGGER_TARGET_USERNAME='demo_player_01'
export TG_TOPIC_MATCH_MODE='any'
# Do not reply to a potentially closed disaster-event thread. Send to the
# configured live forum topic root instead (handled by auto_reply_topic_event).
export TG_REPLY_TO_EVENT='0'
# Bursty group traffic can push a valid disaster notice out of a shallow scan
# before a reconnect completes. Retain a bounded 15-minute recovery window.
export TG_SCAN_LIMIT='1000'
export TG_LOG_SKIPS='0'
export TG_TRIGGER_MAX_EVENT_AGE_SECONDS='900'
export TG_RUN_MAX_SECONDS='90'
# Keep this flow hot: Telegram push is the fast path; history is recovery-only.
export TG_AUTO_REPLY_PERSISTENT='1'
export TG_AUTO_REPLY_RECONCILE_SECONDS='60'
export TG_LOG_IDLE='0'
export PYTHONUNBUFFERED='1'

xiu_ensure_session_authorized
exec ./.venv/bin/python3 -u auto_reply_topic_event.py
