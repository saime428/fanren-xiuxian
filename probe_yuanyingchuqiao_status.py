#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

from dotenv import load_dotenv
from account_lifecycle import AccountPausedError, assert_account_allowed
from command_confirmation import PendingCommand, official_message, no_reply_interval, validate_command_identity, valid_receipt_identity
from transport import send_with_global_gate, collect_recent_messages, collect_direct_replies_to_message, ensure_connected, telegram_client
from state_store import FlowLockBusy, flow_lock, persist_json_atomic, load_json_dict
from reply_parsing import parse_cooldown_retry_delay_seconds
from runtime import env


def parse_markers(raw: str) -> list[str]:
    return [part.strip() for part in raw.split("|") if part.strip()]


def is_yuanying_retreat_status(text: str) -> bool:
    # Other accounts must still respect a manually started retreat. Dispatch
    # help mentioning the command is not an authoritative activity status.
    text = str(text or "").replace("**", "").replace("`", "")
    return re.search(r"^[ \t]*(?:状态|狀態)[ \t]*[:：][ \t]*元[婴嬰][闭閉][关關][ \t]*$",
                     text, re.MULTILINE) is not None


def parse_remaining_seconds(text: str) -> int | None:
    parsed = parse_cooldown_retry_delay_seconds(text, [""])
    if parsed is not None:
        return parsed

    compact = text.replace("：", ":")
    m = re.search(r"剩[余餘]\s*:?\s*(\d+)\s*小时\s*(\d+)\s*分钟\s*(\d+)\s*秒", compact)
    if m:
        hours, minutes, seconds = map(int, m.groups())
        return hours * 3600 + minutes * 60 + seconds
    m = re.search(r"倒计时\s*:?\s*(\d+)\s*小时\s*(\d+)\s*分钟\s*(\d+)\s*秒", compact)
    if m:
        hours, minutes, seconds = map(int, m.groups())
        return hours * 3600 + minutes * 60 + seconds
    return None


async def _main_async() -> int:
    load_dotenv()

    from miniapp_commands.runner import route_from_environment
    routed = await route_from_environment(probe_only=True)
    if routed is not None:
        if routed.get("status") == "ready":
            return 0
        return 10 if routed.get("status") in {"active", "manual_retreat", "scheduled_wait"} else 11
    account = env("XIU_ACCOUNT_SLUG", "")
    if account:
        try:
            assert_account_allowed(account)
        except AccountPausedError as exc:
            # A definite local refusal before any connect: the probe would only
            # be refused at send time and kept as an unknown send to retry.
            print(f"Account paused ({exc}); skip status probe")
            return 12

    api_id = int(env("TG_API_ID", required=True))
    api_hash = env("TG_API_HASH", required=True)
    session_name = env("TG_SESSION_NAME", required=True)
    target_raw = env("TG_TARGET", required=True)
    topic_id = int(env("TG_TOPIC_ID", required=True))
    state_dir = Path(env("TG_STATE_DIR", "state"))
    flow_slug = env("TG_FLOW_SLUG", "yuanyingchuqiao")
    probe_message = env("TG_STATUS_PROBE_MESSAGE", ".元婴状态")
    probe_state_path = Path(env("TG_STATUS_PROBE_PATH", str(state_dir / "status_probe.json")))
    cache_grace_seconds = max(0, int(env("TG_STATUS_PROBE_CACHE_GRACE_SECONDS", "60")))
    wait_after_send_seconds = max(1.0, float(env("TG_STATUS_PROBE_WAIT_SECONDS", "3")))
    scan_limit = max(20, int(env("TG_STATUS_PROBE_SCAN_LIMIT", "40")))
    in_progress_markers = parse_markers(
        env(
            "TG_STATUS_PROBE_IN_PROGRESS_MARKERS",
            "你的元婴正在执行|你的元嬰正在執行|元神出窍|元神出竅|无法分身|無法分身|请先使用 .元婴归窍|請先使用 .元嬰歸竅|状态: 元神出窍|狀態: 元神出竅|归来倒计时|歸來倒計時",
        )
    )
    clear_markers = parse_markers(
        env(
            "TG_STATUS_PROBE_CLEAR_MARKERS",
            "已归窍|已歸竅|当前没有进行中的元婴任务|當前沒有進行中的元嬰任務|空闲|空閒|可再次出窍|可再次出竅|状态: 空闲|狀態: 空閒",
        )
    )
    blocked_markers = parse_markers(
        env(
            "TG_STATUS_PROBE_BLOCKED_MARKERS",
            "尚未凝聚元婴|尚未凝聚元嬰|无法施展此术|無法施展此術|不能施展此术|不能施展此術",
        )
    )
    last_success_path = Path(env("TG_LAST_SUCCESS_PATH", str(state_dir / "last_success.json")))
    nominal_cooldown_seconds = max(1, int(env("TG_NOMINAL_COOLDOWN_SECONDS", "28800")))

    now = time.time()
    cached = load_json_dict(probe_state_path)
    cached_not_before_ts = float(cached.get("not_before_ts") or 0)
    cached_status = str(cached.get("status") or "")
    if cached_status == "probe_timeout" and float(cached.get("retry_not_before_ts") or 0) > now:
        return 11
    if (cached_status == "countdown_pending" and valid_receipt_identity(cached,
            actor_id=int(env("XIU_ACCOUNT_SLUG", "tg0").removeprefix("tg")), flow=flow_slug, command=probe_message)
            and not load_json_dict(state_dir / "status_probe_pending.json").get("pending")
            and cached_not_before_ts - now > cache_grace_seconds):
        remaining = int(max(0, cached_not_before_ts - now) + 0.999999)
        print(
            f"Cached status probe still active for {flow_slug}; remaining={remaining}s until {cached_not_before_ts}"
        )
        return 10

    target: int | str
    try:
        target = int(target_raw)
    except ValueError:
        target = target_raw

    client = telegram_client(Path(session_name), api_id, api_hash)
    async with client:
        await ensure_connected(client)
        me = await client.get_me()
        me_id = getattr(me, "id", 0)
        validate_command_identity(target_raw, topic_id, me_id, expected_account=env("XIU_ACCOUNT_SLUG", ""))
        journal = PendingCommand(state_dir, flow=flow_slug, command=probe_message, target=target_raw,
                                 topic_id=topic_id, actor_id=me_id, filename="status_probe_pending.json")
        interval = no_reply_interval()
        if cached.get("status") in {"probe_timeout", "reply_unclassified", "status_probe_sent"}:
            journal.import_attempt({**cached, "status": "sent_no_ack"}, interval)

        async def reconcile():
            recent = await collect_recent_messages(client, target, scan_limit)
            journal.recover_deliveries(recent)
            replies = list(recent)
            for mid in journal.reply_scan_ids():
                try:
                    replies.extend(await asyncio.wait_for(collect_direct_replies_to_message(client, target, mid, scan_limit), 10))
                except TimeoutError:
                    pass  # Recent replies still match every journalled command.
            selected = []
            for message in replies:
                parent = getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None) or getattr(message, "reply_to_msg_id", None)
                text = str(getattr(message, "message", "") or "")
                if (official_message(message) and parent in journal.message_ids
                        and getattr(message, "id", 0) > parent and (is_yuanying_retreat_status(text) or any(
                            marker in text for marker in in_progress_markers + clear_markers + blocked_markers))):
                    selected.append(message)
            return max(selected, key=lambda item: (getattr(item, "edit_date", None) or item.date).timestamp(), default=None)

        reply = await reconcile() if journal.pending else None
        if reply is None:
            if journal.pending and time.time() < float(journal.data.get("next_retry_ts") or 0):
                return 11
            journal.begin(interval)
            persist_json_atomic(probe_state_path, {"ts": time.time(), "status": "status_probe_sent", "flow_slug": flow_slug,
                "message": probe_message, "target": str(target_raw), "topic_id": topic_id, **journal.retry_payload()})
            try:
                async def send_probe():
                    journal.dispatched(interval)
                    return await client.send_message(entity=target, message=probe_message, reply_to=topic_id)
                sent = await send_with_global_gate(
                    send_probe,
                    label="status-probe send_message", state_dir=state_dir,
                )
                journal.sent(sent)
            except Exception as exc:
                journal.unknown(exc)
                persist_json_atomic(probe_state_path, {"ts": time.time(), "status": "probe_timeout", "flow_slug": flow_slug,
                    "message": probe_message, "error_type": type(exc).__name__, **journal.retry_payload()})
                return 11
            await asyncio.sleep(wait_after_send_seconds)
            reply = await reconcile()
        if reply is None:
            persist_json_atomic(probe_state_path, {"ts": time.time(), "status": "probe_timeout", "flow_slug": flow_slug,
                "message": probe_message, "sent_message_id": journal.message_ids[-1] if journal.message_ids else None,
                "target": str(target_raw), "topic_id": topic_id, **journal.retry_payload()})
            print(f"Status probe got no qualifying reply for {flow_slug}; retry remains scheduled")
            return 11
        sent_id = getattr(getattr(reply, "reply_to", None), "reply_to_msg_id", None) or getattr(reply, "reply_to_msg_id", None)
        sent_ts = float(journal.data.get("first_attempt_ts") or time.time())
        journal.finish(status="status_confirmed", receipt_id=reply.id,
                       reply_ts=(getattr(reply, "edit_date", None) or reply.date).timestamp())

    reply_text = (getattr(reply, "message", None) or "").strip()
    reply_ts = getattr(getattr(reply, "edit_date", None) or getattr(reply, "date", None), "timestamp", lambda: sent_ts)()
    retreat_active = is_yuanying_retreat_status(reply_text)
    remaining_seconds = parse_remaining_seconds(reply_text)
    in_progress = retreat_active or any(marker in reply_text for marker in in_progress_markers)
    clear_state = any(marker in reply_text for marker in clear_markers)
    blocked_state = any(marker in reply_text for marker in blocked_markers)

    if blocked_state:
        payload = {
            "ts": reply_ts, "actor_id": me_id, "acked": True,
            "status": "blocked",
            "completion_reason": "yuanying status probe indicates this account cannot cast the skill now",
            "message": probe_message,
            "sent_message_id": sent_id,
            "reply_message_id": getattr(reply, "id", None),
            "reply_sender_id": getattr(reply, "sender_id", None),
            "reply_text": reply_text,
            "topic_id": topic_id,
            "target": str(target_raw),
            "flow_slug": flow_slug,
            "not_before_ts": None,
            "remaining_seconds": None,
        }
        persist_json_atomic(probe_state_path, payload)
        print(f"Status probe says {flow_slug} is blocked: {reply_text}")
        return 12

    if in_progress and remaining_seconds is None and not retreat_active:
        anchor_ts = 0.0
        for candidate_path in (last_success_path,):
            payload = load_json_dict(candidate_path)
            if not payload.get("succeeded") or not valid_receipt_identity(payload, actor_id=me_id, flow=flow_slug):
                continue
            candidate_anchor = float(payload.get("actual_send_ts") or payload.get("ts") or 0)
            if candidate_anchor > anchor_ts:
                anchor_ts = candidate_anchor
        if anchor_ts > 0:
            derived_remaining = max(0, int((anchor_ts + nominal_cooldown_seconds) - reply_ts + 0.999999))
            if derived_remaining > 0:
                remaining_seconds = derived_remaining

    if in_progress and remaining_seconds and remaining_seconds > 0:
        payload = {
            "ts": reply_ts, "actor_id": me_id, "acked": True,
            "status": "countdown_pending",
            "completion_reason": "active yuanying task detected by status probe",
            "message": probe_message,
            "sent_message_id": sent_id,
            "reply_message_id": getattr(reply, "id", None),
            "reply_sender_id": getattr(reply, "sender_id", None),
            "reply_text": reply_text,
            "topic_id": topic_id,
            "target": str(target_raw),
            "flow_slug": flow_slug,
            "not_before_ts": reply_ts + remaining_seconds,
            "remaining_seconds": remaining_seconds,
        }
        persist_json_atomic(probe_state_path, payload)
        print(
            f"Status probe detected active {flow_slug}; remaining={remaining_seconds}s until {payload['not_before_ts']}"
        )
        return 10

    payload = {"ts": reply_ts, "actor_id": me_id, "acked": True, "flow_slug": flow_slug, "message": probe_message,
               "sent_message_id": sent_id, "reply_message_id": getattr(reply, "id", None),
               "reply_sender_id": getattr(reply, "sender_id", None), "reply_text": reply_text,
               "target": str(target_raw), "topic_id": topic_id}
    if in_progress:
        persist_json_atomic(probe_state_path, {**payload, "status": "active_without_countdown"})
        return 10
    if clear_state:
        persist_json_atomic(probe_state_path, {**payload, "status": "available"})
        print(f"Status probe says {flow_slug} is available")
        return 0
    persist_json_atomic(probe_state_path, {**payload, "status": "reply_unclassified"})
    print(f"Status probe reply is unclassified for {flow_slug}; availability unconfirmed")
    return 11


async def main_async() -> int:
    load_dotenv()
    state_dir = Path(env("TG_STATE_DIR", "state"))
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        with flow_lock(state_dir / "status_probe.lock"):
            return await _main_async()
    except FlowLockBusy:
        return 11


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
