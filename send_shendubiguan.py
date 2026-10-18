#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from telethon import TelegramClient

from account_lifecycle import AccountPausedError, assert_account_allowed
from runtime import env, log_print
from command_confirmation import PendingCommand, official_message, no_reply_interval, validate_command_identity, valid_receipt_identity
from reply_parsing import (
    ACTION_MESSAGE,
    DEFAULT_DURATION_SECONDS,
    STATUS_MESSAGE,
    classify_action_reply,
    classify_status_reply,
    slugify_message,
)
from state_store import (
    FlowLockBusy,
    flow_lock,
    is_paused,
    load_json_dict,
    persist_json_atomic,
    write_lock_metadata,
)
from transport import (
    collect_recent_messages,
    collect_direct_replies_to_message,
    ensure_connected,
    force_reconnect,
    send_with_global_gate,
    telegram_client,
    with_telegram_retry,
)


# Verified active-state replies establish a wait, never a new command success.
HEALTHY_STATUS_STATUSES = {"countdown_pending", "already_in_retreat", "cached_countdown_pending"}
HEALTHY_ACTION_STATUSES = {"started"}


def lock_busy_exit_code(*, observer_immediate_kick: bool) -> int:
    """Tell the observer whether a lock-busy kick must be retried."""
    return 75 if observer_immediate_kick else 0


OBSERVER_COMPLETED_OUTCOMES = {
    "started",
    "already_in_retreat",
    "countdown_pending",
    "cached_countdown_pending",
}


def retryable_outcome_exit_code(status: str, *, observer_immediate_kick: bool) -> int:
    """Keep observer events pending until the sender reaches a proven healthy outcome."""
    if not observer_immediate_kick:
        return 0
    return 0 if status in OBSERVER_COMPLETED_OUTCOMES else 75


def finish_outcome(status: str, *, observer_immediate_kick: bool) -> None:
    exit_code = retryable_outcome_exit_code(status, observer_immediate_kick=observer_immediate_kick)
    if exit_code:
        raise SystemExit(exit_code)


def persist_json(path: Path, payload: dict[str, Any]) -> None:
    persist_json_atomic(path, payload)


def extract_matching_replies(
    messages: list[Any],
    *,
    topic_id: int,
    sent_message_id: int,
    own_sender_id: int,
    seen: set[int] | None = None,
    expected_username: str = "",
) -> list[dict[str, Any]]:
    known = seen if seen is not None else set()
    replies: list[dict[str, Any]] = []
    for message in messages:
        message_id = getattr(message, "id", None)
        if message_id is None or message_id in known:
            continue
        if getattr(message, "sender_id", None) == own_sender_id:
            continue
        if not official_message(message) or getattr(message, "id", 0) <= sent_message_id:
            continue

        reply = getattr(message, "reply_to", None)
        reply_to_msg_id = getattr(message, "reply_to_msg_id", None)
        if not reply_to_msg_id:
            reply_to_msg_id = getattr(reply, "reply_to_msg_id", None)
        reply_to_top_id = getattr(message, "reply_to_top_id", None)
        if not reply_to_top_id:
            reply_to_top_id = getattr(reply, "reply_to_top_id", None)

        is_direct_reply = reply_to_msg_id == sent_message_id
        is_topic_root_reply = message_id > sent_message_id and (
            reply_to_msg_id == topic_id
            or (reply_to_msg_id in (None, 0) and reply_to_top_id == topic_id)
        )
        if is_topic_root_reply:
            from reply_parsing import is_matching_summary
            is_topic_root_reply = bool(expected_username and is_matching_summary(
                str(getattr(message, "message", "") or ""), expected_username))
        if not is_direct_reply and not is_topic_root_reply:
            continue

        known.add(message_id)
        reply_date = getattr(message, "date", None)
        replies.append(
            {
                "id": message_id,
                "sender_id": getattr(message, "sender_id", None),
                "text": (getattr(message, "message", None) or "").strip(),
                "ts": reply_date.timestamp() if reply_date is not None else time.time(),
                "topic_id": topic_id,
                "reply_to_msg_id": reply_to_msg_id,
                "reply_to_top_id": reply_to_top_id,
                "match_kind": "direct" if is_direct_reply else "topic_root",
            }
        )
    replies.sort(key=lambda item: float(item.get("ts") or 0.0))
    return replies


async def collect_reply_snapshot(
    client: TelegramClient,
    *,
    chat_id: int | str,
    topic_id: int,
    sent_message_id: int,
    own_sender_id: int,
    scan_limit: int,
    expected_username: str = "",
    recent: list[Any] | None = None,
    allow_history: bool = True,
) -> list[dict[str, Any]]:
    if recent is None:
        recent = await collect_recent_messages(client=client, chat_id=chat_id, scan_limit=scan_limit)
    direct = []
    if allow_history:
        try:
            direct = await asyncio.wait_for(collect_direct_replies_to_message(
                client=client, chat_id=chat_id, message_id=sent_message_id, scan_limit=scan_limit), 10)
        except TimeoutError:
            pass  # Keep the pending journal and permit its next scheduled retry.
    return extract_matching_replies(
        [*direct, *recent],
        topic_id=topic_id,
        sent_message_id=sent_message_id,
        own_sender_id=own_sender_id,
        expected_username=expected_username,
    )


async def wait_for_replies(
    client: TelegramClient,
    *,
    chat_id: int | str,
    topic_id: int,
    sent_message_id: int,
    own_sender_id: int,
    timeout_seconds: int,
    poll_seconds: int,
    scan_limit: int,
    expected_username: str = "",
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout_seconds
    seen: set[int] = set()
    replies: list[dict[str, Any]] = []

    while time.monotonic() < deadline:
        recent = await collect_recent_messages(client=client, chat_id=chat_id, scan_limit=scan_limit)
        replies.extend(
            extract_matching_replies(
                recent,
                topic_id=topic_id,
                sent_message_id=sent_message_id,
                own_sender_id=own_sender_id,
                expected_username=expected_username,
                seen=seen,
            )
        )
        if any(classify_status_reply(item["text"], expected_username=expected_username)[0] != "unknown"
               or classify_action_reply(item["text"], expected_username=expected_username)[0] != "unknown"
               for item in replies):
            break
        await asyncio.sleep(max(1, poll_seconds))
    replies.sort(key=lambda item: float(item.get("ts") or 0.0))
    return replies


def select_classified_reply(
    replies: list[dict[str, Any]],
    *,
    classifier,
) -> tuple[dict[str, Any] | None, str, int | None]:
    """Select the newest domain reply and ignore unrelated topic-root chatter."""
    for item in reversed(replies):
        status, duration = classifier(str(item.get("text") or ""))
        if status != "unknown":
            return item, status, duration
    return None, "unknown", None


async def send_topic_command(
    client: TelegramClient,
    *,
    chat_id: int | str,
    topic_id: int,
    text: str,
    state_dir: Path,
    on_dispatch=None,
) -> Any:
    await ensure_connected(client)
    async def action():
        if on_dispatch is not None:
            on_dispatch()
        return await client.send_message(chat_id, text, reply_to=topic_id)
    return await send_with_global_gate(
        action,
        label=f"send {text}",
        state_dir=state_dir,
    )


def compute_cached_not_before_ts(
    recent_probe: dict[str, Any],
    recent_success: dict[str, Any],
    *,
    min_repeat_seconds: int,
    actor_id: int | None = None,
    flow: str = "shendubiguan",
) -> float:
    candidates: list[float] = []
    if (recent_probe.get("status") == "countdown_pending"
            and recent_probe.get("message") in {STATUS_MESSAGE, ACTION_MESSAGE}
            and valid_receipt_identity(recent_probe, actor_id=actor_id, flow=flow)):
        candidates.append(float(recent_probe.get("not_before_ts") or 0))
    if (recent_success.get("status") == "started"
            and valid_receipt_identity(recent_success, actor_id=actor_id, flow=flow, command=ACTION_MESSAGE)):
        success_not_before = float(recent_success.get("not_before_ts") or 0)
        if success_not_before > 0:
            candidates.append(success_not_before)
        elif min_repeat_seconds > 0 and recent_success.get("status") == "started":
            success_ts = float(recent_success.get("ts") or 0)
            if success_ts > 0:
                candidates.append(success_ts + min_repeat_seconds)
    return max(candidates) if candidates else 0.0


async def run_once() -> None:
    load_dotenv()

    from miniapp_commands.runner import route_from_environment
    routed = await route_from_environment()
    if routed is not None:
        from miniapp_commands.run_cycle import result_exit_code
        code = result_exit_code(routed, observer_immediate=env("TG_OBSERVER_IMMEDIATE_KICK", "0") == "1")
        if code:
            raise SystemExit(code)
        return

    api_id_raw = env("TG_API_ID", required=True)
    api_hash = env("TG_API_HASH", required=True)
    session_name = env("TG_SESSION_NAME", "state/default/session")
    target_raw = env("TG_TARGET", required=True)
    topic_id_raw = env("TG_TOPIC_ID", required=True)
    flow_slug = env("TG_FLOW_SLUG", slugify_message(ACTION_MESSAGE))
    state_dir = Path(env("TG_STATE_DIR", "state"))
    state_dir.mkdir(parents=True, exist_ok=True)
    observer_immediate_kick = env("TG_OBSERVER_IMMEDIATE_KICK", "0") == "1"

    paused, pause_path = is_paused(state_dir, flow_slug)
    if paused:
        log_print(f"Paused via {pause_path}; skip send")
        finish_outcome("paused", observer_immediate_kick=observer_immediate_kick)
        return
    try:
        if env("XIU_ACCOUNT_SLUG", ""):
            assert_account_allowed(env("XIU_ACCOUNT_SLUG", ""))
    except AccountPausedError as exc:
        # A definite local refusal before any connect: the probe would only be
        # refused at send time and then recorded as an unknown send outcome.
        log_print(f"Account paused ({exc}); skip send")
        finish_outcome("account_paused", observer_immediate_kick=observer_immediate_kick)
        return

    lock_path = Path(env("TG_LOCK_PATH", str(state_dir / f"send_{flow_slug}.lock")))
    last_success_path = Path(env("TG_LAST_SUCCESS_PATH", str(state_dir / f"last_success_{flow_slug}.json")))
    last_attempt_path = Path(env("TG_LAST_ATTEMPT_PATH", str(state_dir / f"last_attempt_{flow_slug}.json")))
    status_state_path = Path(env("TG_STATUS_STATE_PATH", str(state_dir / "status_probe.json")))
    run_max_seconds = max(30, int(env("TG_RUN_MAX_SECONDS", "150")))
    scan_limit = max(20, int(env("TG_SCAN_LIMIT", "80")))
    poll_seconds = max(1, int(env("TG_REPLY_POLL_SECONDS", "2")))
    status_timeout_seconds = max(5, int(env("TG_STATUS_REPLY_TIMEOUT_SECONDS", "25")))
    action_timeout_seconds = max(5, int(env("TG_ACTION_REPLY_TIMEOUT_SECONDS", "25")))
    pre_repeat_probe_seconds = max(0, int(env("TG_PRE_REPEAT_PROBE_SECONDS", "300")))
    min_repeat_seconds = max(0, int(env("TG_MIN_REPEAT_SECONDS", str(DEFAULT_DURATION_SECONDS))))
    countdown_inline_wait_max_seconds = max(0, int(env("TG_COUNTDOWN_INLINE_WAIT_MAX_SECONDS", "900")))
    countdown_inline_wait_safety_margin_seconds = max(1, int(env("TG_COUNTDOWN_INLINE_WAIT_SAFETY_MARGIN_SECONDS", "8")))
    try:
        target: int | str = int(target_raw)
    except ValueError:
        target = target_raw

    try:
        api_id = int(api_id_raw)
    except ValueError as exc:
        raise SystemExit("TG_API_ID must be an integer") from exc

    try:
        topic_id = int(topic_id_raw)
    except ValueError as exc:
        raise SystemExit("TG_TOPIC_ID must be an integer") from exc

    session_path = Path(session_name)
    session_path.parent.mkdir(parents=True, exist_ok=True)
    for path in (last_success_path, last_attempt_path, status_state_path):
        path.parent.mkdir(parents=True, exist_ok=True)

    try:
        lock_ctx = flow_lock(lock_path)
        lock_ctx.__enter__()
    except FlowLockBusy:
        log_print(f"Another {Path(__file__).name} run is active; skip this run. lock={lock_path}")
        exit_code = lock_busy_exit_code(observer_immediate_kick=observer_immediate_kick)
        if exit_code:
            raise SystemExit(exit_code)
        return

    client: TelegramClient | None = None
    try:
        run_started_monotonic = time.monotonic()
        write_lock_metadata(
            lock_path,
            {
                "pid": os.getpid(),
                "flow_slug": flow_slug,
                "message": ACTION_MESSAGE,
                "status_message": STATUS_MESSAGE,
                "target": target_raw,
                "started_at_ts": time.time(),
                "run_max_seconds": run_max_seconds,
            },
        )

        recent_success = load_json_dict(last_success_path)
        recent_probe = load_json_dict(status_state_path)
        previous_attempt = load_json_dict(last_attempt_path)
        now_ts = time.time()
        cached_guard_enabled = min_repeat_seconds > 0
        not_before_ts = compute_cached_not_before_ts(
            recent_probe,
            recent_success,
            min_repeat_seconds=min_repeat_seconds,
            actor_id=int(env("XIU_ACCOUNT_SLUG", "tg0").removeprefix("tg")),
            flow=flow_slug,
        ) if cached_guard_enabled else 0.0
        remaining_cached = max(0, int(not_before_ts - now_ts)) if not_before_ts > now_ts else 0
        outstanding = (load_json_dict(state_dir / "command_pending.json").get("pending") or
                       load_json_dict(state_dir / "status_probe_pending.json").get("pending") or
                       previous_attempt.get("status") in {"action_timeout", "send_submitted", "send_outcome_unknown"})
        if cached_guard_enabled and remaining_cached > pre_repeat_probe_seconds and not outstanding:
            cached_receipt = max((recent_probe, recent_success), key=lambda row: float(row.get("not_before_ts") or 0))
            payload = {
                **cached_receipt,
                "ts": now_ts,
                "status": "cached_countdown_pending",
                "completion_reason": f"skip fresh probe; cached countdown still has {remaining_cached}s remaining",
                "message": cached_receipt.get("message"),
                "flow_slug": flow_slug,
                "not_before_ts": not_before_ts,
                "remaining_seconds": remaining_cached,
            }
            persist_json(last_attempt_path, payload)
            log_print(f"Cached countdown still active for {remaining_cached}s; skip fresh probe")
            finish_outcome("cached_countdown_pending", observer_immediate_kick=observer_immediate_kick)
            return

        client = telegram_client(session_path, api_id, api_hash)
        await with_telegram_retry(
            lambda: client.start(),
            label="shendubiguan client.start",
            attempts=max(2, int(env("TG_SEND_CONNECT_ATTEMPTS", "3"))),
            base_sleep=max(1.0, float(env("TG_SEND_CONNECT_BASE_SLEEP_SECONDS", "2"))),
            reconnect=lambda: force_reconnect(client),
        )
        me = await with_telegram_retry(client.get_me, label="get self", reconnect=lambda: ensure_connected(client))
        me_id = getattr(me, "id", 0) if me else 0
        me_username = str(getattr(me, "username", "") or "") if me else ""
        validate_command_identity(target_raw, topic_id, me_id, expected_account=env("XIU_ACCOUNT_SLUG", ""))
        pending_action = PendingCommand(state_dir, flow=flow_slug, command=ACTION_MESSAGE,
                                        target=target_raw, topic_id=topic_id, actor_id=me_id)
        pending_probe = PendingCommand(state_dir, flow=flow_slug, command=STATUS_MESSAGE,
                                       target=target_raw, topic_id=topic_id, actor_id=me_id,
                                       filename="status_probe_pending.json")
        retry_interval = no_reply_interval()
        if previous_attempt.get("status") == "action_timeout":
            pending_action.import_attempt({**previous_attempt, "status": "sent_no_ack"}, retry_interval)
        else:
            pending_action.import_attempt(previous_attempt, retry_interval)

        def record_action_reply(reply, kind, duration, sent_id):
            ts = float(reply.get("ts") or time.time())
            payload = {"ts": ts, "status": kind, "message": ACTION_MESSAGE, "flow_slug": flow_slug,
                       "actor_id": me_id, "succeeded": kind == "started",
                       "sent_message_id": sent_id, "reply_message_id": reply["id"],
                       "reply_sender_id": reply["sender_id"], "reply_text": reply["text"],
                       "target": str(target_raw), "topic_id": topic_id, "acked": True,
                       "not_before_ts": ts + int(duration or DEFAULT_DURATION_SECONDS) if kind == "started" else None,
                       "remaining_seconds": duration, "completion_reason": "verified official deep retreat reply"}
            persist_json(last_attempt_path, payload)
            persist_json(state_dir / "last_result.json", payload)
            if kind == "started":
                persist_json(last_success_path, payload)
                persist_json(status_state_path, {**payload, "status": "countdown_pending"})
            pending_action.finish(status=kind, receipt_id=reply["id"], reply_ts=ts)
            return payload

        if pending_action.pending:
            recent = await collect_recent_messages(client=client, chat_id=target, scan_limit=max(160, scan_limit))
            pending_action.recover_deliveries(recent)
            lookups = set(pending_action.reply_scan_ids())
            for pending_id in pending_action.message_ids:
                replies = await collect_reply_snapshot(client, chat_id=target, topic_id=topic_id,
                    sent_message_id=pending_id, own_sender_id=me_id, scan_limit=max(160, scan_limit),
                    expected_username=me_username, recent=recent, allow_history=pending_id in lookups)
                reply, kind, duration = select_classified_reply(replies,
                    classifier=lambda text: classify_action_reply(text, expected_username=me_username))
                if reply and kind in {"started", "already_in_retreat", "summary"}:
                    record_action_reply(reply, kind, duration, pending_id)
                    finish_outcome(kind, observer_immediate_kick=observer_immediate_kick)
                    return
            if time.time() < float(pending_action.data.get("next_retry_ts") or 0):
                finish_outcome("reply_retry_pending", observer_immediate_kick=observer_immediate_kick)
                return

        should_start = False
        if observer_immediate_kick or pending_action.pending:
            status_payload = {
                "ts": time.time(),
                "status": "observer_immediate_starting",
                "completion_reason": f"return-summary observer kick; sending {ACTION_MESSAGE} without .查看闭关 probe",
                "message": ACTION_MESSAGE,
                "sent_message_id": None,
                "reply_message_id": None,
                "reply_text": "",
                "topic_id": topic_id,
                "target": str(target_raw),
                "flow_slug": flow_slug,
                "not_before_ts": None,
                "remaining_seconds": None,
            }
            persist_json(status_state_path, status_payload)
            persist_json(last_attempt_path, status_payload)
            log_print(status_payload["completion_reason"])
            should_start = True
        else:
            if recent_probe.get("status") in {"unknown", "probe_timeout", "status_probe_sent", "status_send_unknown"}:
                pending_probe.import_attempt({**recent_probe, "status": "sent_no_ack"}, retry_interval)

            async def recover_status_replies():
                recent = await collect_recent_messages(client=client, chat_id=target, scan_limit=max(160, scan_limit))
                pending_probe.recover_deliveries(recent)
                replies = []
                lookups = set(pending_probe.reply_scan_ids())
                for mid in pending_probe.message_ids:
                    snapshot = await collect_reply_snapshot(client, chat_id=target, topic_id=topic_id,
                        sent_message_id=mid, own_sender_id=me_id, scan_limit=max(160, scan_limit),
                        expected_username=me_username, recent=recent, allow_history=mid in lookups)
                    replies.extend({**row, "command_message_id": mid} for row in snapshot)
                return sorted(replies, key=lambda row: float(row.get("ts") or 0))

            status_replies = await recover_status_replies() if pending_probe.pending else []
            recovered, recovered_kind, _ = select_classified_reply(status_replies,
                classifier=lambda text: classify_status_reply(text, expected_username=me_username))
            status_sent_id = recovered.get("command_message_id") if recovered else None
            if recovered_kind == "unknown":
                if pending_probe.pending and time.time() < float(pending_probe.data.get("next_retry_ts") or 0):
                    finish_outcome("probe_timeout", observer_immediate_kick=observer_immediate_kick)
                    return
                pending_probe.begin(retry_interval)
                submitted = {"ts": time.time(), "status": "status_probe_sent", "actor_id": me_id,
                    "flow_slug": flow_slug, "message": STATUS_MESSAGE, "target": str(target_raw),
                    "topic_id": topic_id, **pending_probe.retry_payload()}
                persist_json(last_attempt_path, submitted)
                persist_json(status_state_path, submitted)
                try:
                    status_sent = await send_topic_command(client, chat_id=target, topic_id=topic_id,
                        text=STATUS_MESSAGE, state_dir=state_dir,
                        on_dispatch=lambda: pending_probe.dispatched(retry_interval))
                    if not getattr(status_sent, "id", None):
                        raise RuntimeError("status send returned no Telegram message id")
                    pending_probe.sent(status_sent)
                except Exception as exc:
                    pending_probe.unknown(exc)
                    payload = {**submitted, "status": "status_send_unknown", "error_type": type(exc).__name__,
                               **pending_probe.retry_payload()}
                    persist_json(status_state_path, payload)
                    persist_json(last_attempt_path, payload)
                    finish_outcome("status_send_unknown", observer_immediate_kick=observer_immediate_kick)
                    return
                status_sent_id = status_sent.id
                submitted.update(sent_message_id=status_sent_id, **pending_probe.retry_payload())
                persist_json(last_attempt_path, submitted)
                persist_json(status_state_path, submitted)
                fresh_replies = await wait_for_replies(client, chat_id=target, topic_id=topic_id,
                    sent_message_id=status_sent_id, own_sender_id=me_id, expected_username=me_username,
                    timeout_seconds=status_timeout_seconds, poll_seconds=poll_seconds, scan_limit=scan_limit)
                status_replies = [{**row, "command_message_id": status_sent_id} for row in fresh_replies]
                if not status_replies:
                    status_replies = await recover_status_replies()

            latest_status_text = ""
            latest_status_ts = now_ts
            status_kind = "unknown"
            countdown_seconds = None
            if status_replies:
                latest, status_kind, countdown_seconds = select_classified_reply(
                    status_replies,
                    classifier=lambda text: classify_status_reply(
                        text,
                        expected_username=me_username,
                    ),
                )
            else:
                latest = None
            if latest is not None:
                status_sent_id = latest.get("command_message_id") or status_sent_id
                latest_status_text = str(latest.get("text") or "")
                latest_status_ts = float(latest.get("ts") or now_ts)
            elif not status_replies:
                status_kind = "probe_timeout"

            status_payload = {
                "ts": latest_status_ts if status_replies else time.time(),
                "status": status_kind,
                "actor_id": me_id,
                "acked": latest is not None and status_kind != "unknown",
                "completion_reason": "processed .查看闭关 reply",
                "message": STATUS_MESSAGE,
                "sent_message_id": status_sent_id,
                "reply_message_id": latest["id"] if latest is not None else None,
                "reply_sender_id": latest["sender_id"] if latest is not None else None,
                "reply_text": latest_status_text,
                "topic_id": topic_id,
                "target": str(target_raw),
                "flow_slug": flow_slug,
                "not_before_ts": None,
                "remaining_seconds": countdown_seconds,
                "retry_not_before_ts": pending_probe.data.get("next_retry_ts") if status_kind in {"unknown", "probe_timeout"} else None,
            }

            if latest is not None and status_kind != "unknown":
                pending_probe.finish(status=status_kind, receipt_id=latest["id"], reply_ts=latest_status_ts)

            if status_kind == "countdown" and countdown_seconds is not None:
                status_payload["status"] = "countdown_pending"
                status_payload["not_before_ts"] = latest_status_ts + countdown_seconds
                status_payload["completion_reason"] = f"deep retreat still running; remaining={countdown_seconds}s"

                inline_wait_budget = max(
                    0.0,
                    min(
                        float(countdown_inline_wait_max_seconds),
                        float(run_max_seconds) - (time.monotonic() - run_started_monotonic)
                        - float(countdown_inline_wait_safety_margin_seconds) - action_timeout_seconds,
                    ),
                )
                wait_seconds = max(0, status_payload["not_before_ts"] - time.time())
                if wait_seconds <= inline_wait_budget:
                    log_print(f"Countdown reply says remaining={countdown_seconds}s; waiting inline {wait_seconds}s before sending {ACTION_MESSAGE}")
                    if wait_seconds > 0:
                        await asyncio.sleep(wait_seconds)
                    should_start = True
                    status_payload["status"] = "countdown_wait_inline"
                    status_payload["completion_reason"] = (
                        f"countdown reply remaining={countdown_seconds}s; waited inline and will start next deep retreat now"
                    )
                else:
                    log_print(f"Countdown reply says remaining={countdown_seconds}s; cached next start because inline wait budget is {int(inline_wait_budget)}s")
            elif status_kind in {"summary", "not_in_retreat"}:
                should_start = True
                status_payload["completion_reason"] = f"status reply={status_kind}; should start next deep retreat"
            elif status_kind == "already_in_retreat":
                status_payload["completion_reason"] = "status says already in deep retreat but without explicit countdown"
            elif status_kind == "probe_timeout":
                status_payload["completion_reason"] = "no official status reply; retry the probe before starting"
            else:
                status_payload["completion_reason"] = f"unclassified .查看闭关 reply: {latest_status_text or '[empty]'}"

            persist_json(status_state_path, status_payload)
            if status_payload["acked"]:
                persist_json(state_dir / "last_result.json", status_payload)

            if not should_start:
                persist_json(last_attempt_path, status_payload)
                if status_payload["status"] not in HEALTHY_STATUS_STATUSES:
                    log_print(f"Status outcome {status_payload['status']!r} is not healthy; success state not updated")
                log_print(status_payload["completion_reason"])
                finish_outcome(str(status_payload["status"]), observer_immediate_kick=observer_immediate_kick)
                return

        await ensure_connected(client)
        pending_action.begin(retry_interval)
        persist_json(last_attempt_path, {"ts": time.time(), "status": "send_submitted", "flow_slug": flow_slug,
            "message": ACTION_MESSAGE, "target": str(target_raw), "topic_id": topic_id, **pending_action.retry_payload()})
        try:
            action_sent = await send_topic_command(client, chat_id=target, topic_id=topic_id,
                text=ACTION_MESSAGE, state_dir=state_dir,
                on_dispatch=lambda: pending_action.dispatched(retry_interval))
            pending_action.sent(action_sent)
        except Exception as exc:
            pending_action.unknown(exc)
            persist_json(last_attempt_path, {"ts": time.time(), "status": "send_outcome_unknown", "flow_slug": flow_slug,
                "message": ACTION_MESSAGE, "target": str(target_raw), "topic_id": topic_id,
                "error_type": type(exc).__name__, **pending_action.retry_payload()})
            finish_outcome("send_outcome_unknown", observer_immediate_kick=observer_immediate_kick)
            return
        action_sent_id = getattr(action_sent, "id", None)
        if action_sent_id is None:
            persist_json(
                last_attempt_path,
                {
                    "ts": time.time(),
                    "status": "action_send_failed",
                    "completion_reason": "action send returned no message id",
                    "message": ACTION_MESSAGE,
                    "topic_id": topic_id,
                    "target": str(target_raw),
                    "flow_slug": flow_slug,
                },
            )
            log_print("Action send returned no message id; treating run as failed")
            finish_outcome("action_send_failed", observer_immediate_kick=observer_immediate_kick)
            return

        action_replies = await wait_for_replies(
            client,
            chat_id=target,
            topic_id=topic_id,
            sent_message_id=int(action_sent_id),
            own_sender_id=me_id,
            expected_username=me_username,
            timeout_seconds=action_timeout_seconds,
            poll_seconds=poll_seconds,
            scan_limit=scan_limit,
        )

        action_text = ""
        action_ts = time.time()
        action_kind = "unknown"
        action_duration_seconds = None
        action_latest = None
        if action_replies:
            direct_replies = [item for item in action_replies if item.get("match_kind") == "direct"]
            fallback_replies = [item for item in action_replies if item.get("match_kind") == "topic_root"]

            chosen_reply, action_kind, action_duration_seconds = select_classified_reply(
                direct_replies,
                classifier=lambda text: classify_action_reply(text, expected_username=me_username),
            )
            if chosen_reply is None:
                chosen_reply, action_kind, action_duration_seconds = select_classified_reply(
                    fallback_replies,
                    classifier=lambda text: classify_action_reply(text, expected_username=me_username),
                )

            if chosen_reply is not None:
                action_latest = chosen_reply
                action_text = str(action_latest.get("text") or "")
                action_ts = float(action_latest.get("ts") or action_ts)
        if action_latest is None:
            action_kind = "action_timeout"
        else:
            record_action_reply(action_latest, action_kind, action_duration_seconds, action_sent_id)
            finish_outcome(action_kind, observer_immediate_kick=observer_immediate_kick)
            return

        attempt_payload = {
            "ts": action_ts,
            "status": action_kind,
            "completion_reason": "processed .深度闭关 reply",
            "message": ACTION_MESSAGE,
            "sent_message_id": action_sent_id,
            "reply_message_id": action_latest["id"] if action_latest is not None else None,
            "reply_text": action_text,
            "topic_id": topic_id,
            "target": str(target_raw),
            "flow_slug": flow_slug,
            "not_before_ts": None,
            "remaining_seconds": action_duration_seconds,
            **pending_action.retry_payload(),
        }

        attempt_payload["completion_reason"] = "no official deep retreat reply; retry remains scheduled"
        persist_json(last_attempt_path, attempt_payload)
        log_print(attempt_payload["completion_reason"])
        finish_outcome(str(attempt_payload["status"]), observer_immediate_kick=observer_immediate_kick)
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass
        lock_ctx.__exit__(None, None, None)


def main() -> None:
    load_dotenv()
    run_max_seconds = max(30, int(env("TG_RUN_MAX_SECONDS", "150")))

    async def _bounded() -> None:
        try:
            await asyncio.wait_for(run_once(), timeout=run_max_seconds)
        except asyncio.TimeoutError as exc:
            raise SystemExit(f"{Path(__file__).name} exceeded TG_RUN_MAX_SECONDS={run_max_seconds}") from exc

    asyncio.run(_bounded())


if __name__ == "__main__":
    main()
