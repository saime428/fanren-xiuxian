#!/usr/bin/env python3
from __future__ import annotations

import json
import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from flow_registry import kickstart_argv, registry_path
from reply_parsing import slugify_message
import state_store
from state_store import load_json_dict
from command_confirmation import UNCONFIRMED_SEND_STATUSES, valid_receipt_identity, validate_command_identity

ACTIVE_FLOWS_PATH = registry_path(".")

FLOW_SLUG = os.getenv("TG_FLOW_SLUG") or slugify_message(os.getenv("TG_MESSAGE", ".闭关修炼"))
STATE_DIR = Path(os.getenv("TG_STATE_DIR", "state"))
STATE_PATH = Path(os.getenv("TG_LAST_SUCCESS_PATH", str(STATE_DIR / f"last_success_{FLOW_SLUG}.json")))
ATTEMPT_PATH = Path(os.getenv("TG_LAST_ATTEMPT_PATH", str(STATE_DIR / f"last_attempt_{FLOW_SLUG}.json")))
LOG_PATH = Path(os.getenv("TG_WATCHDOG_LOG_PATH", str(STATE_DIR / f"watchdog_{FLOW_SLUG}.log")))
LOCK_PATH = Path(os.getenv("TG_LOCK_PATH", str(STATE_DIR / f"send_{FLOW_SLUG}.lock")))
COOLDOWN_STATE_PATH = Path(os.getenv("TG_COOLDOWN_STATE_PATH", str(STATE_DIR / f"cooldown_retry_{FLOW_SLUG}.json")))
# State-machine flows (deep retreat / yuanying) persist their authoritative
# bot-derived countdown separately from the last command success.  A current,
# matching future countdown is healthy waiting, not a stale-sender failure.
STATUS_PROBE_PATH = Path(os.getenv("TG_STATUS_PROBE_PATH", str(STATE_DIR / "status_probe.json")))
MAX_AGE_SECONDS = int(os.getenv("TG_WATCHDOG_MAX_AGE_SECONDS", str(20 * 60)))
LOCK_STALE_SECONDS = int(os.getenv("TG_WATCHDOG_LOCK_STALE_SECONDS", str(10 * 60)))
AUTO_HEAL_STALE_SEND = os.getenv("TG_WATCHDOG_AUTO_HEAL_STALE_SEND", "1") == "1"
REQUIRE_SUCCESS_STATE = os.getenv("TG_WATCHDOG_REQUIRE_SUCCESS_STATE", "1") == "1"
# Return-observer watchdogs judge the observer's own scan evidence
# (observer_health.json) instead of the sender's receipts.
OBSERVER_MODE = os.getenv("TG_WATCHDOG_MODE", "") == "observer"

# Attempt statuses that mean the sender is legitimately mid-flight or waiting.
PENDING_ATTEMPT_STATUSES = UNCONFIRMED_SEND_STATUSES | {"cooldown_pending", "skip_duplicate_guard", "action_timeout", "probe_timeout"}


def infer_send_launchd_label() -> str:
    explicit = os.getenv("TG_SEND_LAUNCHD_LABEL", "").strip()
    if explicit:
        return explicit
    account_slug = os.getenv("XIU_ACCOUNT_SLUG", "").strip()
    flow_slug = os.getenv("TG_FLOW_SLUG", FLOW_SLUG).strip() or FLOW_SLUG
    parts = ["ai", "hermes", "xiuxian"]
    if account_slug:
        parts.append(account_slug)
    parts.extend([flow_slug, "send"])
    return ".".join(parts)


SEND_LAUNCHD_LABEL = infer_send_launchd_label()


def flow_enabled_in_registry(kind: str | None = None) -> tuple[bool | None, str]:
    account_slug = os.getenv("XIU_ACCOUNT_SLUG", "").strip()
    flow_slug = os.getenv("TG_FLOW_SLUG", FLOW_SLUG).strip() or FLOW_SLUG
    if not account_slug:
        return None, "no account-scoped registry rule"
    if not ACTIVE_FLOWS_PATH.exists():
        return None, f"missing {ACTIVE_FLOWS_PATH}"
    try:
        payload = json.loads(ACTIVE_FLOWS_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, f"failed to read {ACTIVE_FLOWS_PATH}: {exc}"
    flows = payload.get("flows") if isinstance(payload, dict) else None
    if not isinstance(flows, list):
        return None, f"invalid {ACTIVE_FLOWS_PATH}"
    matched = [
        item for item in flows
        if isinstance(item, dict)
        and str(item.get("account") or "").strip() == account_slug
        and str(item.get("flow") or "").strip() == flow_slug
    ]
    if not matched:
        return False, f"{account_slug}/{flow_slug} not present in {ACTIVE_FLOWS_PATH}"
    enabled = any(bool(item.get("enabled", True)) and (kind is None or kind in (item.get("kinds") or []))
                  for item in matched)
    scope = f"{account_slug}/{flow_slug}" + (f" kind={kind}" if kind else "")
    return enabled, f"{scope} enabled={enabled} in {ACTIVE_FLOWS_PATH}"


def is_paused() -> tuple[bool, Path]:
    # pause_<flow> / pause_all in the flow directory, the account directory
    # (state/accounts/<account>/pause_all pauses every flow of that account)
    # or the global state root. An observer also honors its own pause slug.
    slugs = (FLOW_SLUG, f"{FLOW_SLUG}_observer") if OBSERVER_MODE else (FLOW_SLUG,)
    for slug in slugs:
        paused, path = state_store.is_paused(STATE_DIR, slug, extra_dirs=(STATE_DIR.parent, Path("state")))
        if paused:
            return True, path
    return False, STATE_DIR / f"pause_{FLOW_SLUG}"


def fail(msg: str) -> int:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] FAIL {msg}\n")
    print(msg)
    return 1


def ok(msg: str) -> int:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] OK {msg}\n")
    print(msg)
    return 0


def current_state_key(topic_id: Any, message: Any, target: Any | None = None) -> str:
    effective_target = target if target not in (None, "") else os.getenv("TG_TARGET", "")
    return f"{effective_target}|{topic_id}|{message}"


def format_attempt_summary(data: dict[str, Any]) -> str:
    if not data:
        return "status=unknown"
    parts = [f"status={data.get('status', 'unknown')}"]
    sent_message_id = data.get("sent_message_id", data.get("message_id"))
    if sent_message_id is not None:
        parts.append(f"sent_message_id={sent_message_id}")
    if data.get("ack_reply_message_id") is not None:
        parts.append(f"ack_reply_message_id={data.get('ack_reply_message_id')}")
    if data.get("resent_message_id") is not None:
        parts.append(f"resent_message_id={data.get('resent_message_id')}")
    if data.get("completion_reason"):
        parts.append(f"reason={data.get('completion_reason')}")
    return " ".join(parts)


def attempt_is_recent_and_pending(data: dict[str, Any], now: float) -> tuple[bool, str]:
    if not data:
        return False, ""
    status = str(data.get("status") or "")
    ts = float(data.get("ts") or data.get("actual_send_ts") or 0)
    age = now - ts if ts > 0 else None
    if age is None or age > MAX_AGE_SECONDS:
        return False, ""
    if status == "cooldown_pending":
        return True, f"Healthy waiting state: cooldown-pending attempt age={int(age)}s {format_attempt_summary(data)}"
    if status == "skip_duplicate_guard":
        return True, f"Healthy waiting state: guard-skip attempt age={int(age)}s {format_attempt_summary(data)}"
    if status in PENDING_ATTEMPT_STATUSES:
        return True, f"Healthy waiting state: pending attempt age={int(age)}s {format_attempt_summary(data)}"
    return False, ""


def compute_lock_age(lock_meta: dict[str, Any], now: float) -> float | None:
    started_at_ts = float(lock_meta.get("started_at_ts") or 0)
    if started_at_ts > 0:
        return now - started_at_ts
    # Corrupt/empty metadata must not make the lock immortal: fall back to
    # the lock file's mtime.
    try:
        return now - LOCK_PATH.stat().st_mtime
    except OSError:
        return None


def lock_stale_threshold(lock_meta: dict[str, Any]) -> int:
    run_max_seconds = int(lock_meta.get("run_max_seconds") or 0)
    return max(LOCK_STALE_SECONDS, run_max_seconds + 180) if run_max_seconds > 0 else LOCK_STALE_SECONDS


def pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def pid_looks_like_sender(pid: int) -> bool:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
        )
    except Exception:
        return True  # cannot verify; keep legacy kill behavior
    command = (result.stdout or "").strip()
    if not command:
        return False
    return "send_topic_message" in command or "send_shendubiguan" in command or "python" in command.lower()


def maybe_heal_stale_send(lock_meta: dict[str, Any], reason: str) -> str:
    if not AUTO_HEAL_STALE_SEND:
        return "auto-heal disabled"

    # A watchdog is not the writer of command/cooldown state and must not kill
    # an in-flight sender. launchd and the sender's flock serialize a safe kick.
    actions: list[str] = []
    if SEND_LAUNCHD_LABEL:
        domain = f"gui/{os.getuid()}/{SEND_LAUNCHD_LABEL}"
        try:
            result = subprocess.run(
                kickstart_argv(SEND_LAUNCHD_LABEL),
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                actions.append(f"kickstarted {domain}")
            else:
                err = (result.stderr or result.stdout or "").strip()
                actions.append(f"kickstart failed for {domain}: {err or 'unknown error'}")
        except Exception as exc:
            actions.append(f"kickstart exception for {domain}: {exc}")

    if not actions:
        actions.append("no action taken")
    return f"{reason}; " + "; ".join(actions)


def find_pending_cooldown(cooldown_state: dict[str, Any], *sources: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(cooldown_state, dict):
        return None
    for source in sources:
        if not source:
            continue
        key = current_state_key(source.get("topic_id"), source.get("message"), source.get("target"))
        pending = cooldown_state.get(key)
        if isinstance(pending, dict):
            return pending
    # Fallback: match on topic+message regardless of how target was recorded.
    for source in sources:
        if not source:
            continue
        wanted_topic = str(source.get("topic_id"))
        wanted_message = str(source.get("message"))
        for key, value in cooldown_state.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            parts = key.split("|", 2)
            if len(parts) != 3:
                continue
            _, topic_part, message_part = parts
            if topic_part == wanted_topic and message_part == wanted_message:
                return value
    return None


def fresh_flow_countdown(now: float) -> dict[str, Any] | None:
    """Return a valid future bot-derived countdown for this exact account+flow."""
    probe = load_json_dict(STATUS_PROBE_PATH)
    if str(probe.get("status") or "") != "countdown_pending" or not verified_result(probe):
        return None
    if str(probe.get("flow_slug") or "") != FLOW_SLUG:
        return None
    try:
        not_before_ts = float(probe.get("not_before_ts") or 0)
        observed_ts = float(probe.get("ts") or 0)
    except (TypeError, ValueError):
        return None
    if not_before_ts <= now or observed_ts <= 0 or observed_ts > now + 60:
        return None
    target = str(probe.get("target") or "")
    expected_target = str(os.getenv("TG_TARGET", "") or "")
    if expected_target and target and target != expected_target:
        return None
    return probe


def warn(msg: str) -> int:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as stream:
        stream.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] WARN {msg}\n")
    print(msg)
    return 0


def send_lock_is_held() -> bool:
    try:
        with LOCK_PATH.open("r+") as lease:
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(lease, fcntl.LOCK_UN)
            return False
    except FileNotFoundError:
        return False


def verified_result(data):
    account = os.getenv("XIU_ACCOUNT_SLUG", "")
    return (account.removeprefix("tg").isdigit()
            and valid_receipt_identity(data, actor_id=int(account[2:]), flow=FLOW_SLUG))


def as_ts(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def observer_health(now: float) -> int:
    """Report a stale or misattributed return observer; never kill or restart it."""
    path = STATE_DIR / "observer_health.json"
    data = load_json_dict(path)
    identity = (data.get("account"), data.get("flow"), data.get("kind"))
    if not data:
        return fail(f"Observer health missing: {path}")
    if identity != (os.getenv("XIU_ACCOUNT_SLUG", ""), FLOW_SLUG, "return_observer"):
        return fail(f"Observer health identity mismatch: {identity}")
    status = data.get("status")
    success_ts = as_ts(data.get("last_success_ts"))
    if not success_ts:
        started_age = now - as_ts(data.get("started_at_ts"))
        if 0 <= started_age <= MAX_AGE_SECONDS:
            return warn(f"Observer starting {int(started_age)}s ago; no verified scan yet status={status}")
        return fail(f"Observer has never completed a verified scan status={status}")
    age = now - success_ts
    if not -60 <= age <= MAX_AGE_SECONDS:
        return fail(f"Observer stale: last verified scan age={int(age)}s status={status} "
                    f"error={data.get('error_stage')}/{data.get('error_type')}")
    if status == "failed":
        return warn(f"Observer cycle failed after a verified scan age={int(age)}s "
                    f"error={data.get('error_stage')}/{data.get('error_type')}")
    return ok(f"Observer verified scan age={int(age)}s status={status}")


def main() -> int:
    now = time.time()
    enabled, enabled_reason = flow_enabled_in_registry("return_observer" if OBSERVER_MODE else None)
    if enabled is False:
        return ok(f"Disabled flow registry skip: {enabled_reason}")
    paused, pause_path = is_paused()
    if paused:
        return ok(f"Paused via {pause_path}; watchdog skip")
    if OBSERVER_MODE:
        return observer_health(now)
    held = send_lock_is_held()
    lock_meta = load_json_dict(LOCK_PATH)
    lock_age = compute_lock_age(lock_meta, now) if held else None
    if held and lock_age is not None and lock_age > lock_stale_threshold(lock_meta):
        return fail(maybe_heal_stale_send(lock_meta, f"Held send lock is stale: age={int(lock_age)}s"))
    if not REQUIRE_SUCCESS_STATE:
        return ok("Watchdog lease check complete; business confirmation is owned by the event monitor")

    from miniapp_commands.catalog import migrated
    account = os.getenv("XIU_ACCOUNT_SLUG", "")
    if migrated(Path(__file__).resolve().parent, account, FLOW_SLUG):
        from miniapp_commands.receipts import health
        severity, status, note, due = health(STATE_DIR, account, FLOW_SLUG, now)
        return {"ok": ok, "warn": warn, "bad": fail}[severity](
            f"MiniApp {status}: {note}; next_due={due}")

    for filename in ("command_pending.json", "status_probe_pending.json"):
        pending = load_json_dict(STATE_DIR / filename)
        if not pending.get("pending"):
            continue
        try:
            retry_at = float(pending.get("next_retry_ts") or 0)
        except (ValueError, TypeError):
            return fail("Invalid unconfirmed-command retry deadline")
        if pending.get("flow_slug") != FLOW_SLUG:
            return fail("Unconfirmed-command flow identity mismatch")
        try:
            validate_command_identity(pending.get("target"), pending.get("topic_id"), pending.get("actor_id"),
                                      expected_account=os.getenv("XIU_ACCOUNT_SLUG", ""))
        except (ValueError, TypeError):
            return fail("Unconfirmed-command account/topic identity mismatch")
        if retry_at <= 0 or now > retry_at + 300:
            return fail(maybe_heal_stale_send({}, "Unconfirmed-command retry is overdue"))
        return warn(f"Awaiting official business reply; retry_at={retry_at}; success remains unconfirmed")

    attempt = load_json_dict(ATTEMPT_PATH)
    result = load_json_dict(STATE_DIR / "last_result.json")
    success = load_json_dict(STATE_PATH)
    if verified_result(result) and result.get("failed"):
        return fail(f"Official command rejection: {format_attempt_summary(result)}")
    countdown = fresh_flow_countdown(now)
    if countdown is not None:
        return ok(f"Healthy bot-derived countdown: next state check in {int(countdown['not_before_ts'] - now)}s flow={FLOW_SLUG}")
    for record in (result, attempt, success):
        if not verified_result(record):
            continue
        due = float(record.get("next_due_ts") or record.get("not_before_ts") or 0)
        if due > now:
            return ok(f"Verified business result waiting until {due}: {format_attempt_summary(record)}")
    cooldown = find_pending_cooldown(load_json_dict(COOLDOWN_STATE_PATH), attempt, result, success)
    if cooldown and verified_result(cooldown):
        due = float(cooldown.get("not_before_ts") or 0)
        if due > now:
            return ok(f"Official cooldown retry pending until {due}")
        return warn(maybe_heal_stale_send({}, "Official cooldown due; sender owns reconciliation and state cleanup"))
    if held:
        return warn("Sender holds its lease; waiting for an official business reply")
    if str(attempt.get("status") or "") in UNCONFIRMED_SEND_STATUSES | {"action_timeout", "probe_timeout"}:
        return warn(maybe_heal_stale_send({}, "Legacy unconfirmed command requires reconciliation"))
    if verified_result(success) and success.get("succeeded", success.get("status") == "started"):
        age = now - float(success.get("ts") or 0)
        if 0 <= age <= MAX_AGE_SECONDS:
            return ok(f"Last verified success age={int(age)}s {format_attempt_summary(success)}")
    return fail(f"No current verified business result: {format_attempt_summary(attempt)}")


if __name__ == "__main__":
    sys.exit(main())
