#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import shutil
import sys
from contextlib import ExitStack, suppress
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv
from telethon import TelegramClient
from runtime import env, log_print
from state_store import FlowLockBusy, flow_lock

from transport import disconnect_telegram_client, ensure_connected, telegram_client, with_telegram_retry
from telegram_monitor_targets import monitor_targets
from event_monitor_runtime import EventMonitorHealth, QUESTION_FLOWS
from flow_registry import registry_path
from nanlonghou_trade import TRADE_FLOWS, validate_trade_policy
import xuangu_quiz as xuangu


def parse_marker_groups(text: str) -> list[list[str]]:
    """Parse trigger marker text into OR groups of AND-matched substrings.

    Syntax:
    - "a|b" -> one group requiring both a and b
    - "a|b||c" -> (a and b) OR c
    """
    groups: list[list[str]] = []
    for raw_group in text.split("||"):
        markers = [part.strip() for part in raw_group.split("|") if part.strip()]
        if markers:
            groups.append(markers)
    return groups


def normalized_event_age_seconds(raw: str, default: int = 1800) -> int:
    """Allow short-lived event contracts while keeping a positive age guard."""
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return max(1, int(default))


def parse_trusted_sender_ids(raw: str) -> set[int]:
    sender_ids = {int(item.strip()) for item in raw.split(",") if item.strip().isdigit()}
    if not sender_ids:
        raise SystemExit("TG_TRIGGER_TRUSTED_SENDER_IDS must contain at least one verified sender ID")
    return sender_ids


async def run_once(
    *,
    on_scan_complete: Callable[[], None] | None = None,
    client_override: TelegramClient | None = None,
) -> None:
    # All registered event senders now use one durable account-bound handler.
    # The historical one-shot path advanced last_success immediately on send;
    # do not leave it callable as a second, less strict production entry point.
    raise ValueError("Event commands require TG_AUTO_REPLY_PERSISTENT=1 and the account-bound receipt monitor")


def normalized_reconcile_seconds(raw: str) -> float:
    """Bound recovery scans without preventing short-deadline push flows."""
    return max(5.0, float(raw))


def _session_sqlite_path(session_name: str | Path) -> Path:
    path = Path(session_name)
    return path if path.suffix == ".session" else Path(f"{path}.session")


async def run_persistent_auto_reply() -> None:
    """Use Telegram updates as the fast path and a slow history scan for recovery."""
    load_dotenv()
    flow_slug = env("TG_FLOW_SLUG", required=True)
    if flow_slug in xuangu.RETIRED_FLOWS:
        raise ValueError("Retired fixed-answer flows are replaced by xuangu_zuoda")
    if flow_slug not in QUESTION_FLOWS:
        raise ValueError("No official receipt policy registered for this event flow")
    if flow_slug in TRADE_FLOWS:
        registry = json.loads(registry_path(Path(__file__).resolve().parent).read_text())
        validate_trade_policy(registry, env("XIU_ACCOUNT_SLUG", required=True), flow_slug,
                              env("TG_AUTO_REPLY_MESSAGE", required=True))
    state_dir = Path(env("TG_STATE_DIR", "state"))
    # Take the process lease before touching the health file, the session
    # copy or Telegram: a second instance is a normal skip, and must not
    # overwrite the running instance's health with its own pid/failure.
    with ExitStack() as lease:
        try:
            lease.enter_context(flow_lock(state_dir / "question_monitor.lock"))
        except FlowLockBusy:
            log_print(f"Another {flow_slug} monitor is running; skip this start.")
            return
        await _run_persistent_auto_reply_locked(flow_slug, state_dir)


async def _run_persistent_auto_reply_locked(flow_slug: str, state_dir: Path) -> None:
    api_id = int(env("TG_API_ID", required=True))
    api_hash = env("TG_API_HASH", required=True)
    session_name = env("TG_SESSION_NAME", "state/default/auto_reply_session")
    listener_name = env("TG_PUSH_SESSION_NAME", f"{session_name}_push")
    source_targets = monitor_targets(env("TG_MONITOR_TARGETS", ""), env("TG_TARGET", required=True))
    target_username = env("TG_TRIGGER_TARGET_USERNAME", "")
    trusted_sender_ids = parse_trusted_sender_ids(env("TG_TRIGGER_TRUSTED_SENDER_IDS", ""))
    max_event_age_seconds = normalized_event_age_seconds(env("TG_TRIGGER_MAX_EVENT_AGE_SECONDS", "1800"))
    reconcile_seconds = normalized_reconcile_seconds(env("TG_AUTO_REPLY_RECONCILE_SECONDS", "60"))

    source_session = _session_sqlite_path(session_name)
    listener_session = _session_sqlite_path(listener_name)
    listener_session.parent.mkdir(parents=True, exist_ok=True)
    if not source_session.exists():
        raise SystemExit(f"Authorized source session missing: {source_session}")
    # The listener owns a separate sqlite file, so its long-lived connection
    # never contends with the bounded reconciliation client's session database.
    if not listener_session.exists():
        shutil.copy2(source_session, listener_session)

    client = telegram_client(
        listener_name,
        api_id,
        api_hash,
        auto_reconnect=True,
        connection_retries=3,
    )
    health = EventMonitorHealth(
        state_dir, env("XIU_ACCOUNT_SLUG", required=True),
        flow_slug, "send", source_targets, reconcile_seconds,
    )
    try:
        await health.verify_listener(client)
        entities = []
        for source_target in source_targets:
            entity = await with_telegram_retry(
                lambda source_target=source_target: client.get_entity(source_target),
                label=f"resolve push target {source_target}",
                reconnect=lambda: ensure_connected(client),
            )
            entities.append(entity)

        from question_reply_monitor import QuestionReplyMonitor
        answer_bank_path = Path(env("TG_ANSWER_BANK_PATH", "state/xuangu_answer_bank.json"))
        answer_bank = xuangu.merge_answer_bank(
            xuangu.parse_answer_bank(env("TG_ANSWER_BANK_TEXT", "")),
            xuangu.load_answer_bank_file(answer_bank_path),
        )
        monitor = QuestionReplyMonitor(
            client, health, list(zip(source_targets, entities)), username=target_username,
            trusted_sender_ids=trusted_sender_ids, flow=flow_slug, state_dir=state_dir,
            state_path=Path(env("TG_LAST_SUCCESS_PATH", str(state_dir / f"last_success_{flow_slug}.json"))),
            max_age=max_event_age_seconds, scan_limit=max(50, int(env("TG_SCAN_LIMIT", "120"))),
            reconcile_seconds=reconcile_seconds, reply_text=env("TG_AUTO_REPLY_MESSAGE", required=True),
            answer_bank=answer_bank,
            answer_bank_path=answer_bank_path,
            auto_update_answer_bank=env("TG_AUTO_UPDATE_ANSWER_BANK", "0") == "1",
            result_marker_groups=parse_marker_groups(env("TG_LEARNED_ANSWER_RESULT_MARKER_TEXT", "玄骨|答案|完全正")),
            result_pattern=env("TG_LEARNED_ANSWER_RESULT_PATTERN", r"答案\s*([A-D])\s*完全正[确確]"),
            learned_max_age=normalized_event_age_seconds(env("TG_LEARNED_ANSWER_MAX_AGE_SECONDS", "86400")),
            dry_run=env("TG_DRY_RUN", "0") == "1",
        )
        await monitor.run()
    except (Exception, SystemExit) as exc:
        health.failed("push_listener_startup", exc)
        raise
    finally:
        health.close()
        with suppress(Exception):
            await disconnect_telegram_client(client, 15)


async def main() -> None:
    load_dotenv()
    run_max_seconds = max(30, int(env("TG_RUN_MAX_SECONDS", "120")))
    persistent = env("TG_AUTO_REPLY_PERSISTENT", "0") == "1"
    if persistent:
        await run_persistent_auto_reply()
        return
    try:
        await asyncio.wait_for(run_once(), timeout=run_max_seconds)
    except asyncio.TimeoutError as exc:
        raise SystemExit(f"auto_reply_topic_event.py exceeded TG_RUN_MAX_SECONDS={run_max_seconds}") from exc


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
