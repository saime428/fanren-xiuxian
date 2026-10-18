#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from dotenv import load_dotenv
from telethon import TelegramClient

from runtime import env, log_print
from reply_parsing import (
    AckPolicy,
    DEFAULT_SUCCESS_MARKER_TEXT,
    ReplyOutcome,
    classify_reply_messages,
    compute_inline_cooldown_wait_budget_seconds,
    parse_cooldown_markers,
    parse_markers,
    parse_reset_triggers,
    slugify_message,
)
from state_store import (
    FlowLockBusy,
    clear_pending_retry,
    flow_lock,
    is_paused,
    load_json_dict,
    load_retry_state,
    persist_json_atomic,
    set_pending_retry,
    write_lock_metadata,
)
from attempt_ledger import append_event, ledger_path
from command_confirmation import PendingCommand, no_reply_interval, validate_command_identity, valid_receipt_identity


LEDGER_EVENT_ENV = "XIU_ATTEMPT_LEDGER"


class ExpectedFlowExit(Exception):
    """Expected non-success exit that must not escape an asyncio task as SystemExit."""

    def __init__(self, code: int) -> None:
        self.code = int(code)
        super().__init__(str(self.code))


def record_attempt_event(state_dir: Path, attempt_id: str, event: str, state: str, **payload: Any) -> None:
    if os.getenv(LEDGER_EVENT_ENV, "1") == "0":
        return
    append_event(ledger_path(state_dir), attempt_id=attempt_id, event=event, state=state, payload=payload)


from transport import (
    collect_direct_replies_to_message,
    collect_recent_messages,
    ensure_connected,
    fetch_message_by_id,
    send_with_global_gate,
    telegram_client,
    with_telegram_retry,
)

print = log_print

DEFAULT_RESET_TRIGGER_TEXT = "冷却时间被重置|可以立刻再次闭关|毫无疲惫之感"
DEFAULT_COOLDOWN_MARKER_TEXT = "时间没到|時間沒到|还不能|還不能|后可再|後可再|后再来|後再來|后再试|後再試"


def persist_attempt_state(path: Path, payload: dict[str, Any]) -> None:
    persist_json_atomic(path, payload)


def should_stop_before_send(cfg: "Config") -> bool:
    if os.getenv("XIU_FLOW_SLUG", "").strip() != "doufa_demo_player_02":
        return False
    pause_path = cfg.state_dir / "pause_until.json"
    if not pause_path.exists():
        return False
    data = load_json_dict(pause_path)
    if str(data.get("mode") or "") == "manual_resume_required":
        print(
            f"Anti-repeat manual pause active before send; authenticated_sender_account={os.getenv('XIU_ACCOUNT_SLUG', '')}; "
            f"resume explicitly before sending via {pause_path}"
        )
        return True
    # Migrate any pre-existing date-only pauses to the new no-auto-resume
    # policy rather than silently resuming them at a calendar boundary.
    data["mode"] = "manual_resume_required"
    data["reason"] = str(data.get("reason") or "legacy anti-repeat pause; manual resume required")
    data.pop("until_date", None)
    persist_attempt_state(pause_path, data)
    print(
        f"Migrated legacy anti-repeat pause to manual resume; authenticated_sender_account={os.getenv('XIU_ACCOUNT_SLUG', '')}; "
        f"resume explicitly before sending via {pause_path}"
    )
    return True


async def maybe_wait_until_target_ts(target_ts: float | None) -> None:
    if target_ts is None:
        return
    while True:
        remaining = target_ts - time.time()
        if remaining <= 0:
            return
        if remaining > 1.0:
            await asyncio.sleep(min(remaining - 0.25, 0.5))
            continue
        if remaining > 0.05:
            await asyncio.sleep(min(remaining / 2.0, 0.05))
            continue
        return


def _ack_policy_from_env(reply_bot_id: int) -> AckPolicy:
    ack_marker_text = os.getenv("TG_ACK_MARKER_TEXT", "")
    defaults = {
        "xuexishanlin": "【血洗功成】",
        "shenji_budao": "布道成功|信仰提升|信仰增加",
    }
    flow = os.getenv("TG_FLOW_SLUG", os.getenv("XIU_FLOW_SLUG", ""))
    return AckPolicy(
        reply_bot_id=reply_bot_id,
        ack_markers=parse_markers(ack_marker_text) if ack_marker_text.strip() else [],
        success_markers=parse_markers(os.getenv("TG_SUCCESS_MARKER_TEXT", defaults.get(flow, DEFAULT_SUCCESS_MARKER_TEXT))),
        failure_markers=parse_markers(os.getenv("TG_FAILURE_MARKER_TEXT", "没有这件拥有器灵的法宝|名字输入错误|尚未凝聚元婴|尚未凝聚元嬰|无法施展此术|無法施展此術")),
        ack_any_reply=os.getenv("TG_ACK_ANY_REPLY", "0") == "1",
        ack_reply_bot_with_time_only=os.getenv("TG_ACK_REPLY_BOT_WITH_TIME_ONLY", "0") == "1",
        require_success_or_cooldown=os.getenv("TG_REQUIRE_SUCCESS_OR_COOLDOWN", "0") == "1",
        settlement_markers=parse_markers(os.getenv("TG_SETTLEMENT_MARKER_TEXT", ack_marker_text)),
    )


async def resend_after_explicit_failure(
    outcome: ReplyOutcome,
    *,
    send,
    resend_count: int,
    resend_max: int,
):
    if not outcome.failed or resend_count >= resend_max:
        return None
    return await send()


async def _classify_with_fetches(
    client: TelegramClient,
    *,
    recent: list[object],
    chat_id: int | str,
    topic_id: int,
    own_sender_id: int,
    target_message: str,
    sent_message_id: int | None,
    scan_limit: int,
    reset_triggers: Sequence[str],
    cooldown_markers: Sequence[str],
    policy: AckPolicy,
    sent_record: dict | None = None,
    allow_fetches: bool = True,
) -> ReplyOutcome:
    merged: list[object] = []
    seen_ids: set[int] = set()
    for m in recent:
        mid = getattr(m, "id", None)
        if mid is None or mid in seen_ids:
            continue
        seen_ids.add(mid)
        merged.append(m)

    if sent_message_id is not None and sent_message_id not in seen_ids:
        fetched_sent = (await fetch_message_by_id(client=client, chat_id=chat_id, message_id=sent_message_id)
                        if allow_fetches else None)
        if fetched_sent is not None:
            merged.append(fetched_sent)
        elif sent_record and sent_record.get("message_id") == sent_message_id:
            # A command deleted by chat cleanup can still have an official
            # reply. Only the actor-bound durable send journal supplies this
            # fallback; an arbitrary reply ID or legacy success file cannot.
            sent_ts = sent_record.get("sent_at_ts") or sent_record.get("submitted_at_ts")
            if sent_ts:
                merged.append(SimpleNamespace(
                    id=sent_message_id, sender_id=own_sender_id, message=target_message,
                    date=datetime.fromtimestamp(float(sent_ts), timezone.utc),
                    reply_to=SimpleNamespace(reply_to_msg_id=topic_id, reply_to_top_id=topic_id),
                ))

    def classify():
        return classify_reply_messages(
            merged, topic_id=topic_id, own_sender_id=own_sender_id,
            target_message=target_message, sent_message_id=sent_message_id,
            reset_triggers=reset_triggers, cooldown_markers=cooldown_markers,
            policy=policy, log=print)

    observed = classify()
    if observed.acked or sent_message_id is None or not allow_fetches:
        return observed
    # Use already fetched, fully attributed evidence before trying GetReplies;
    # Telegram rejects GetReplies for some forum commands with TOPIC_ID_INVALID.
    direct_replies = await collect_direct_replies_to_message(
        client=client, chat_id=chat_id, message_id=sent_message_id, scan_limit=scan_limit)
    seen_ids = {getattr(item, "id", None) for item in merged}
    for message in direct_replies:
        if getattr(message, "id", None) not in seen_ids:
            merged.append(message)
            seen_ids.add(message.id)
    return classify()


async def gather_reply_outcome(
    client: TelegramClient,
    *,
    chat_id: int | str,
    topic_id: int,
    own_sender_id: int,
    target_message: str,
    reply_bot_id: int,
    sent_message_id: int | None = None,
    scan_limit: int = 120,
    reset_triggers: Sequence[str] = (),
    cooldown_markers: Sequence[str] = (),
    policy: AckPolicy | None = None,
    sent_record: dict | None = None,
) -> ReplyOutcome:
    if policy is None:
        policy = _ack_policy_from_env(reply_bot_id)
    else:
        policy.reply_bot_id = reply_bot_id
    try:
        recent = await collect_recent_messages(client=client, chat_id=chat_id, scan_limit=scan_limit)
    except Exception as exc:
        print(f"cleanup_replied_messages skipped: failed to scan recent messages: {exc}")
        return ReplyOutcome()
    return await _classify_with_fetches(
        client,
        recent=recent,
        chat_id=chat_id,
        topic_id=topic_id,
        own_sender_id=own_sender_id,
        target_message=target_message,
        sent_message_id=sent_message_id,
        scan_limit=scan_limit,
        reset_triggers=reset_triggers,
        cooldown_markers=cooldown_markers,
        policy=policy,
        sent_record=sent_record,
    )


async def cleanup_replied_messages(
    client: TelegramClient,
    chat_id: int | str,
    topic_id: int,
    own_sender_id: int,
    target_message: str,
    reply_bot_id: int,
    sent_message_id: int | None = None,
    scan_limit: int = 120,
    reset_triggers: Sequence[str] = (),
    cooldown_markers: Sequence[str] = (),
) -> tuple[list[int], bool, bool, int | None, str, float | None, int | None]:
    outcome = await gather_reply_outcome(
        client,
        chat_id=chat_id,
        topic_id=topic_id,
        own_sender_id=own_sender_id,
        target_message=target_message,
        reply_bot_id=reply_bot_id,
        sent_message_id=sent_message_id,
        scan_limit=scan_limit,
        reset_triggers=reset_triggers,
        cooldown_markers=cooldown_markers,
    )
    return outcome.as_tuple()


@dataclass
class Config:
    target_raw: str
    target: str | int
    topic_id: int
    message: str
    flow_slug: str
    state_dir: Path
    lock_path: Path
    last_success_path: Path
    last_attempt_path: Path
    cooldown_state_path: Path
    run_max_seconds: int
    dry_run: bool
    wait_until_ts: float | None
    reply_bot_username: str
    post_send_wait_seconds: int
    post_send_max_wait_seconds: int
    post_send_poll_seconds: int
    disable_immediate_resend: bool
    require_ack_reply: bool
    scan_limit: int
    late_ack_scan_limit: int
    reset_trigger_text: str
    reset_triggers: list[str]
    cooldown_markers: list[str]
    cooldown_retry_buffer_seconds: int
    cooldown_retry_max_wait_seconds: int
    cooldown_inline_wait_safety_margin_seconds: int
    min_repeat_seconds: int
    pre_repeat_probe_seconds: int
    pending_sent_max_age_seconds: int

    @property
    def state_key(self) -> str:
        return f"{self.target_raw}|{self.topic_id}|{self.message}"


class SendRun:
    """One scheduled send attempt: send, watch replies, resend as needed."""

    def __init__(self, cfg: Config, client: TelegramClient, entity: object, me_id: int, policy: AckPolicy,
                 *, run_started_monotonic: float | None = None):
        self.cfg = cfg
        self.client = client
        self.entity = entity
        self.me_id = me_id
        self.policy = policy
        self.sent_records: dict[int, dict] = {}
        self.run_started_monotonic = (time.monotonic() if run_started_monotonic is None
                                      else run_started_monotonic)

    async def gather(self, sent_message_id: int | None, *, scan_limit: int | None = None,
                     recent: list[object] | None = None, receipt_message_id: int | None = None,
                     allow_fetches: bool = True) -> ReplyOutcome:
        cfg = self.cfg
        limit = scan_limit if scan_limit is not None else cfg.scan_limit
        if receipt_message_id and recent is None:
            receipt = await fetch_message_by_id(client=self.client, chat_id=cfg.target, message_id=int(receipt_message_id))
            if receipt is not None:
                recent = [receipt]
        if recent is None:
            return await gather_reply_outcome(
                self.client,
                chat_id=cfg.target,
                topic_id=cfg.topic_id,
                own_sender_id=self.me_id,
                target_message=cfg.message,
                reply_bot_id=self.policy.reply_bot_id,
                sent_message_id=sent_message_id,
                scan_limit=limit,
                reset_triggers=cfg.reset_triggers,
                cooldown_markers=cfg.cooldown_markers,
                policy=self.policy,
                sent_record=self.sent_records.get(sent_message_id),
            )
        return await _classify_with_fetches(
            self.client,
            recent=recent,
            chat_id=cfg.target,
            topic_id=cfg.topic_id,
            own_sender_id=self.me_id,
            target_message=cfg.message,
            sent_message_id=sent_message_id,
            scan_limit=limit,
            reset_triggers=cfg.reset_triggers,
            cooldown_markers=cfg.cooldown_markers,
            policy=self.policy,
            sent_record=self.sent_records.get(sent_message_id),
            allow_fetches=allow_fetches,
        )

    async def send(self, label: str) -> object:
        cfg = self.cfg
        await ensure_connected(self.client)
        async def action():
            journal = getattr(self, "pending_journal", None)
            if journal is not None:
                journal.dispatched(self.retry_interval)
            return await self.client.send_message(entity=self.entity, message=cfg.message, reply_to=cfg.topic_id)
        return await send_with_global_gate(
            action,
            label=f"{cfg.flow_slug}:{label}",
            state_dir=cfg.state_dir,
            fact={
                "event_type": "message",
                "identity_id": self.me_id,
                "chat_id": int(cfg.target) if str(cfg.target).lstrip("-").isdigit() else 0,
                "sender_id": self.me_id,
                "text": cfg.message,
                "reply_to_msg_id": cfg.topic_id,
            },
        )

    def inline_wait_budget_seconds(self) -> float:
        cfg = self.cfg
        # The initial delay precedes the full reply-polling window. Reserve
        # both, plus a poll interval and the configured network/write margin,
        # before deciding whether a cooldown can fit into this invocation.
        reply_reserve_seconds = (max(0, cfg.post_send_wait_seconds)
                                 + max(0, cfg.post_send_max_wait_seconds)
                                 + max(0, cfg.post_send_poll_seconds))
        return compute_inline_cooldown_wait_budget_seconds(
            run_started_monotonic=self.run_started_monotonic,
            run_max_seconds=cfg.run_max_seconds,
            max_inline_wait_seconds=cfg.cooldown_retry_max_wait_seconds,
            safety_margin_seconds=cfg.cooldown_inline_wait_safety_margin_seconds + reply_reserve_seconds,
        )

    async def maybe_wait_for_cooldown_retry(
        self,
        *,
        not_before_ts: float,
        source: str,
        cooldown_retry_seconds: int | None = None,
        cooldown_reply_text: str = "",
    ) -> bool:
        cfg = self.cfg
        wait_seconds = max(0, int((not_before_ts - time.time()) + 0.999999))
        if wait_seconds <= 0:
            return True
        inline_wait_budget_seconds = self.inline_wait_budget_seconds()
        if wait_seconds <= inline_wait_budget_seconds:
            print(
                f"Cooldown retry pending for {cfg.state_key}; waiting inline {wait_seconds}s "
                f"before resend (budget {inline_wait_budget_seconds:.1f}s, source={source})"
            )
            await asyncio.sleep(wait_seconds)
            return True

        set_pending_retry(
            cfg.cooldown_state_path,
            cfg.state_key,
            {
                **(load_retry_state(cfg.cooldown_state_path).get(cfg.state_key) or {}),
                "target": str(cfg.target_raw),
                "topic_id": cfg.topic_id,
                "message": cfg.message,
                "not_before_ts": not_before_ts,
                "cooldown_reply_text": cooldown_reply_text,
                "cooldown_retry_seconds": cooldown_retry_seconds,
                "buffer_seconds": cfg.cooldown_retry_buffer_seconds,
                "saved_at_ts": time.time(),
                "source": source,
                "inline_wait_budget_seconds": inline_wait_budget_seconds,
            },
        )
        print(f"Cooldown retry pending for {cfg.state_key}; skip send for another {wait_seconds}s")
        return False

    def schedule_cooldown_retry(self, outcome: ReplyOutcome, *, source: str, inline_wait_budget_seconds: float) -> float:
        cfg = self.cfg
        retry_after_seconds = (outcome.cooldown_retry_seconds or 0) + cfg.cooldown_retry_buffer_seconds
        not_before_ts = time.time() + retry_after_seconds
        set_pending_retry(
            cfg.cooldown_state_path,
            cfg.state_key,
            {
                "target": str(cfg.target_raw),
                "topic_id": cfg.topic_id,
                "message": cfg.message,
                "not_before_ts": not_before_ts,
                "cooldown_reply_text": outcome.cooldown_reply_text,
                "cooldown_retry_seconds": outcome.cooldown_retry_seconds,
                "buffer_seconds": cfg.cooldown_retry_buffer_seconds,
                "saved_at_ts": time.time(),
                "source": source,
                "inline_wait_budget_seconds": inline_wait_budget_seconds,
            },
        )
        return not_before_ts


def build_config() -> Config:
    api_id_raw = env("TG_API_ID", required=True)  # noqa: F841 - validated in run_once
    target_raw = env("TG_TARGET", required=True)
    topic_id_raw = env("TG_TOPIC_ID", required=True)
    message = env("TG_MESSAGE", ".闭关修炼", required=True)

    flow_slug = env("TG_FLOW_SLUG", slugify_message(message))
    state_dir = Path(env("TG_STATE_DIR", "state"))

    try:
        topic_id = int(topic_id_raw)
    except ValueError as exc:
        raise SystemExit("TG_TOPIC_ID must be an integer") from exc

    target: str | int
    try:
        target = int(target_raw)
    except ValueError:
        target = target_raw

    wait_until_raw = env("TG_WAIT_UNTIL_TS", "").strip()

    return Config(
        target_raw=target_raw,
        target=target,
        topic_id=topic_id,
        message=message,
        flow_slug=flow_slug,
        state_dir=state_dir,
        lock_path=Path(env("TG_LOCK_PATH", str(state_dir / f"send_{flow_slug}.lock"))),
        last_success_path=Path(env("TG_LAST_SUCCESS_PATH", str(state_dir / f"last_success_{flow_slug}.json"))),
        last_attempt_path=Path(env("TG_LAST_ATTEMPT_PATH", str(state_dir / f"last_attempt_{flow_slug}.json"))),
        cooldown_state_path=Path(env("TG_COOLDOWN_STATE_PATH", str(state_dir / f"cooldown_retry_{flow_slug}.json"))),
        run_max_seconds=max(30, int(env("TG_RUN_MAX_SECONDS", "180"))),
        dry_run=env("TG_DRY_RUN", "0") == "1",
        wait_until_ts=float(wait_until_raw) if wait_until_raw else None,
        reply_bot_username=env("TG_REPLY_BOT_USERNAME", "fanrenxiuxian_bot"),
        post_send_wait_seconds=int(env("TG_POST_SEND_WAIT_SECONDS", "8")),
        post_send_max_wait_seconds=int(env("TG_POST_SEND_MAX_WAIT_SECONDS", "30")),
        post_send_poll_seconds=max(2, int(env("TG_POST_SEND_POLL_SECONDS", "5"))),
        disable_immediate_resend=env("TG_DISABLE_IMMEDIATE_RESEND", "0") == "1",
        require_ack_reply=True,
        scan_limit=max(20, int(env("TG_SCAN_LIMIT", "60"))),
        late_ack_scan_limit=max(
            max(20, int(env("TG_SCAN_LIMIT", "60"))),
            int(env("TG_LATE_ACK_SCAN_LIMIT", "200")),
        ),
        reset_trigger_text=env("TG_RESET_TRIGGER_TEXT", DEFAULT_RESET_TRIGGER_TEXT),
        reset_triggers=parse_reset_triggers(env("TG_RESET_TRIGGER_TEXT", DEFAULT_RESET_TRIGGER_TEXT)),
        cooldown_markers=parse_cooldown_markers(env("TG_COOLDOWN_MARKER_TEXT", DEFAULT_COOLDOWN_MARKER_TEXT)),
        cooldown_retry_buffer_seconds=max(1, int(env("TG_COOLDOWN_RETRY_BUFFER_SECONDS", "3"))),
        cooldown_retry_max_wait_seconds=max(0, int(env("TG_COOLDOWN_RETRY_MAX_WAIT_SECONDS", "900"))),
        cooldown_inline_wait_safety_margin_seconds=max(0, int(env("TG_COOLDOWN_INLINE_WAIT_SAFETY_MARGIN_SECONDS", "15"))),
        min_repeat_seconds=max(0, int(env("TG_MIN_REPEAT_SECONDS", "120"))),
        pre_repeat_probe_seconds=max(0, int(env("TG_PRE_REPEAT_PROBE_SECONDS", "0"))),
        pending_sent_max_age_seconds=max(0, int(env("TG_PENDING_SENT_MAX_AGE_SECONDS", "0"))),
    )


async def run_once() -> None:
    run_started_monotonic = time.monotonic()
    load_dotenv()

    from miniapp_commands.runner import route_from_environment
    routed = await route_from_environment()
    if routed is not None:
        from miniapp_commands.run_cycle import result_exit_code
        code = result_exit_code(routed, observer_immediate=env("TG_OBSERVER_IMMEDIATE_KICK", "0") == "1")
        if code:
            raise ExpectedFlowExit(code)
        return

    api_id_raw = env("TG_API_ID", required=True)
    api_hash = env("TG_API_HASH", required=True)
    session_name = env("TG_SESSION_NAME", "state/default/session")

    cfg = build_config()
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    paused, pause_path = is_paused(cfg.state_dir, cfg.flow_slug)
    if paused:
        print(f"Paused via {pause_path}; skip send")
        return
    if cfg.flow_slug == "send" and (cfg.state_dir / "pause_send").exists():
        print("Legacy generic send flow is paused via state/pause_send; skip send")
        return

    try:
        api_id = int(api_id_raw)
    except ValueError as exc:
        raise SystemExit("TG_API_ID must be an integer") from exc

    try:
        lock_ctx = flow_lock(cfg.lock_path)
        lock_ctx.__enter__()
    except FlowLockBusy as busy:
        suffix = f" meta={busy.meta_text}" if busy.meta_text else ""
        print(f"Another send_topic_message.py run is active; skip this run. lock={cfg.lock_path}{suffix}")
        return

    client: TelegramClient | None = None
    try:
        write_lock_metadata(
            cfg.lock_path,
            {
                "pid": os.getpid(),
                "flow_slug": cfg.flow_slug,
                "message": cfg.message,
                "target": cfg.target_raw,
                "started_at_ts": time.time(),
                "run_max_seconds": cfg.run_max_seconds,
            },
        )

        session_path = Path(session_name)
        session_path.parent.mkdir(parents=True, exist_ok=True)
        for path in (cfg.cooldown_state_path, cfg.last_success_path, cfg.last_attempt_path):
            path.parent.mkdir(parents=True, exist_ok=True)

        client = telegram_client(session_path, api_id, api_hash)
        await asyncio.wait_for(client.start(), timeout=cfg.run_max_seconds)
        entity = await with_telegram_retry(
            lambda: client.get_entity(cfg.target),
            label="get target entity",
            reconnect=lambda: ensure_connected(client),
        )
        reply_bot = await with_telegram_retry(
            lambda: client.get_entity(cfg.reply_bot_username),
            label="get reply bot entity",
            reconnect=lambda: ensure_connected(client),
        )
        me = await with_telegram_retry(
            client.get_me,
            label="get self",
            reconnect=lambda: ensure_connected(client),
        )
        me_id = getattr(me, "id", 0) if me else 0
        validate_command_identity(cfg.target_raw, cfg.topic_id, me_id,
                                  expected_account=env("XIU_ACCOUNT_SLUG", ""))

        if cfg.dry_run:
            print(f"DRY RUN: would send to {cfg.target_raw} topic {cfg.topic_id}: {cfg.message}")
            if me:
                print(f"Authenticated as: {getattr(me, 'username', None) or me.id}")
            return

        policy = _ack_policy_from_env(getattr(reply_bot, "id", 0))
        run = SendRun(cfg, client, entity, me_id, policy,
                      run_started_monotonic=run_started_monotonic)
        await _run_send_cycle(run)
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass
        lock_ctx.__exit__(None, None, None)


def recovered_reply_status(outcome: ReplyOutcome) -> str:
    if outcome.failed:
        return "failed"
    if outcome.cooldown_retry_seconds is not None:
        return "cooldown_pending"
    if outcome.reset_triggered:
        return "reset_pending"
    return "acknowledged" if outcome.succeeded else "settled"


def persist_reply_state(cfg: Config, payload: dict[str, Any]) -> bool:
    """Use the same success contract for recovery and newly observed replies."""
    is_success = (payload.get("status") == "acknowledged" and payload.get("succeeded") is True
                  and bool(payload.get("ack_reply_message_id")) and not payload.get("failed"))
    persist_attempt_state(cfg.last_attempt_path, payload)
    if is_success:
        persist_attempt_state(cfg.last_success_path, payload)
    try:
        from rare_item_notice import spool_command_receipt
        spool_command_receipt(cfg.state_dir, payload)
    except Exception as exc:
        print(f"Rare reward receipt queue failed: {type(exc).__name__}")
    return is_success


async def _run_send_cycle(run: SendRun) -> None:
    cfg = run.cfg
    if should_stop_before_send(cfg):
        return
    interval = no_reply_interval(minimum=max(120, cfg.post_send_wait_seconds + cfg.post_send_max_wait_seconds))
    journal = PendingCommand(cfg.state_dir, flow=cfg.flow_slug, command=cfg.message,
                             target=cfg.target_raw, topic_id=cfg.topic_id,
                             actor_id=int(getattr(run, "me_id", 0) or 0), clock=time.time)
    run.pending_journal, run.retry_interval = journal, interval
    journal.import_attempt(load_json_dict(cfg.last_attempt_path), interval)
    result_path = cfg.state_dir / "last_result.json"
    reconcile_only = env("TG_RECONCILE_ONLY", "0") == "1"
    # Retain authenticated send evidence even after a journal has settled.
    # An older writer may have omitted receipt metadata that now needs a
    # fresh official readback, including when chat cleanup deleted the send.
    run.sent_records = {int(row["message_id"]): row for row in journal.data.get("attempts", [])
                        if row.get("message_id")}

    def runtime_has_reply_window():
        started = getattr(run, "run_started_monotonic", time.monotonic())
        return cfg.run_max_seconds - (time.monotonic() - started) >= cfg.post_send_wait_seconds + cfg.post_send_max_wait_seconds + 5

    def remember(outcome, *, current):
        status = recovered_reply_status(outcome)
        mid = outcome.command_message_id or (outcome.matched_ids[-1] if outcome.matched_ids else None)
        ts = outcome.acked_at_ts or time.time()
        previous = load_json_dict(result_path)
        used_failure_retries = max(failure_count, int(previous.get("failure_resend_count") or 0)
                                   if previous.get("ack_reply_message_id") == outcome.ack_reply_message_id else 0)
        payload = {"ts": ts, "status": status, "message_id": mid, "sent_message_id": mid,
                   "actual_send_ts": max(float(outcome.command_sent_at_ts or 0),
                       float(load_json_dict(cfg.last_attempt_path).get("actual_send_ts") or 0),
                       max((float(row.get("sent_at_ts") or row.get("submitted_at_ts") or 0)
                            for row in journal.data.get("attempts", [])), default=0)),
                   "command_sent_at_ts": outcome.command_sent_at_ts,
                   "actor_id": int(getattr(run, "me_id", 0) or 0),
                   "message": cfg.message, "flow_slug": cfg.flow_slug, "target": str(cfg.target_raw),
                   "topic_id": cfg.topic_id, "acked": True, "succeeded": outcome.succeeded,
                   "failed": outcome.failed, "ack_reply_message_id": outcome.ack_reply_message_id,
                   "ack_reply_sender_id": outcome.ack_reply_sender_id, "ack_reply_text": outcome.ack_reply_text,
                   "reset_triggered_after_send": outcome.reset_triggered,
                   "cooldown_reply_after_send": outcome.cooldown_reply_text,
                   "cooldown_retry_seconds_after_send": outcome.cooldown_retry_seconds,
                   "daily_mind_remaining": outcome.daily_mind_remaining, "daily_mind_limit": outcome.daily_mind_limit,
                   "duel_anti_repeat": outcome.duel_anti_repeat,
                   "failure_resend_count": used_failure_retries,
                   "completion_reason": "account-matched official business reply",
                   "next_due_ts": ts + cfg.min_repeat_seconds,
                   "command_message_ids": [row.get("message_id") for row in journal.data.get("attempts", [])] if current else [mid]}
        if outcome.cooldown_retry_seconds is not None and not outcome.failed:
            payload["next_due_ts"] = ts + outcome.cooldown_retry_seconds + cfg.cooldown_retry_buffer_seconds
            set_pending_retry(cfg.cooldown_state_path, cfg.state_key, {
                **payload,
                "target": str(cfg.target_raw), "topic_id": cfg.topic_id, "message": cfg.message,
                "not_before_ts": payload["next_due_ts"], "cooldown_retry_seconds": outcome.cooldown_retry_seconds,
                "cooldown_reply_text": outcome.cooldown_reply_text, "source": "official-reply",
                "ack_reply_message_id": outcome.ack_reply_message_id})
        elif not outcome.reset_triggered:
            clear_pending_retry(cfg.cooldown_state_path, cfg.state_key)
        if ts >= float(load_json_dict(result_path).get("ts") or 0):
            persist_attempt_state(result_path, payload)
        persist_reply_state(cfg, payload)
        if current:
            journal.finish(status=status, receipt_id=outcome.ack_reply_message_id, reply_ts=ts)
        return payload

    async def pending_reply(*, recent=None):
        if recent is None:
            recent = await collect_recent_messages(client=run.client, chat_id=cfg.target,
                                                    scan_limit=cfg.late_ack_scan_limit)
        run.sent_records = {int(row["message_id"]): row for row in journal.data.get("attempts", [])
                            if row.get("message_id")}
        outcomes = []
        lookups = set(journal.reply_scan_ids())
        for mid in journal.message_ids:
            try:
                outcome = await asyncio.wait_for(run.gather(mid, scan_limit=cfg.late_ack_scan_limit,
                    recent=recent, allow_fetches=mid in lookups), 10)
            except TimeoutError:
                # One deleted or unavailable old thread cannot consume the
                # next command's full reply window or prevent its retry.
                outcome = ReplyOutcome()
            if outcome.acked:
                if outcome.command_message_id is None:
                    outcome.command_message_id = mid
                outcomes.append(outcome)
        # A delayed success of an earlier copy ends the whole retry chain.
        return max(outcomes, key=lambda item: (item.succeeded, item.acked_at_ts or 0), default=ReplyOutcome())

    failure_limit = max(0, int(env("TG_FAILURE_RESEND_MAX", "0")))
    failure_count = int(journal.data.get("failure_resend_count") or 0) if journal.pending else 0
    if journal.pending:
        if any(not row.get("message_id") and not row.get("resolved") for row in journal.data.get("attempts", [])):
            journal.recover_deliveries(await collect_recent_messages(client=run.client, chat_id=cfg.target, scan_limit=cfg.late_ack_scan_limit))
        outcome = await pending_reply()
        if outcome.acked:
            if outcome.failed and failure_count < failure_limit:
                if reconcile_only:
                    remember(outcome, current=True)
                    return
                failure_count += 1
                journal.reject_attempt(outcome.command_message_id, outcome.ack_reply_message_id)
                journal.data["failure_resend_count"] = failure_count
                journal.data["next_retry_ts"] = time.time()
                journal.save()
            elif outcome.reset_triggered:
                journal.reject_attempt(outcome.command_message_id, outcome.ack_reply_message_id)
            else:
                remember(outcome, current=True)
                return
        if reconcile_only:
            print(f"Read-only reconciliation: no final official reply for {cfg.flow_slug}")
            return
        if time.time() < float(journal.data.get("next_retry_ts") or 0):
            print(f"Awaiting official reply; retry at {journal.data['next_retry_ts']} for {cfg.flow_slug}")
            return
    else:
        # Recover an official reply observed while this scheduled sender was off.
        outcome = await run.gather(None)
        previous_result = load_json_dict(result_path)
        if not outcome.acked and not valid_receipt_identity(
                previous_result, actor_id=run.me_id, flow=cfg.flow_slug, command=cfg.message):
            seen = set()
            for previous in (previous_result, load_json_dict(cfg.last_attempt_path),
                             load_json_dict(cfg.last_success_path)):
                mid = previous.get("sent_message_id") or previous.get("message_id")
                if not mid or mid in seen or previous.get("message") not in (None, cfg.message):
                    continue
                seen.add(mid)
                outcome = await run.gather(int(mid), scan_limit=cfg.late_ack_scan_limit,
                    receipt_message_id=previous.get("ack_reply_message_id") or previous.get("reply_message_id"))
                if outcome.acked:
                    break
        if outcome.acked:
            remember(outcome, current=False)
        if reconcile_only:
            print(f"Read-only reconciliation: {'official reply verified' if outcome.acked else 'no final official reply'} for {cfg.flow_slug}")
            return

    retry_state = load_retry_state(cfg.cooldown_state_path)
    cooldown = retry_state.get(cfg.state_key) if isinstance(retry_state, dict) else None
    cooldown_due = valid_receipt_identity(cooldown, actor_id=run.me_id, flow=cfg.flow_slug, command=cfg.message)
    if cooldown_due:
        not_before = float(cooldown.get("not_before_ts") or 0)
        if not_before > time.time() and not await run.maybe_wait_for_cooldown_retry(
                not_before_ts=not_before, source="official-cooldown",
                cooldown_retry_seconds=cooldown.get("cooldown_retry_seconds"),
                cooldown_reply_text=str(cooldown.get("cooldown_reply_text") or "")):
            return
        clear_pending_retry(cfg.cooldown_state_path, cfg.state_key)

    if not journal.pending and not cooldown_due:
        result = load_json_dict(result_path)
        # Only verified business results establish the ordinary command cadence.
        # An unconfirmed legacy last_success is never a repeat guard.
        if (valid_receipt_identity(result, actor_id=run.me_id, flow=cfg.flow_slug, command=cfg.message)
                and not result.get("reset_triggered_after_send")):
            next_due = float(result.get("next_due_ts") or 0)
            if result.get("succeeded") and cfg.pre_repeat_probe_seconds > 0:
                next_due -= cfg.pre_repeat_probe_seconds
            if next_due > time.time():
                if not (result.get("failed") and int(result.get("failure_resend_count") or 0) < failure_limit):
                    persist_attempt_state(cfg.last_attempt_path, {**result, "status": "skip_duplicate_guard",
                                          "completion_reason": "verified business reply is still within its cadence"})
                    return
                failure_count = int(result.get("failure_resend_count") or 0) + 1

    if cfg.wait_until_ts is not None:
        await maybe_wait_until_target_ts(cfg.wait_until_ts)
    while True:
        if should_stop_before_send(cfg):
            return
        actual_send_ts = time.time()
        journal.begin(interval)
        journal.data["failure_resend_count"] = failure_count
        journal.save()
        base = {"ts": actual_send_ts, "actual_send_ts": actual_send_ts, "status": "send_submitted",
                "message": cfg.message, "flow_slug": cfg.flow_slug, "target": str(cfg.target_raw),
                "topic_id": cfg.topic_id, "acked": False, **journal.retry_payload()}
        persist_attempt_state(cfg.last_attempt_path, base)
        try:
            sent = await run.send("send command awaiting official reply")
            if not getattr(sent, "id", None):
                raise RuntimeError("send returned no Telegram message id")
            journal.sent(sent)
        except BaseException as exc:
            journal.unknown(exc)
            persist_attempt_state(cfg.last_attempt_path, {**base, **journal.retry_payload(),
                "status": "send_outcome_unknown", "error_type": type(exc).__name__,
                "completion_reason": "no business reply; retry remains scheduled"})
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            print(f"Command transport unconfirmed: {type(exc).__name__}; retry remains scheduled")
            return
        base.update(status="sent", message_id=sent.id, sent_message_id=sent.id, **journal.retry_payload())
        persist_attempt_state(cfg.last_attempt_path, base)
        if cfg.post_send_wait_seconds > 0:
            await asyncio.sleep(cfg.post_send_wait_seconds)
        deadline = time.monotonic() + max(0, cfg.post_send_max_wait_seconds)
        while True:
            try:
                recent = await collect_recent_messages(client=run.client, chat_id=cfg.target, scan_limit=cfg.scan_limit)
                outcome = await pending_reply(recent=recent)
            except Exception as exc:
                print(f"Reply reconciliation pending: {type(exc).__name__}")
                outcome = ReplyOutcome()
            if outcome.acked or time.monotonic() >= deadline:
                break
            await asyncio.sleep(cfg.post_send_poll_seconds)
        if not outcome.acked:
            persist_attempt_state(cfg.last_attempt_path, {**base, **journal.retry_payload(),
                "status": "reply_retry_pending", "completion_reason": "no official business reply; retry until related reply"})
            print(f"Command {sent.id} remains unconfirmed; next retry={journal.data['next_retry_ts']}")
            return
        if outcome.failed and failure_count < failure_limit:
            failure_count += 1
            journal.reject_attempt(outcome.command_message_id, outcome.ack_reply_message_id)
            journal.data["failure_resend_count"] = failure_count
            journal.save()
            # Keep the rejected copy out of later reply scans. A replacement
            # always gets its own complete confirmation window.
            if not runtime_has_reply_window():
                persist_attempt_state(cfg.last_attempt_path, {**base, **journal.retry_payload(),
                    "status": "failure_resent_pending_ack", "completion_reason": "explicit-failure retry scheduled"})
                return
            continue
        if outcome.reset_triggered:
            journal.reject_attempt(outcome.command_message_id, outcome.ack_reply_message_id)
            if not runtime_has_reply_window():
                return
            continue
        remember(outcome, current=True)
        print(f"Official reply confirmed command={outcome.command_message_id} status={recovered_reply_status(outcome)}")
        return


async def main() -> int:
    load_dotenv()
    run_max_seconds = max(30, int(env("TG_RUN_MAX_SECONDS", "180")))
    try:
        await asyncio.wait_for(run_once(), timeout=run_max_seconds)
    except ExpectedFlowExit as exc:
        return exc.code
    except asyncio.TimeoutError:
        print(f"send_topic_message.py exceeded TG_RUN_MAX_SECONDS={run_max_seconds}")
        return 124
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
