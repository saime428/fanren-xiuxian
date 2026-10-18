#!/usr/bin/env python3
"""Per-account 宗门日常 runner: send each group command once per day and read its official reply.

Order (mirrors the operator's manual sequence):
  1. ``.宗门点卯``
  2. ``.边境军功``  (read-only panel: today's council + whether already supported)
  3. ``.搜集军报``  (skipped when the panel already says 已支援)
  4. ``.公开军报 N`` for the one known true report in the box (skipped when none)
  5. ``.支援慕兰 <action>`` chosen from the published route and the council

Every step is confirmed only by a direct official reply to our own message.
A send is journaled (with its message id) before its reply is awaited; a step
whose reply never arrives stays ``unconfirmed``, and the next tick first looks
up the reply to the journaled message and only re-sends when none exists, at
most ``MAX_STEP_SENDS`` times per day. A rejected/unknown reply is recorded
and the flow moves on so one bad step cannot block the day.
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from command_confirmation import CANONICAL_CHAT_ID, CANONICAL_TOPIC_ID, official_message
from miniapp_shared import daily
from miniapp_shared.accounts import ACCOUNT_USERNAMES, stagger_offsets
from miniapp_shared.dwelling import verify_username
from miniapp_shared.state import LockBusy
from transport import send_with_global_gate

from .protocol import (
    CHECKIN_COMMAND, COLLECT_COMMAND, PANEL_COMMAND, PUBLISH_COMMAND, SUPPORT_COMMAND,
    choose_support_action, known_true_report, parse_checkin, parse_panel, parse_publish,
    parse_report_box, parse_support,
)
from .state import account_dir, account_lock, read_state, write_state

START_HOUR, START_MINUTE = 1, 0
ACCOUNT_MINUTE_OFFSETS = stagger_offsets("sect_daily", 3)
RETRY_SECONDS = 15 * 60
REPLY_TIMEOUT_SECONDS = 40
# A journaled send whose reply was lost is looked up on the next tick before
# anything is re-sent; the official reply follows our message within seconds.
REPLY_LOOKUP_TIMEOUT_SECONDS = 8
STEP_GAP_SECONDS = 4
SCAN_LIMIT = 60
# One command may reach the group at most this many times per day. On
# 2026-09-22 @demo_player_04 sent ``.公开军报 3`` 40 times: a Telethon error
# after the send aborted the run before the step was journaled, and every
# retry treated the step as never sent.
MAX_STEP_SENDS = 2
STEPS = ("checkin", "panel", "collect", "publish", "support")
TERMINAL = {"checked_in", "already_checked_in", "no_sect", "supported", "already_supported",
            "true_report", "false_report", "already_published", "published", "pending",
            "not_supported", "skipped", "not_collected"}


def admitted(account: str, now_ts: float) -> bool:
    return daily.admitted(account, now_ts, start_hour=START_HOUR, start_minute=START_MINUTE,
                          offsets=ACCOUNT_MINUTE_OFFSETS)


class GroupCommander:
    """Send one command to the canonical topic and wait for its direct official reply."""

    def __init__(self, client: Any, *, state_dir: Path, sleep: Any = asyncio.sleep,
                 monotonic: Any = time.monotonic) -> None:
        self.client, self.state_dir = client, Path(state_dir)
        self.sleep, self.monotonic = sleep, monotonic

    async def send(self, text: str) -> int:
        async def action():
            return await self.client.send_message(CANONICAL_CHAT_ID, text, reply_to=CANONICAL_TOPIC_ID)
        message = await send_with_global_gate(action, label=f"sect_daily {text}", state_dir=self.state_dir)
        return int(getattr(message, "id", 0) or 0)

    async def wait_reply(self, sent_id: int, *, timeout: float = REPLY_TIMEOUT_SECONDS) -> dict[str, Any] | None:
        async def read() -> dict[str, Any] | None:
            # Ascending from our own message: the reply sits a few messages after
            # it, so neither a busy group nor a lookup hours later can push it
            # past the scan limit the way a newest-first scan could.
            async for message in self.client.iter_messages(CANONICAL_CHAT_ID, limit=SCAN_LIMIT, min_id=sent_id,
                                                           reverse=True):
                reply = getattr(message, "reply_to", None)
                reply_id = getattr(message, "reply_to_msg_id", None) or getattr(reply, "reply_to_msg_id", None)
                if reply_id == sent_id and official_message(message):
                    return {"id": int(message.id), "sender_id": int(getattr(message, "sender_id", 0) or 0),
                            "text": str(getattr(message, "message", "") or ""), "sent_id": sent_id}
            return None

        return await daily.poll_until(read, timeout=timeout, sleep=self.sleep, monotonic=self.monotonic)

    async def command(self, text: str) -> tuple[int, dict[str, Any] | None]:
        sent_id = await self.send(text)
        if not sent_id:
            return 0, None
        return sent_id, await self.wait_reply(sent_id)


def _step(state: dict[str, Any], name: str) -> dict[str, Any]:
    row = dict((state.get("steps") or {}).get(name) or {})
    if name == "publish" and row.get("status") == "unknown" and row.get("reply_text"):
        reparsed = parse_publish(row["reply_text"])
        if reparsed.get("status") != "unknown":
            row.update(reparsed)
    return row


def _done(step: dict[str, Any]) -> bool:
    return step.get("status") in TERMINAL


async def run_account(root: Path, account: str, *, day_key: str, now: float, bridge_factory: Any,
                      force: bool = False, sleep: Any = asyncio.sleep,
                      monotonic: Any = time.monotonic) -> dict[str, Any]:
    prior = read_state(root, account)
    if prior.get("day") == day_key and prior.get("status") == "completed" and not force:
        return {"ok": True, "account": account, "day": day_key, "status": "daily_already_complete",
                "steps": prior.get("steps", {}), "support": prior.get("support")}
    if not force and not admitted(account, now):
        return {"ok": True, "account": account, "day": day_key, "status": "waiting_admission"}
    if not force and prior.get("day") == day_key and float(prior.get("next_due_at") or 0) > now:
        return {"ok": True, "account": account, "day": day_key, "status": "scheduled_wait",
                "next_due_at": float(prior["next_due_at"])}
    try:
        with account_lock(root, account):
            return await _run_locked(root, account, prior if prior.get("day") == day_key else {},
                                     day_key=day_key, now=now, bridge_factory=bridge_factory, sleep=sleep,
                                     monotonic=monotonic)
    except LockBusy:
        return {"ok": True, "account": account, "day": day_key, "status": "lock_busy"}


async def _run_locked(root: Path, account: str, prior: dict[str, Any], *, day_key: str, now: float,
                      bridge_factory: Any, sleep: Any, monotonic: Any) -> dict[str, Any]:
    steps: dict[str, dict[str, Any]] = {name: _step(prior, name) for name in STEPS}
    result: dict[str, Any] = {"ok": True, "account": account, "username": ACCOUNT_USERNAMES.get(account, ""),
                              "day": day_key, "flow": "sect_daily", "steps": steps, "updated_at": now}
    state_dir = account_dir(root, account)
    os.environ.setdefault("TG_GLOBAL_SEND_GATE_STATE_PATH", str(Path(root) / "state" / "global_telegram_send_gate.json"))
    try:
        async with bridge_factory(root, account) as bridge:
            await verify_username(bridge, account)
            commander = GroupCommander(bridge.client, state_dir=state_dir, sleep=sleep, monotonic=monotonic)

            async def run_step(name: str, text: str, parser: Any) -> dict[str, Any]:
                prior_row = steps[name]
                if _done(prior_row):
                    return prior_row
                from miniapp_commands.catalog import migrated
                if name == "checkin" and migrated(root, account, "sect_checkin"):
                    from contextlib import asynccontextmanager
                    from miniapp_commands.runner import run_flow

                    @asynccontextmanager
                    async def current_bridge(_root, _account):
                        yield bridge

                    api = await run_flow(root, account, "sect_checkin", bridge_factory=current_bridge)
                    evidence = api.get("last_response") or {}
                    parsed = parser(evidence.get("message", ""))
                    row = {"command": text, "transport": "miniapp", "at": time.time(),
                           "status": "unconfirmed", "miniapp_status": api.get("status"),
                           "miniapp_receipt": evidence}
                    if api.get("status") in {"checked_in", "already_checked_in", "no_sect"}:
                        row.update(parsed)
                    else:
                        row["error"] = api.get("error") or api.get("status")
                    steps[name] = row
                    write_state(root, account, {**result, "status": "in_progress"})
                    return row
                same_command = prior_row.get("command") == text
                sends = int(prior_row.get("send_count") or (1 if prior_row.get("sent_message_id") else 0)) if same_command else 0
                sent_id, reply = 0, None
                if same_command and prior_row.get("sent_message_id"):
                    # The command already reached the group; find its reply first.
                    sent_id = int(prior_row["sent_message_id"])
                    reply = await commander.wait_reply(sent_id, timeout=REPLY_LOOKUP_TIMEOUT_SECONDS)
                    if reply is None:
                        if sends >= MAX_STEP_SENDS:
                            steps[name] = {**prior_row, "status": "unconfirmed", "error": "resend_limit_reached",
                                           "send_count": sends}
                            return steps[name]
                        sent_id = 0
                if not sent_id:
                    if sends >= MAX_STEP_SENDS:
                        steps[name] = {**prior_row, "status": "unconfirmed", "error": "resend_limit_reached",
                                       "send_count": sends}
                        return steps[name]
                    sends += 1
                    # Journal the attempt before the RPC: a send that raises or
                    # returns no message id must still count against the cap.
                    row = {"command": text, "sent_message_id": 0, "send_count": sends, "at": time.time(),
                           "status": "unconfirmed", "error": "send_pending"}
                    steps[name] = row
                    write_state(root, account, {**result, "status": "in_progress"})
                    sent_id = await commander.send(text)
                    row.update(sent_message_id=sent_id, error="reply_pending")
                    if sent_id:
                        # Journal the send before waiting: a transport error while
                        # reading the reply must never turn into a second send.
                        write_state(root, account, {**result, "status": "in_progress"})
                        try:
                            reply = await commander.wait_reply(sent_id)
                        except Exception as exc:
                            row.update(error=f"reply_wait_failed:{type(exc).__name__}")
                            raise
                row = {"command": text, "sent_message_id": sent_id, "send_count": sends, "at": time.time()}
                if not sent_id:
                    row.update(status="unconfirmed", error="send_failed")
                elif reply is None:
                    row.update(status="unconfirmed", error="no_official_reply")
                else:
                    parsed = parser(reply["text"])
                    row.update(parsed, reply_message_id=reply["id"], reply_sender_id=reply["sender_id"],
                               reply_text=reply["text"][:600])
                steps[name] = row
                await sleep(STEP_GAP_SECONDS)
                return row

            await run_step("checkin", CHECKIN_COMMAND, parse_checkin)
            panel = await run_step("panel", PANEL_COMMAND, parse_panel)
            council = panel.get("council") or {}
            if panel.get("status") == "supported":
                for name in ("collect", "publish", "support"):
                    if not _done(steps[name]):
                        steps[name] = {"status": "skipped", "reason": "panel_already_supported"}
            else:
                box = await run_step("collect", COLLECT_COMMAND, parse_report_box)
                published_route = ""
                if box.get("status") in {"pending", "published"}:
                    true_report = known_true_report(box.get("reports") or [])
                    if box.get("status") == "published":
                        steps["publish"] = steps["publish"] if _done(steps["publish"]) else {
                            "status": "already_published", "reason": "box_state_published"}
                    elif true_report is None:
                        steps["publish"] = {"status": "skipped", "reason": "no_known_true_report",
                                            "reports": box.get("reports")}
                    elif not _done(steps["publish"]):
                        published = await run_step("publish", f"{PUBLISH_COMMAND} {true_report['number']}", parse_publish)
                        published["expected_route"] = true_report["route"]
                    published = steps["publish"]
                    if published.get("status") == "true_report":
                        published_route = published.get("route") or published.get("expected_route") or ""
                elif not _done(steps["publish"]):
                    steps["publish"] = {"status": "skipped", "reason": f"collect_{box.get('status')}"}
                if not _done(steps["support"]):
                    choice = choose_support_action(council, published_route)
                    support = await run_step("support", f"{SUPPORT_COMMAND} {choice['action']}", parse_support)
                    support["choice"] = choice
    except Exception as exc:  # noqa: BLE001 - never leak transport internals
        result.update(ok=False, status="account_failed", error=type(exc).__name__ if not str(exc) else str(exc)[:80])
    unresolved = [name for name in STEPS if not _done(steps[name])]
    if result.get("status") == "account_failed":
        result["next_due_at"] = now + RETRY_SECONDS
    elif unresolved:
        result.update(status="partial", unresolved=unresolved, next_due_at=now + RETRY_SECONDS)
    else:
        result.update(status="completed", next_due_at=None)
    support = steps.get("support") or {}
    result["support"] = {k: support.get(k) for k in ("status", "route", "outcome", "merit_gain", "merit_total",
                                                      "streak_days", "matched_order", "intel_bonus")}
    checkin = steps.get("checkin") or {}
    result["checkin"] = {k: checkin.get(k) for k in ("status", "contribution", "streak_days", "stipend")}
    write_state(root, account, result)
    return result
