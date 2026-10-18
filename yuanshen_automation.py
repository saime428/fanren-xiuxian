"""Account-scoped 第二元神 automation; the two CLI wrappers share this sole writer.

Only direct official replies or attributed completion pushes authorize actions.
Unknown sends are journaled first, reconciled, then retried from fresh panels.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import time
from pathlib import Path
from typing import Any

from account_lifecycle import AccountPausedError, assert_account_allowed
from command_confirmation import CANONICAL_CHAT_ID, CANONICAL_TOPIC_ID, OFFICIAL_REPLY_SENDER_IDS, official_message
from miniapp_shared.accounts import ACCOUNT_USERNAMES, accounts_for
from miniapp_shared.priority import yield_to_qyz
from miniapp_shared.state import LockBusy, day_key, exclusive_lock, next_midnight_ts, read_json, write_json
from reply_parsing import parse_cooldown_retry_delay_seconds
from transport import send_with_global_gate

# The one account enabling the yuanshen feature.
ACCOUNT = next(iter(accounts_for("yuanshen")), None)
USERNAME = ACCOUNT_USERNAMES.get(ACCOUNT, "")
ACTOR_ID = int(ACCOUNT[2:]) if ACCOUNT else 0
STATUS_COMMAND = ".第二元神"
# Read legacy journals, but never send the verbose panel automatically.
DEMON_COMMAND = ".五子同心魔"
PURGE_COMMAND = ".元神镇魔"
TRAIN_COMMAND = ".元神修炼"
COMMANDS = (STATUS_COMMAND, PURGE_COMMAND, TRAIN_COMMAND)
SOUL_STATUSES = {"idle", "training", "injured", "heart_demon"}
# Operator policy borrowed from the reference client, NOT server limits.
PURGE_THRESHOLD = 60
PURGE_DAILY_LIMIT = 2
RETRY_SECONDS = 30 * 60
TRAIN_RETRY_SECONDS = 120
PAUSED_RETRY_SECONDS = 600  # a shattered account stays paused for hours
TRAIN_STATUS_POLL_SECONDS = 60
INJURY_STATUS_POLL_SECONDS = 300
BUFFER_SECONDS = 60
TRAIN_SECONDS = 24 * 3600
# Confirmed by the operator on 2026-09-29; distinct from our spend policy.
PURGE_COOLDOWN_SECONDS = 6 * 3600
# Local read cadence, not an assertion about corruption growth or server limits.
PURGE_CHECK_SECONDS = 6 * 3600
STATE_RELATIVE = Path(f"state/yuanshen/{ACCOUNT}/state.json")
ROOT = Path(__file__).resolve().parent


def retry_deadline(state: dict[str, Any], flow: str) -> float:
    pending = state.get("pending") or {}
    owner = state.get("retry_flow") or pending.get("flow")
    if owner is None:
        owner = "purge" if pending.get("command") in (DEMON_COMMAND, PURGE_COMMAND) else "train"
    return float(state.get("retry_until", 0)) if owner == flow else 0.0


def duration(text: str) -> int:
    """Sum every duration in ``text``; only for the panel's own 剩余 segment."""
    units = {"天": 86400, "小时": 3600, "分钟": 60, "分": 60, "秒": 1}
    return sum(int(number) * units[unit] for number, unit in
               re.findall(r"(\d+)\s*(小时|分钟|天|分|秒)", text))


def moran(text: str) -> int | None:
    matches = re.findall(r"魔染(?:度)?\s*[:：]?\s*(\d+)(?:\s*(?:→|->|=>)\s*(\d+))?", text)
    if not matches:
        return None
    value = int(matches[-1][1] or matches[-1][0])
    return value if 0 <= value <= 100 else None


def parse_reply(command: str, text: str) -> dict[str, Any]:
    """Fail closed on unrecognized/refused text; never infer success from a title."""
    if "尚未凝练第二元神" in text:
        return {"status": "not_unlocked"}
    if re.search(r"(?:冷[却卻]|[还還][需要]|[请請]等待|稍候|稍[后後])", text):
        # The shared parser anchors on the remaining-time phrase (both scripts),
        # so an unrelated duration in the same reply is not added to the wait.
        wait = parse_cooldown_retry_delay_seconds(text, [])
        if wait:
            return {"status": "cooldown", "wait": wait}
    if command in (STATUS_COMMAND, TRAIN_COMMAND) and re.search(r"【你的第二元神[:：]", text):
        match = re.search(r"状态[:：]\s*(窍中温养|修炼中|受伤|心魔试炼中)(?:\s*[（(]剩余[:：]\s*([^）)\n]+)[）)])?", text)
        if match:
            status = {"窍中温养": "idle", "修炼中": "training", "受伤": "injured",
                      "心魔试炼中": "heart_demon"}[match[1]]
            return {"status": status, "wait": duration(match[2] or ""), "moran": moran(text)}
    if command == DEMON_COMMAND and "【五子同心魔】" in text and moran(text) is not None:
        return {"status": "demon_panel", "moran": moran(text)}
    if command == PURGE_COMMAND and "【元神镇魔】" in text:
        change = re.search(r"魔染度\s*[:：]\s*(\d+)\s*(?:→|->)\s*(\d+)", text)
        cost = re.search(r"耗去\s*(\d+)\s*点修为", text)
        if change and cost and 0 <= int(change[2]) < int(change[1]) <= 100:
            return {"status": "purged", "moran_before": int(change[1]), "moran": int(change[2]),
                    "cultivation_cost": int(cost[1])}
    if command == TRAIN_COMMAND:
        if "你的第二元神已开始闭关修炼" in text and re.search(r"24\s*小时", text):
            return {"status": "training_started", "wait": TRAIN_SECONDS}
        if "无法分心修炼" in text:
            for label, status in (("心魔试炼中", "heart_demon"), ("修炼中", "training"), ("受伤", "injured")):
                if f"({label})" in text or f"（{label}）" in text:
                    return {"status": status, "wait": 0}
    return {"status": "unknown"}


def injury_countdown(state: dict[str, Any], now: float) -> dict[str, Any] | None:
    """Read a current-cycle official injury countdown from the runner's ledger.

    Elapsed time authorizes a bounded recovery attempt, never a fabricated idle
    state or successful training receipt. A newer positive countdown wins.
    """
    if (state.get("version") != 1 or state.get("account") != ACCOUNT
            or state.get("actor_id") != ACTOR_ID):
        return None
    generation = state.get("last_training_started_at", 0)
    if type(generation) not in (int, float) or not math.isfinite(generation) or generation <= 0:
        return None
    candidates = []
    for attempt in state.get("attempts", []):
        reply = attempt.get("reply") or {}
        at, mid, sent = reply.get("at"), reply.get("id"), attempt.get("sent_id")
        if (attempt.get("command") != STATUS_COMMAND or attempt.get("status") != "confirmed"
                or reply.get("sender_id") not in OFFICIAL_REPLY_SENDER_IDS
                or type(at) not in (int, float) or not math.isfinite(at)
                or not generation <= at <= now or now - at > 7 * 86400
                or type(mid) is not int or type(sent) is not int or not mid > sent > 0):
            continue
        parsed = parse_reply(STATUS_COMMAND, str(reply.get("text") or ""))
        if parsed["status"] == "injured" and 0 < parsed.get("wait", 0) <= 7 * 86400:
            candidates.append({"id": mid, "at": at, "ends_at": at + parsed["wait"]})
    return max(candidates, key=lambda row: row["id"]) if candidates else None


def training_due(state: dict[str, Any], now: float) -> float:
    """Reconcile only the old six-hour no-countdown fallback, not real locks."""
    due = float(state.get("due", {}).get("train", 0))
    flow = state.get("flow_status", {}).get("train", {})
    evidence = injury_countdown(state, now)
    if (flow.get("status") != "injured" or evidence is None
            or evidence["ends_at"] + BUFFER_SECONDS > now):
        return due
    observed = float(flow.get("at", 0))
    if (not math.isfinite(observed) or not evidence["at"] <= observed <= now
            or abs(due - observed - 6 * 3600) > 2):
        return due
    return min(due, observed + INJURY_STATUS_POLL_SECONDS)


class Deferred(RuntimeError):
    pass


class Commander:
    def __init__(self, client: Any, root: Path, *, clock: Any = time.time, sleep: Any = asyncio.sleep):
        self.client, self.root, self.clock, self.sleep = client, Path(root), clock, sleep

    def guard(self) -> None:
        if yield_to_qyz("yuanshen", self.clock(), self.root):
            raise Deferred("yielding_to_qyz")

    async def watermark(self) -> int:
        messages = await self.client.get_messages(CANONICAL_CHAT_ID, limit=1)
        return int(messages[0].id) if messages else 0

    async def send(self, command: str) -> int:
        if command not in COMMANDS:
            raise ValueError("command_not_allowed")
        self.guard()

        async def action():
            # Recheck after waiting for the shared send gate, not only at tick start.
            self.guard()
            return await self.client.send_message(CANONICAL_CHAT_ID, command, reply_to=CANONICAL_TOPIC_ID)

        message = await send_with_global_gate(action, label=f"yuanshen {command}",
                                              state_dir=self.root / STATE_RELATIVE.parent)
        return int(message.id)

    async def recover_id(self, attempt: dict[str, Any]) -> int:
        if attempt.get("sent_id"):
            return int(attempt["sent_id"])
        async for message in self.client.iter_messages(CANONICAL_CHAT_ID, min_id=attempt["watermark"],
                                                        reverse=True, limit=200):
            reply = getattr(message, "reply_to", None)
            parent = getattr(message, "reply_to_msg_id", None) or getattr(reply, "reply_to_msg_id", None)
            if (getattr(message, "sender_id", None) == ACTOR_ID and parent == CANONICAL_TOPIC_ID
                    and getattr(message, "message", "") == attempt["command"]
                    and getattr(message, "fwd_from", None) is None):
                return int(message.id)
        return 0

    async def reply(self, sent_id: int, *, timeout: float = 35) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        while True:
            async for message in self.client.iter_messages(CANONICAL_CHAT_ID, min_id=sent_id,
                                                            reverse=True, limit=200):
                reply = getattr(message, "reply_to", None)
                parent = getattr(message, "reply_to_msg_id", None) or getattr(reply, "reply_to_msg_id", None)
                if parent == sent_id and official_message(message, chat_id=CANONICAL_CHAT_ID):
                    return {"id": int(message.id), "sender_id": int(message.sender_id),
                            "text": str(message.message or ""), "at": message.date.timestamp()}
            if time.monotonic() >= deadline:
                return None
            self.guard()
            await self.sleep(3)


class Runner:
    def __init__(self, root: Path, state: dict[str, Any], commander: Any, *, clock: Any = time.time,
                 threshold: int = PURGE_THRESHOLD, daily_limit: int = PURGE_DAILY_LIMIT):
        self.root, self.state, self.commander, self.clock = Path(root), state, commander, clock
        self.threshold, self.daily_limit = threshold, daily_limit

    def save(self) -> None:
        self.state.update(version=1, account=ACCOUNT, username=USERNAME, actor_id=ACTOR_ID,
                          updated_at=self.clock())
        write_json(self.root / STATE_RELATIVE, self.state)

    def reconcile_injury_wait(self) -> None:
        due = training_due(self.state, self.clock())
        if due < float(self.state.get("due", {}).get("train", 0)):
            self.state["due"]["train"] = due
            self.state["injury_wait_reconciled_at"] = self.clock()
            self.save()

    def can_probe_injury_recovery(self, parsed: dict[str, Any]) -> bool:
        evidence = injury_countdown(self.state, self.clock())
        if (parsed["status"] != "injured" or parsed.get("wait") or evidence is None
                or evidence["ends_at"] + BUFFER_SECONDS > self.clock()
                or self.state.get("training_needs_reconcile")):
            return False
        # Intent is durable before a send. Even a lost reply consumes this one
        # probe; recovery must reconcile it instead of blindly sending again.
        return not any(attempt.get("command") == TRAIN_COMMAND
                       and float(attempt.get("at", 0)) >= evidence["ends_at"]
                       for attempt in self.state.get("attempts", []))

    def wait(self, status: str, seconds: float, *, both: bool = True, flow: str = "train") -> dict[str, Any]:
        due = self.state.setdefault("due", {})
        for key in (("train", "purge") if both else (flow,)):
            due[key] = max(float(due.get(key, 0)), self.clock() + max(0, seconds))
            self.state.setdefault("flow_status", {})[key] = {"status": status, "at": self.clock()}
        self.state["status"] = status
        self.save()
        return {"status": status, "due": dict(due), "account": ACCOUNT}

    def apply_return(self) -> bool:
        from yuanshen_return import INBOX_RELATIVE, completion_kind, current_return
        from yuanshen_heart_demon import current_resolution

        resolution = current_resolution(self.root, self.state, self.clock())
        if resolution is not None:
            due = self.state.setdefault("due", {})
            failed = resolution["status"] == "break_failed"
            due["train"] = (resolution["resolved_at"] + resolution["wait_seconds"] + BUFFER_SECONDS
                            if failed else min(float(due.get("train", self.clock())), self.clock()))
            # Re-read an official panel before the next training command. The
            # listener never mutates this runner's timers or purge budget.
            self.state["training_needs_status"] = True
            self.state["last_heart_demon"] = {k: resolution[k] for k in
                                              ("id", "status", "resolved_at", "result_message_id")}
            self.state.setdefault("flow_status", {})["train"] = {
                "status": "heart_demon_injured" if failed else "heart_demon_resolved",
                "at": resolution["resolved_at"],
            }
            if self.state.get("retry_flow", "train") == "train" and not self.state.get("pending"):
                self.state["retry_until"] = 0
            self.save()
            return True

        event = current_return(self.state, read_json(self.root / INBOX_RELATIVE), self.clock())
        if event is None:
            return False
        due = self.state.setdefault("due", {})
        changed = due.get("train", 0) > event["at"] or self.state.get("last_training_return", {}).get("id") != event["id"]
        if changed:
            self.state["last_training_return"] = dict(event)
            due["train"] = min(float(due.get("train", event["at"])), event["at"])
            self.state["training_needs_status"] = False
            # A prior training wait is superseded, not another flow's pending RPC
            # or an explicit server/FloodWait deadline (kept separately).
            pending = self.state.get("pending") or {}
            pending_flow = pending.get("flow", "purge" if pending.get("command") == PURGE_COMMAND else "train")
            if pending_flow == "train" and self.state.get("retry_flow", "train") == "train":
                self.state["retry_until"] = 0
            self.state.setdefault("flow_status", {})["train"] = {
                "status": completion_kind(event["text"]), "at": event["at"]}
            self.save()
        return True

    async def recover(self) -> None:
        pending = self.state.get("pending")
        if not pending:
            return
        sent_id = await self.commander.recover_id(pending)
        if sent_id:
            pending["sent_id"] = sent_id
            self.save()
            reply = await self.commander.reply(sent_id, timeout=0)
            if reply:
                self.record_reply(pending, reply)
                # JSON reload breaks the original pending/ledger object alias.
                for row in self.state.get("attempts", []):
                    if row.get("at") == pending.get("at") and row.get("command") == pending.get("command"):
                        row.update(pending)
        # The fresh status query below, rather than a blind resend, determines
        # whether another mutation is safe. The old attempt remains in the ledger.
        self.state.pop("pending", None)
        self.save()

    def record_reply(self, attempt: dict[str, Any], reply: dict[str, Any]) -> dict[str, Any]:
        parsed = parse_reply(attempt["command"], reply["text"])
        attempt.update(status="confirmed" if parsed["status"] != "unknown" else "unrecognized_reply",
                       reply=reply, parsed=parsed)
        self.state["last_reply"] = {"command": attempt["command"], **parsed, "at": reply["at"]}
        if attempt["command"] == TRAIN_COMMAND and parsed["status"] != "unknown":
            self.state["training_needs_reconcile"] = False
        if attempt["command"] == TRAIN_COMMAND and parsed["status"] == "training_started":
            self.state["last_training_started_at"] = reply["at"]
            self.state["training_needs_status"] = True
            self.state.setdefault("due", {})["train"] = float(reply["at"]) + TRAIN_SECONDS + BUFFER_SECONDS
            returned = self.state.get("last_training_return") or {}
            if int(attempt.get("sent_id", 0)) > int(returned.get("id", 0)) > 0:
                returned["continued_by_sent_id"] = attempt["sent_id"]
                returned["continued_by_reply_id"] = reply["id"]
        if attempt["command"] == TRAIN_COMMAND and parsed["status"] == "cooldown":
            due = self.state.setdefault("due", {})
            self.state["train_not_before"] = max(float(self.state.get("train_not_before", 0)),
                                                 float(reply["at"]) + parsed["wait"] + BUFFER_SECONDS)
            due["train"] = max(float(due.get("train", 0)), self.state["train_not_before"])
        if attempt["command"] == PURGE_COMMAND:
            if parsed["status"] == "purged":
                self.state["last_purge_confirmed_at"] = reply["at"]
                ready = float(reply["at"]) + PURGE_COOLDOWN_SECONDS
            elif parsed["status"] == "cooldown":
                ready = float(reply["at"]) + parsed["wait"] + BUFFER_SECONDS
            else:
                ready = 0
            self.state["purge_ready_at"] = max(float(self.state.get("purge_ready_at", 0)), ready,
                float(self.state.get("last_purge_confirmed_at", 0)) + PURGE_COOLDOWN_SECONDS)
        self.save()
        return parsed

    async def command(self, command: str, *, flow: str) -> dict[str, Any]:
        self.commander.guard()
        watermark = await self.commander.watermark()
        attempt = {"command": command, "flow": flow, "at": self.clock(), "watermark": watermark, "sent_id": 0,
                   "status": "unconfirmed"}
        ledger = self.state.setdefault("attempts", [])
        ledger.append(attempt)
        self.state["attempts"] = ledger[-200:]
        self.state["pending"] = attempt
        self.state["retry_until"] = self.clock() + (TRAIN_RETRY_SECONDS if flow == "train" else RETRY_SECONDS)
        self.state["retry_flow"] = flow
        # Reserve spend BEFORE sending. A crash cannot reset the daily budget.
        if command == TRAIN_COMMAND:
            self.state["training_needs_reconcile"] = True
        if command == PURGE_COMMAND:
            self.state["purge_spends"] = int(self.state.get("purge_spends", 0)) + 1
            self.state["purge_ready_at"] = max(float(self.state.get("purge_ready_at", 0)),
                                                self.clock() + PURGE_COOLDOWN_SECONDS)
        self.save()
        attempt["sent_id"] = await self.commander.send(command)
        self.save()
        reply = await self.commander.reply(attempt["sent_id"])
        if reply is None:
            raise Deferred("unconfirmed")
        parsed = self.record_reply(attempt, reply)
        self.state.pop("pending", None)
        self.state["retry_until"] = 0
        self.save()
        return parsed

    def blocked(self, parsed: dict[str, Any], *, flow: str = "train") -> dict[str, Any] | None:
        status = parsed["status"]
        retry = TRAIN_RETRY_SECONDS if flow == "train" else RETRY_SECONDS
        defaults = {"training": TRAIN_STATUS_POLL_SECONDS, "injured": 6 * 3600, "heart_demon": 1800,
                    "not_unlocked": 7 * 86400, "unknown": retry, "cooldown": retry}
        injury = injury_countdown(self.state, self.clock()) if flow == "train" and status == "injured" else None
        if injury and injury["ends_at"] + BUFFER_SECONDS <= self.clock():
            defaults["injured"] = INJURY_STATUS_POLL_SECONDS
        if status in defaults:
            wait = parsed.get("wait", 0) + BUFFER_SECONDS if parsed.get("wait") else defaults[status]
            if flow == "train" and status in SOUL_STATUSES:
                self.state["training_needs_status"] = True
            if flow == "purge":
                wait = max(wait, float(self.state.get("purge_ready_at", 0)) - self.clock())
            return self.wait("manual_heart_demon" if status == "heart_demon" else status,
                             wait, both=False, flow=flow)
        return None

    def purge_gate(self) -> dict[str, Any] | None:
        """Known ineligibility needs neither a panel query nor a Telegram session."""
        now = self.clock()
        cooldown = float(self.state.get("purge_ready_at", 0)) - now
        if (self.state.get("budget_day") == day_key(now)
                and int(self.state.get("purge_spends", 0)) >= self.daily_limit):
            return self.wait("purge_budget_exhausted", max(cooldown, next_midnight_ts(now) - now + BUFFER_SECONDS),
                             both=False, flow="purge")
        if cooldown > 0:
            return self.wait("purge_cooldown", cooldown, both=False, flow="purge")
        return None

    async def run(self, flow: str) -> dict[str, Any]:
        now = self.clock()
        if self.state.get("budget_day") != day_key(now):
            self.state.update(budget_day=day_key(now), purge_spends=0)
        pending_train = (self.state.get("training_needs_reconcile", False)
                         or (self.state.get("pending") or {}).get("command") == TRAIN_COMMAND)
        await self.recover()
        self.apply_return()
        if flow == "train":
            self.reconcile_injury_wait()
        pending_train = self.state.get("training_needs_reconcile", pending_train)
        due = max(float(self.state.get("due", {}).get(flow, 0)), float(self.state.get("transport_not_before", 0)),
                  float(self.state.get("train_not_before", 0)) if flow == "train" else 0)
        if due > self.clock():
            return {"status": "scheduled_wait", "next_due_at": due, "account": ACCOUNT}
        if flow == "train":
            if pending_train or self.state.get("training_needs_status", bool(self.state.get("last_training_started_at"))):
                # A 24h estimate is only a fallback, not completion evidence.
                # A fresh return push bypasses this probe; uncertain sends do not.
                panel = await self.command(STATUS_COMMAND, flow=flow)
                if not self.can_probe_injury_recovery(panel):
                    blocked = self.blocked(panel)
                    if blocked:
                        return blocked
                    if panel["status"] != "idle":
                        return self.wait("training_unconfirmed", TRAIN_RETRY_SECONDS, both=False)
                    self.state["training_needs_status"] = False
            trained = await self.command(TRAIN_COMMAND, flow=flow)
            if trained["status"] == "training_started":
                return self.wait("training_started", max(0, self.state["due"]["train"] - self.clock()), both=False)
            if trained["status"] in {"training", "injured", "heart_demon"}:
                panel = await self.command(STATUS_COMMAND, flow=flow)
                return self.blocked(panel) or self.wait("retry_training", TRAIN_RETRY_SECONDS, both=False)
            return self.blocked(trained) or self.wait("training_unconfirmed", TRAIN_RETRY_SECONDS, both=False)

        # The concise panel contains both soul phase and corruption. A training
        # timer does not prevent purging; only corruption and purge gates decide.
        gated = self.purge_gate()
        if gated:
            return gated
        panel = await self.command(STATUS_COMMAND, flow=flow)
        if panel["status"] not in SOUL_STATUSES:
            return self.blocked(panel, flow="purge") or self.wait("unknown_moran", RETRY_SECONDS, both=False, flow="purge")
        value = panel.get("moran")
        if value is None:
            return self.wait("unknown_moran", RETRY_SECONDS, both=False, flow="purge")
        self.state["moran"] = value
        if value < self.threshold:
            return self.wait("moran_below_threshold", PURGE_CHECK_SECONDS, both=False, flow="purge")
        purged = await self.command(PURGE_COMMAND, flow=flow)
        if purged["status"] != "purged":
            return self.blocked(purged, flow="purge") or self.wait("purge_unconfirmed", RETRY_SECONDS, both=False, flow="purge")
        self.state["moran"] = purged["moran"]
        return self.wait("purged", self.state["purge_ready_at"] - self.clock(), both=False, flow="purge")


async def run_cycle(root: Path, flow: str, *, bridge_factory: Any = None, clock: Any = time.time,
                    commander_factory: Any = Commander, threshold: int = PURGE_THRESHOLD,
                    daily_limit: int = PURGE_DAILY_LIMIT) -> dict[str, Any]:
    if flow not in {"train", "purge"} or not 1 <= threshold <= 100 or not 1 <= daily_limit <= 2:
        raise ValueError("invalid_policy")
    root = Path(root)
    yielded = yield_to_qyz("yuanshen", clock(), root)
    if yielded:
        return yielded
    try:
        with exclusive_lock(root / STATE_RELATIVE.parent / "runner.lock"):
            state = read_json(root / STATE_RELATIVE)
            if state and (state.get("account") != ACCOUNT or state.get("actor_id") != ACTOR_ID
                          or state.get("version") != 1):
                return {"status": "invalid_state", "ok": False}
            if flow == "train":
                reconciler = Runner(root, state, None, clock=clock)
                reconciler.apply_return()
                reconciler.reconcile_injury_wait()
            due = max(float(state.get("due", {}).get(flow, 0)), retry_deadline(state, flow),
                      float(state.get("transport_not_before", 0)),
                      float(state.get("train_not_before", 0)) if flow == "train" else 0,
                      float(state.get("purge_ready_at", 0)) if flow == "purge" else 0)
            if due > clock():
                return {"status": "scheduled_wait", "next_due_at": due, "account": ACCOUNT}
            if flow == "purge":
                gate = Runner(root, state, None, clock=clock, threshold=threshold, daily_limit=daily_limit).purge_gate()
                if gate:
                    return gate
            try:
                assert_account_allowed(ACCOUNT, path=root / "state/account_lifecycle.json")
            except AccountPausedError:
                # A definite local refusal: no bridge, no get_me, no attempt.
                return {"status": "account_paused", "account": ACCOUNT}
            if bridge_factory is None:
                from miniapp_shared.dwelling import make_bridge
                bridge_factory = make_bridge
            os.environ.setdefault("TG_GLOBAL_SEND_GATE_STATE_PATH", str(root / "state/global_telegram_send_gate.json"))
            runner = None
            try:
                async with bridge_factory(root, ACCOUNT) as bridge:
                    me = await bridge.client.get_me()
                    if int(me.id) != ACTOR_ID or str(me.username or "").lower() != USERNAME:
                        raise ValueError("account_identity_mismatch")
                    commander = commander_factory(bridge.client, root, clock=clock)
                    runner = Runner(root, state, commander, clock=clock, threshold=threshold, daily_limit=daily_limit)
                    return await runner.run(flow)
            except Deferred as exc:
                status = str(exc)
                if runner:
                    return runner.wait(status, TRAIN_RETRY_SECONDS if flow == "train" else RETRY_SECONDS,
                                       both=False, flow=flow)
                return {"status": status, "ok": False}
            except AccountPausedError:
                # Paused between the check above and the send: nothing reached
                # Telegram. Not a failure to retry every TRAIN_RETRY_SECONDS.
                if runner:
                    return runner.wait("account_paused", PAUSED_RETRY_SECONDS, both=False, flow=flow)
                return {"status": "account_paused", "account": ACCOUNT}
            except Exception as exc:
                # Never include transport error strings or credentials in logs.
                state["error"] = type(exc).__name__
                if runner:
                    seconds = float(getattr(exc, "seconds", 0) or 0)
                    if seconds > 0:
                        state["transport_not_before"] = max(float(state.get("transport_not_before", 0)),
                                                             clock() + seconds + BUFFER_SECONDS)
                    flood_wait = max(TRAIN_RETRY_SECONDS if flow == "train" else RETRY_SECONDS,
                                     seconds + BUFFER_SECONDS)
                    return {**runner.wait("failed", flood_wait, both=False, flow=flow), "error": type(exc).__name__, "ok": False}
                # The bridge or get_me failed before a Runner existed. Persist a
                # backoff anyway: with due unchanged, the return listener would
                # re-kick (and reconnect) on every group message.
                seconds = float(getattr(exc, "seconds", 0) or 0)
                state["transport_not_before"] = max(float(state.get("transport_not_before", 0)), clock() + (
                    seconds + BUFFER_SECONDS if seconds > 0 else RETRY_SECONDS))
                Runner(root, state, None, clock=clock).save()
                return {"status": "failed", "error": type(exc).__name__, "ok": False,
                        "next_due_at": state["transport_not_before"]}
    except LockBusy:
        return {"status": "lock_busy", "account": ACCOUNT}


def main(flow: str, argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"@{USERNAME} 元神 {flow}; dry-run unless --send")
    parser.add_argument("--send", action="store_true", help="allow real commands in the canonical topic")
    parser.add_argument("--check", action="store_true", help="offline configuration and state check")
    parser.add_argument("--threshold", type=int, default=PURGE_THRESHOLD)
    parser.add_argument("--daily-purge-limit", type=int, choices=(1, 2), default=PURGE_DAILY_LIMIT)
    args = parser.parse_args(argv)
    if not 1 <= args.threshold <= 100:
        parser.error("threshold must be between 1 and 100")
    if not args.send or args.check:
        state = read_json(ROOT / STATE_RELATIVE)
        result = {"status": "offline_check" if args.check else "dry_run", "account": ACCOUNT,
                  "username": USERNAME, "flow": flow, "chat_id": CANONICAL_CHAT_ID,
                  "topic_id": CANONICAL_TOPIC_ID, "state_path": str(ROOT / STATE_RELATIVE),
                  "state_status": state.get("status", "not_started"), "purge_threshold": args.threshold,
                  "daily_purge_limit": args.daily_purge_limit, "observed_cost_per_purge": 5000,
                  "purge_cooldown_seconds": PURGE_COOLDOWN_SECONDS,
                  "purge_check_seconds": PURGE_CHECK_SECONDS, "status_command": STATUS_COMMAND,
                  "training_completion_mode": "official_return_push_with_status_fallback",
                  "training_status_poll_seconds": TRAIN_STATUS_POLL_SECONDS,
                  "training_retry_seconds": TRAIN_RETRY_SECONDS,
                  "commands": list(COMMANDS), "network_used": False}
    else:
        result = asyncio.run(run_cycle(ROOT, flow, threshold=args.threshold, daily_limit=args.daily_purge_limit))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 1 if result.get("status") in {"failed", "invalid_state", "unknown", "unknown_panel", "unknown_moran",
                                          "unconfirmed", "purge_unconfirmed", "training_unconfirmed",
                                          "manual_heart_demon", "purge_budget_exhausted"} else 0
