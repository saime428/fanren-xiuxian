#!/usr/bin/env python3
"""Kick exact account/flow senders when a server-confirmed countdown becomes due."""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from flow_registry import active_entries, kickstart_argv, launchagent_label, registry_path as default_registry_path
from command_confirmation import UNCONFIRMED_SEND_STATUSES, valid_receipt_identity, validate_command_identity
from state_store import FlowLockBusy, flow_lock, is_paused, persist_json_atomic
from scripts.interval_flow_dispatch import dispatch_intervals
from tianji_trial.deadlines import collect_retry_labels as collect_tianji_retry_labels


FLOWS = ("shendubiguan", "yuanyingchuqiao")
# Ordinary periodic send_topic_message flows. Event replies, manual duels and
# probe-controlled return flows have their own deadlines and must not use an
# old command result as permission to start another action.
PERIODIC_COMMANDS = {
    "fumofabao_qingzhufengyunjian_shenlei": ".抚摸法宝 青竹蜂云剑（神雷版）",
    "fumofabao_xuantianzhanlingjian": ".抚摸法宝 玄天斩灵剑",
    "tanxunliefeng": ".探寻裂缝",
    "wendao": ".问道",
    "xuexishanlin": ".血洗山林",
    "zhaohuanmoying": ".召唤魔影",
}


def load_dict(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def collect_due_labels(
    state_root: Path,
    *,
    now_ts: float | None = None,
    lead_seconds: int = 10,
    overdue_max_seconds: int = 300,
    dedupe_seconds: int = 120,
    registry_path: Path | None = None,
) -> list[str]:
    now = time.time() if now_ts is None else now_ts
    dispatch_state_path = state_root / "return_deadline_dispatch.json"
    dispatch_state = load_dict(dispatch_state_path)
    last_kick_by_label = dispatch_state.get("last_kick_by_label") or {}
    due: list[str] = []
    registered = {(entry["account"], entry["flow"]) for entry in active_entries(
        load_dict(Path(registry_path or default_registry_path(PROJECT_ROOT))))
        if "send" in entry["kinds"] and entry.get("transport") != "miniapp"}
    for account_dir in sorted((state_root / "accounts").glob("tg*")):
        if not account_dir.is_dir():
            continue
        for flow in FLOWS:
            if (account_dir.name, flow) not in registered or is_paused(account_dir / flow, flow)[0]:
                continue
            payload = load_dict(account_dir / flow / "status_probe.json")
            if (payload.get("status") != "countdown_pending"
                    or not valid_receipt_identity(payload, actor_id=int(account_dir.name[2:]), flow=flow)):
                continue
            try:
                not_before_ts = float(payload.get("not_before_ts") or 0)
            except (TypeError, ValueError):
                continue
            if not_before_ts <= 0:
                continue
            seconds_until_due = not_before_ts - now
            if seconds_until_due > lead_seconds or seconds_until_due < -overdue_max_seconds:
                continue
            label = f"ai.hermes.xiuxian.{account_dir.name}.{flow}.send"
            try:
                last_kick = float(last_kick_by_label.get(label) or 0)
            except (TypeError, ValueError):
                last_kick = 0
            if last_kick > 0 and now - last_kick < dedupe_seconds:
                continue
            due.append(label)
    return due


def collect_unconfirmed_labels(state_root: Path, *, registry_path=None, now_ts=None) -> list[str]:
    """Resume enabled senders even when their ordinary cadence is several hours.

    Each sender remains the sole writer of its command state. A non-forced
    kickstart cannot interrupt a running sender or bypass its send lock.
    """
    now = time.time() if now_ts is None else now_ts
    registry = load_dict(Path(registry_path or default_registry_path(PROJECT_ROOT)))
    entries = active_entries(registry)
    kicks = load_dict(state_root / "return_deadline_dispatch.json").get("last_kick_by_label") or {}
    due = []
    for entry in entries:
        if ("send" not in entry["kinds"] or entry["flow"] == "doufa_demo_player_02"
                or entry.get("transport") == "miniapp"):
            continue
        directory = state_root / "accounts" / entry["account"] / entry["flow"]
        if is_paused(directory, entry["flow"])[0]:
            continue
        pending = next((row for name in ("command_pending.json", "status_probe_pending.json")
                        if (row := load_dict(directory / name)).get("pending")), {})
        attempt = load_dict(directory / "last_attempt.json")
        if pending:
            if not pending.get("pending") or pending.get("flow_slug") != entry["flow"]:
                continue
            try:
                validate_command_identity(pending.get("target"), pending.get("topic_id"), pending.get("actor_id"),
                                          expected_account=entry["account"])
            except (ValueError, TypeError):
                continue
            deadline = pending.get("next_retry_ts")
        else:
            status = attempt.get("status")
            if not (status in UNCONFIRMED_SEND_STATUSES | {"action_timeout", "probe_timeout",
                    "command_send_unknown", "command_sent_waiting_reply", "sent_unconfirmed_assume_consumed"}
                    and not attempt.get("acked") and not attempt.get("reply_message_id")):
                continue
            try:
                deadline = attempt.get("retry_not_before_ts") or float(attempt.get("actual_send_ts") or attempt.get("ts") or now) + 120
            except (TypeError, ValueError):
                continue
        try:
            ready = float(deadline or 0) > 0 and float(deadline) <= now
        except (ValueError, TypeError):
            ready = False
        label = launchagent_label(entry, "send")
        try:
            elapsed = now - float(kicks.get(label) or 0)
        except (TypeError, ValueError):
            elapsed = 30
        if ready and elapsed >= 30:
            due.append(label)
    return sorted(set(due))


def collect_result_labels(state_root: Path, *, registry_path=None, now_ts=None,
                          dedupe_seconds=120) -> list[str]:
    """Wake periodic senders at their persisted official result deadline.

    Scheduler intervals restart with the supervisor and are only a fallback.
    This path covers both timed refusals and the next cycle after a success.
    It reads business state; only the existing account wrapper may change it.
    """
    now = time.time() if now_ts is None else now_ts
    entries = active_entries(load_dict(Path(registry_path or default_registry_path(PROJECT_ROOT))))
    helpers = {entry["label"] for entry in entries if "helper" in entry["kinds"]}
    kicks = load_dict(state_root / "return_deadline_dispatch.json").get("last_kick_by_label") or {}
    due = []
    for entry in entries:
        flow, account = entry["flow"], entry["account"]
        if entry.get("transport") == "miniapp" and "send" in entry["kinds"]:
            if flow == "shenji_budao":
                continue  # Its persistent monitor owns the event deadline.
            from miniapp_commands.catalog import STATE_FILE, VERSION, ACCOUNTS
            directory = state_root / "accounts" / account / flow
            state = load_dict(directory / STATE_FILE)
            if (account not in ACCOUNTS.get(flow, ()) or is_paused(directory, flow)[0]
                    or state.get("account") != account or state.get("flow") != flow
                    or state.get("runtime_version") != VERSION):
                continue
            from miniapp_commands.protocol import number
            deadline = number(state.get("next_due_ts"))
            label = launchagent_label(entry, "send")
            last_kick = number(kicks.get(label)) or 0
            if deadline and deadline <= now and now-last_kick >= dedupe_seconds:
                due.append(label)
            continue
        if ("send" not in entry["kinds"] or flow not in PERIODIC_COMMANDS
                or entry.get("interval_dispatch_seconds") is not None):
            continue  # interval-owned jobs reconcile their receipts under their lifetime lock
        directory = state_root / "accounts" / account / flow
        if is_paused(directory, flow)[0] or load_dict(directory / "command_pending.json").get("pending"):
            continue
        result = load_dict(directory / "last_result.json")
        try:
            trusted = valid_receipt_identity(result, actor_id=int(account[2:]), flow=flow,
                                             command=PERIODIC_COMMANDS[flow])
        except (TypeError, ValueError):
            continue
        if (not trusted or result.get("failed") or result.get("reset_triggered_after_send")
                or result.get("status") not in {"acknowledged", "cooldown_pending", "settled"}):
            continue
        # The existing artifact-specific helper already owns its cooldown wake.
        # Its successful-result cadence still needs this shared deadline path.
        if (result.get("status") == "cooldown_pending"
                and launchagent_label(entry, "cooldown_dispatch") in helpers):
            continue
        label = launchagent_label(entry, "send")
        raw_due = result.get("next_due_ts")
        try:
            deadline = float(raw_due)
            last_kick = float(kicks.get(label) or 0)
        except (TypeError, ValueError):
            continue
        if (not isinstance(raw_due, bool) and math.isfinite(deadline) and 0 < deadline <= now
                and math.isfinite(last_kick) and now - last_kick >= dedupe_seconds):
            due.append(label)
    return sorted(set(due))


def collect_yuanshen_labels(state_root: Path, *, now_ts: float | None = None) -> list[str]:
    from yuanshen_return import TRAIN_LABEL, training_dispatch_due

    now = time.time() if now_ts is None else now_ts
    kicks = load_dict(state_root / "return_deadline_dispatch.json").get("last_kick_by_label") or {}
    try:
        if now - float(kicks.get(TRAIN_LABEL, 0)) < 30:
            return []
    except (TypeError, ValueError):
        return []
    return [TRAIN_LABEL] if training_dispatch_due(state_root.parent, now) else []


def main() -> int:
    state_root = Path(os.environ.get("XIU_STATE_ROOT", "state"))
    dispatch_state_path = state_root / "return_deadline_dispatch.json"
    try:
        # Both dispatchers must collect, deduplicate and update the ledger
        # under this lock, including the successful launchctl result.
        with flow_lock(dispatch_state_path.with_suffix(".lock")):
            return _dispatch_due(state_root)
    except FlowLockBusy:
        print("deadline dispatcher busy; skip this tick")
        return 0


def _dispatch_due(state_root: Path) -> int:
    now = time.time()
    try:
        from automation_dossiers import refresh_automation_dossiers
        refresh_automation_dossiers(PROJECT_ROOT,state_root,now=now)
    except Exception as exc:
        print(f'task dossier refresh failed: {type(exc).__name__}')
    labels = sorted(set(collect_due_labels(state_root, now_ts=now) +
                        collect_unconfirmed_labels(state_root, now_ts=now) +
                        collect_result_labels(state_root, now_ts=now) +
                        collect_tianji_retry_labels(PROJECT_ROOT, state_root, now=now) +
                        collect_yuanshen_labels(state_root, now_ts=now)))
    registry = load_dict(default_registry_path(PROJECT_ROOT))
    handled = dispatch_intervals(PROJECT_ROOT, state_root, registry, now=now, retry_labels=labels)
    labels = [label for label in labels if label not in handled]
    if not labels:
        return 0
    dispatch_state_path = state_root / "return_deadline_dispatch.json"
    dispatch_state = load_dict(dispatch_state_path)
    last_kick_by_label = dispatch_state.get("last_kick_by_label")
    if not isinstance(last_kick_by_label, dict):
        last_kick_by_label = {}
    for label in labels:
        result = subprocess.run(
            kickstart_argv(label),
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            last_kick_by_label[label] = now
            print(f"deadline due; kicked {label}")
        else:
            error = (result.stderr or result.stdout or "unknown launchctl failure").strip()
            print(f"deadline kick failed label={label} rc={result.returncode}: {error}")
    dispatch_state["last_kick_by_label"] = last_kick_by_label
    dispatch_state["ts"] = time.time()
    persist_json_atomic(dispatch_state_path, dispatch_state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
