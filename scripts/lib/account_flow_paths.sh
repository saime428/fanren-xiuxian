#!/bin/bash
set -euo pipefail

if [[ -z "${XIU_ACCOUNT_SLUG:-}" ]]; then
  echo 'Missing XIU_ACCOUNT_SLUG' >&2
  exit 1
fi

if [[ -z "${XIU_FLOW_SLUG:-}" ]]; then
  echo 'Missing XIU_FLOW_SLUG' >&2
  exit 1
fi

# Private, shared Telegram transport settings.  All account flows source this
# common path helper before constructing a Telethon client.
if [[ -r ./config/accounts/mtproto.env ]]; then
  source ./config/accounts/mtproto.env
fi

XIU_STATE_ROOT="state/accounts/${XIU_ACCOUNT_SLUG}/${XIU_FLOW_SLUG}"
mkdir -p "${XIU_STATE_ROOT}"

export TG_STATE_DIR="${XIU_STATE_ROOT}"
export TG_SESSION_NAME="${TG_SESSION_NAME:-${XIU_STATE_ROOT}/session}"
export TG_LOCK_PATH="${TG_LOCK_PATH:-${XIU_STATE_ROOT}/send.lock}"
export TG_LAST_SUCCESS_PATH="${TG_LAST_SUCCESS_PATH:-${XIU_STATE_ROOT}/last_success.json}"
export TG_LAST_ATTEMPT_PATH="${TG_LAST_ATTEMPT_PATH:-${XIU_STATE_ROOT}/last_attempt.json}"
export TG_COOLDOWN_STATE_PATH="${TG_COOLDOWN_STATE_PATH:-${XIU_STATE_ROOT}/cooldown_retry.json}"
export TG_WATCHDOG_LOG_PATH="${TG_WATCHDOG_LOG_PATH:-${XIU_STATE_ROOT}/watchdog.log}"
export TG_MESSAGE_LOG_PATH="${TG_MESSAGE_LOG_PATH:-${XIU_STATE_ROOT}/messages.jsonl}"
# Canonical outbound route for every Xiuxian command/reply. Listeners may read
# additional verified sources, but no automation may send there. The tracked
# values are placeholders; config/accounts/game.env (written from the accounts
# config by scripts/xiuxian_accounts.py render) names the real game group.
# Assigned, not defaulted, so an account env file can never redirect commands.
XIU_GAME_TARGET='@example_game_group'
XIU_GAME_TOPIC_ID='7310786'
XIU_GAME_SECOND_CHAT_ID='-1009400000005'
if [[ -r ./config/accounts/game.env ]]; then
  source ./config/accounts/game.env
fi
export TG_TARGET="${XIU_GAME_TARGET}"
export TG_TOPIC_ID="${XIU_GAME_TOPIC_ID}"
export TG_REPLY_SCAN_TARGET="${XIU_GAME_TARGET}"
export TG_REPLY_SCAN_TOPIC_ID="${XIU_GAME_TOPIC_ID}"
# Read-only listeners inspect both verified production groups. Commands still
# send only to the canonical TG_TARGET/TG_TOPIC_ID; never broadcast actions.
export TG_MONITOR_TARGETS="${TG_MONITOR_TARGETS:-${XIU_GAME_TARGET}|${XIU_GAME_SECOND_CHAT_ID}}"
# Most command flows send to the game group's forum topic.  The second verified
# group is a plain/root chat, so listener topic filtering must be disabled there.
export TG_MONITOR_ROOT_TARGETS="${TG_MONITOR_ROOT_TARGETS:-${XIU_GAME_SECOND_CHAT_ID}}"
# Global cross-account Telegram send gate: default to 1 message/second shared by all flows.
export TG_GLOBAL_SEND_GATE_STATE_PATH="${TG_GLOBAL_SEND_GATE_STATE_PATH:-state/global_telegram_send_gate.json}"
export TG_GLOBAL_MIN_SEND_SPACING_SECONDS="${TG_GLOBAL_MIN_SEND_SPACING_SECONDS:-1}"

case "${XIU_ACCOUNT_SLUG}" in
  tg9100000001)
    # demo_player_01 must use only its own account-level authoritative session.
    # A per-flow session must never become the seed for every other flow: if
    # that one copy is revoked/corrupted it would fan the failure out account-wide.
    # Assigned, not defaulted, so an env file's fallback cannot override it.
    export TG_SESSION_FALLBACK_NAME='state/accounts/tg9100000001/session'
    ;;
  *)
    # Every other account seeds from its credential source's state/<source>.session
    # (alias wrappers may already have set TG_SESSION_FALLBACK_NAME).
    export TG_SESSION_FALLBACK_NAME="${TG_SESSION_FALLBACK_NAME:-state/${XIU_CREDENTIAL_SOURCE:-${XIU_ACCOUNT_SLUG}}}"
    ;;
esac

xiu_ensure_session_authorized() {
  ./.venv/bin/python3 ./scripts/ensure_session_authorized.py
}

# Called only by migrated send wrappers, before legacy cache/probe handling.
# Code 3 means the registry explicitly uses the old transport. Any MiniApp
# failure exits this invocation, so it can never cause a group-message fallback.
xiu_route_miniapp_command() {
  local miniapp_exit
  if ./.venv/bin/python3 -m miniapp_commands.run_cycle --account "$XIU_ACCOUNT_SLUG" --flow "$XIU_FLOW_SLUG"; then
    exit 0
  else
    miniapp_exit=$?
    if [[ "$miniapp_exit" != "3" ]]; then
      exit "$miniapp_exit"
    fi
  fi
}
