#!/usr/bin/env python3
"""Dispatch one account-scoped artifact retry only after its recorded cooldown."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from command_confirmation import CANONICAL_TARGET, CANONICAL_TOPIC_ID  # noqa: E402
from miniapp_shared.accounts import accounts_for  # noqa: E402
from state_store import FlowLockBusy, flow_lock, is_paused, load_json_dict, persist_json_atomic  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FLOW = "fumofabao_xuantianzhanlingjian"
ACCOUNT = next(iter(accounts_for(FLOW)), None)
MESSAGE = ".抚摸法宝 玄天斩灵剑"
TARGET = CANONICAL_TARGET
TOPIC_ID = CANONICAL_TOPIC_ID
STATE_DIR = ROOT / "state" / "accounts" / ACCOUNT / FLOW
RETRY_PATH = STATE_DIR / "cooldown_retry.json"
WRAPPER = ROOT / "scripts" / f"send_{FLOW}_{ACCOUNT}.sh"
# Share return_deadline_dispatch's kick ledger, label and 120 s window, so a
# sender that stays paused or lock-busy (and so never clears its cooldown
# record) is not relaunched every minute, nor by both dispatchers at once.
DISPATCH_STATE_PATH = ROOT / "state" / "return_deadline_dispatch.json"
SEND_LABEL = f"ai.hermes.xiuxian.{ACCOUNT}.{FLOW}.send"
DEDUPE_SECONDS = 120


def load() -> dict:
    try:
        value = json.loads(RETRY_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:
        print(f"invalid_retry_state={type(exc).__name__}")
        return {}
    return value if isinstance(value, dict) else {}


def main() -> int:
    paused, pause_path = is_paused(STATE_DIR, FLOW)
    if paused:
        print(f"paused via {pause_path.name}")
        return 0
    state = load()
    key = f"{TARGET}|{TOPIC_ID}|{MESSAGE}"
    pending = state.get(key)
    if not isinstance(pending, dict):
        print("no_pending_cooldown")
        return 0
    try:
        not_before = float(pending["not_before_ts"])
    except (KeyError, TypeError, ValueError):
        print("invalid_pending_cooldown")
        return 1
    now = time.time()
    remaining = not_before - now
    if remaining > 0:
        print(f"cooldown_not_due remaining_seconds={int(remaining + 0.999999)}")
        return 0
    try:
        with flow_lock(DISPATCH_STATE_PATH.with_suffix(".lock")):
            dispatch_state = load_json_dict(DISPATCH_STATE_PATH)
            kicks = dispatch_state.get("last_kick_by_label")
            kicks = kicks if isinstance(kicks, dict) else {}
            try:
                since_kick = now - float(kicks.get(SEND_LABEL) or 0)
            except (TypeError, ValueError):
                since_kick = DEDUPE_SECONDS
            if since_kick < DEDUPE_SECONDS:
                print(f"cooldown_due recently_dispatched seconds_ago={int(since_kick)}")
                return 0
            # Reserve the kick under the shared lock; release it before the
            # synchronous sender so other labels can still be dispatched.
            persist_json_atomic(DISPATCH_STATE_PATH, {**dispatch_state, "last_kick_by_label": {**kicks, SEND_LABEL: now}})
    except FlowLockBusy:
        print("cooldown_due dispatcher_busy; skip this tick")
        return 0
    print("cooldown_due dispatching_sender=1")
    completed = subprocess.run(["/bin/bash", str(WRAPPER)], cwd=ROOT, check=False)
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
