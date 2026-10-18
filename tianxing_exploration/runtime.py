"""Durable single-identity exploration cycle with receipt-driven preparation."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
import uuid
from pathlib import Path

from miniapp_shared.dwelling import safe_text
from miniapp_shared.state import read_json, write_json

from . import ACCOUNT, FLOW, PLAYER_ID, USERNAME, VERSION
from .protocol import (COMMANDS, RIFT_SECONDS, ExplorationError, accept_preparation, action_text,
                       active, apply_fate, choose_star, day, group_result, journey_result,
                       midnight, parse_board, parse_wild, required_preparation)


class Store:
    def __init__(self, root, *, clock=time.time, persist=True):
        self.directory = Path(root) / "state/accounts" / ACCOUNT / FLOW
        self.path = self.directory / "state.json"
        self.clock, self.persist = clock, persist
        self.data = read_json(self.path)
        if self.data and (self.data.get("actor_id") != PLAYER_ID or self.data.get("flow") != FLOW
                          or self.data.get("status") == "malformed_state"):
            raise ExplorationError("exploration_state_invalid")
        if not self.data:
            self.data = {"actor_id": PLAYER_ID, "account": ACCOUNT, "flow": FLOW, "version": VERSION,
                         "day": day(clock()), "events": [], "calls": {}, "pending": None,
                         "rift_next_at": 0, "node_next_at": 0, "outbox": [], "observed_day": None}
        if not isinstance(self.data.get("pending"), (dict, type(None))):
            raise ExplorationError("pending_state_invalid")
        self.roll_day()

    def roll_day(self):
        if self.data["day"] != day(self.clock()):
            self.data["day"] = day(self.clock())
            self.data["calls"] = {}
            self.data.pop("journey", None)
            self.data.pop("board_start", None)
            self.data.pop("day_error", None)

    def save(self):
        self.data.update(version=VERSION, updated_at=self.clock())
        if self.persist:
            write_json(self.path, self.data)

    def begin(self, kind, command, **extra):
        if self.data.get("pending"):
            raise ExplorationError("unresolved_previous_action", uncertain=True)
        self.data["pending"] = {"id": uuid.uuid4().hex, "kind": kind, "command": command,
                                "at": self.clock(), "day": self.data["day"], **extra}
        self.save()

    def sent(self, receipt):
        if self.data.get("pending"):
            self.data["pending"].update(receipt, transport="group")
            self.save()

    def finish(self, event):
        pending = self.data.get("pending") or {}
        row = {"id": pending.get("id", uuid.uuid4().hex), "day": pending.get("day", self.data["day"]),
               "kind": pending.get("kind"), "at": self.clock(), **event}
        if not any(e.get("id") == row["id"] for e in self.data["events"]):
            self.data["events"] = (self.data["events"] + [row])[-300:]
        self.data["pending"] = None
        self.save()


class Cycle:
    def __init__(self, store, client, *, clock=time.time, sleep=asyncio.sleep, guard=lambda: None):
        self.store, self.client, self.clock, self.sleep, self.guard = store, client, clock, sleep, guard
        self.s = store.data
        self.synced = False

    def check(self):
        self.guard()
        if day(self.clock()) != self.s["day"]:
            raise ExplorationError("day_changed")

    async def refresh_board(self):
        reply = await self.client.command(".天机盘")
        board = parse_board(action_text(reply), self.clock())
        previous = self.s.get("board")
        if previous and board["debt"] > previous["debt"]:
            self.s.setdefault("debt_hold_level", previous["debt"])
        self.s["board"] = board
        if "debt_hold_level" in self.s:
            if board["debt"] > self.s["debt_hold_level"]:
                raise ExplorationError("new_fate_debt")
            self.s.pop("debt_hold_level")
        self.s.setdefault("board_start", copy.deepcopy(board))
        self.synced = True
        self.store.save()

    async def adopt_history(self):
        history = await self.client.existing_actions()
        if history.get("observed_day") == self.s["day"]:
            self.s["observed_day"] = self.s["day"]
        latest = {}
        for row in history.get("actions", []):
            kind = row.get("kind") or next((k for k, v in COMMANDS.items() if v == row.get("command")), None)
            if kind in COMMANDS and row["at"] >= latest.get(kind, {}).get("at", 0):
                latest[kind] = row
        for kind, row in latest.items():
            known = self.s.get(f"{kind}_history_at", 0)
            if row["at"] <= known:
                continue
            if row["status"] in {"pending", "unconfirmed"}:
                if self.clock() - row["at"] < (RIFT_SECONDS if kind == "rift" else 86400):
                    raise ExplorationError(f"manual_{kind}_unconfirmed")
                continue
            if row.get("confirmed"):
                if kind == "rift":
                    self.s["rift_next_at"] = row["at"] + RIFT_SECONDS + 3
                else:
                    self.s["node_done_day"] = day(row["at"])
                self.s.setdefault("existing", {})[kind] = {
                    "at": row["at"], "status": row["status"], "receipt": row.get("receipt"),
                    "source": "existing_account_receipt"}
            elif row["status"] == "cooldown":
                self.s[f"{kind}_next_at"] = row["at"] + row["remaining_seconds"] + 3
            self.s[f"{kind}_history_at"] = row["at"]
        self.store.save()

    async def prepare(self, kind):
        self.check()
        if any((self.s.get("board", {}).get(f) or {}).get("known") is not True for f in ("prediction", "protection")):
            await self.refresh_board()
        commands = required_preparation(self.s["board"], kind, self.clock())
        for command in commands:
            self.check()
            self.store.begin("preparation", command,
                             transport="miniapp" if command.split()[0] in self.client.commands else "group")
            reply = await self.client.command(command, on_sent=self.store.sent)
            self.s["board"] = accept_preparation(self.s["board"], command, action_text(reply), self.clock())
            self.store.finish({"command": command, "status": "confirmed", "receipt": reply.get("_receipt")})
            await self.sleep(.7)
        if not all(active(self.s["board"], field, self.clock()) for field in ("prediction", "protection")):
            raise ExplorationError("exploration_effects_not_ready")
        if self.s["board"]["star"] != choose_star(kind, self.s["board"]["available"]):
            raise ExplorationError("wrong_exploration_star")

    async def observe(self):
        if self.s.get("observed_day") == self.s["day"]:
            return
        self.check()
        self.store.begin("preparation", ".观命", transport="miniapp" if ".观命" in self.client.commands else "group")
        reply = await self.client.command(".观命", on_sent=self.store.sent)
        self.s["board"] = accept_preparation(self.s["board"], ".观命", action_text(reply), self.clock())
        self.s["observed_day"] = self.s["day"]
        self.store.finish({"command": ".观命", "status": "confirmed", "receipt": reply.get("_receipt")})

    async def consume(self, kind, reply, parsed, *, recovered=False):
        receipt = reply.get("_receipt") or {}
        at = float(receipt.get("at") or self.clock())
        pending = self.s["pending"]
        event = {"status": parsed["status"], "confirmed": parsed.get("confirmed", False),
                 "star": self.s["board"]["star"], "receipt": receipt,
                 "summary": safe_text(parsed.get("summary") or action_text(reply), 1600),
                 "cultivation_delta": parsed.get("cultivation_delta"), "loot": parsed.get("loot", []),
                 "fate": parsed.get("fate", {})}
        if parsed.get("confirmed"):
            if kind == "journey":
                self.s["journey"] = parsed["state"]
                event.update(count=parsed["state"]["count"], mode=parsed["mode"])
            elif kind == "rift":
                self.s["rift_next_at"] = at + RIFT_SECONDS + 3
                self.s["rift_history_at"] = at
            elif kind == "node":
                self.s["node_done_day"] = day(at)
                self.s["node_history_at"] = at
            if not recovered:
                self.s["board"] = apply_fate(self.s["board"], parsed)
        elif parsed["status"] == "cooldown":
            self.s[f"{kind}_next_at"] = at + parsed["remaining_seconds"] + 3
        elif parsed["status"] == "unavailable":
            self.s[f"{kind}_next_at"] = midnight(self.clock())
        else:
            pending["last_reply"] = safe_text(action_text(reply), 1000)
            self.store.save()
            raise ExplorationError("group_outcome_unconfirmed", uncertain=True)
        mismatched_star = parsed.get("star") and parsed["star"] != event["star"]
        self.store.finish(event)
        if mismatched_star:
            raise ExplorationError("external_fate_star_change")
        # Nodes lack a documented consumption contract; one query per node is
        # necessary. Normal rift/journey receipts usually provide both effects.
        if not recovered and any(self.s["board"][f].get("known") is not True for f in ("prediction", "protection")):
            await self.refresh_board()

    async def resume_pending(self):
        pending = self.s.get("pending")
        if not pending:
            return
        kind = pending["kind"]
        if kind == "preparation":
            # The fresh authoritative board supersedes uncertain preparation.
            # Re-arm only a currently missing effect, never replay blindly.
            if pending["command"] == ".观命":
                if pending["day"] == self.s["day"]:
                    history = await self.client.existing_actions()
                    if history.get("observed_day") != self.s["day"]:
                        raise ExplorationError("observation_outcome_pending", uncertain=True)
                    self.s["observed_day"] = self.s["day"]
            self.store.finish({"command": pending["command"], "status": "reconciled_from_board"})
            return
        if kind == "journey":
            if pending["day"] != self.s["day"]:
                self.store.finish({"status": "previous_day_outcome_unconfirmed", "confirmed": False})
                return
            before = pending["before_count"]
            count = self.s["journey"]["count"]
            if count > before:
                self.store.finish({"status": "recovered_server_counter", "confirmed": False,
                                   "count": count, "summary": "服务器次数已推进；该次模式和收益未单独确认"})
                return
            raise ExplorationError("journey_outcome_pending", uncertain=True)
        if pending.get("transport") == "group" or pending.get("command_id"):
            reply = await self.client.recover_group(pending)
            if reply:
                parsed = group_result(kind, action_text(reply))
                if parsed["status"] != "unconfirmed":
                    # The board was read after the original action; do not
                    # subtract its effects twice during receipt recovery.
                    await self.consume(kind, reply, parsed, recovered=True)
                    return
            raise ExplorationError("group_outcome_pending", uncertain=True)
        raise ExplorationError("miniapp_exploration_outcome_pending", uncertain=True)

    def due(self):
        now = self.clock()
        journey = self.s.get("journey") or {}
        values = [midnight(now)]
        if journey.get("count", 0) < journey.get("limit", 8):
            values.append(max(now + 60, journey.get("ready_at", 0), now + journey.get("remaining_seconds", 0)))
        values.append(max(now + 30, self.s.get("rift_next_at", 0)))
        if self.s.get("node_done_day") != self.s["day"]:
            values.append(max(now + 30, self.s.get("node_next_at", 0)))
        return min(values)

    async def run(self, *, read_only=False):
        detail = await self.client.open()
        self.s["journey"] = parse_wild(detail, self.s["day"])
        await self.refresh_board()
        await self.resume_pending()
        await self.adopt_history()
        if read_only:
            return self.result("inspected")
        now = self.clock()
        actionable = (self.s["journey"]["count"] < self.s["journey"]["limit"]
                      or now >= self.s.get("rift_next_at", 0)
                      or (self.s.get("node_done_day") != self.s["day"] and now >= self.s.get("node_next_at", 0)))
        if actionable:
            await self.observe()
        actions = 0
        while self.s["journey"]["available"] and actions < 8:
            if day(self.clock()) != self.s["day"]:
                return self.result("day_changed", next_due=self.clock() + 30)
            if not self.s["journey"]["deep_available"]:
                raise ExplorationError("deep_mode_unavailable")
            await self.prepare("journey")
            self.check()
            before = self.s["journey"]["count"]
            self.store.begin("journey", "wild_experience:deep", before_count=before, transport="miniapp")
            try:
                reply = await self.client.journey()
            except ExplorationError as exc:
                if exc.uncertain or exc.code not in {"wild_experience_daily_limit", "wild_experience_unavailable", "wild_experience_cooldown"}:
                    raise
                self.store.finish({"status": "not_executed", "confirmed": False, "error": exc.code})
                self.s["journey"] = parse_wild(await self.client.details(), self.s["day"])
                break
            parsed = journey_result(reply, before, self.s["day"])
            await self.consume("journey", reply, parsed)
            actions += 1
            await self.sleep(1)
        # When both are due, nodes run first so a retained prediction can be
        # used by the following rift instead of leaving an extra preparation.
        for kind in ("node", "rift"):
            if day(self.clock()) != self.s["day"]:
                return self.result("day_changed", next_due=self.clock() + 30)
            if self.clock() < self.s.get(f"{kind}_next_at", 0):
                continue
            if kind == "node" and self.s.get("node_done_day") == self.s["day"]:
                continue
            await self.prepare(kind)
            self.check()
            command = COMMANDS[kind]
            self.store.begin(kind, command, transport="miniapp" if command in self.client.commands else "group")
            reply = await self.client.command(command, on_sent=self.store.sent)
            await self.consume(kind, reply, group_result(kind, action_text(reply)))
            await self.sleep(1)
        return self.result("waiting", next_due=self.due())

    def result(self, status, *, next_due=None, error=None):
        self.s["status"] = status
        self.s["next_due_at"] = next_due if next_due is not None else self.due()
        if error:
            self.s["error"] = error
        else:
            self.s.pop("error", None)
        self.store.save()
        return {"status": status, "flow": FLOW, "account": ACCOUNT, "actor_id": PLAYER_ID,
                "day": self.s["day"], "version": VERSION, "journey": self.s.get("journey"),
                "node_done_day": self.s.get("node_done_day"), "rift_next_at": self.s.get("rift_next_at"),
                "next_due_at": self.s["next_due_at"], "error": error,
                "calls": self.s.get("calls"), "pending": bool(self.s.get("pending"))}


def report_text(state):
    today = state["day"]
    events = [e for e in state.get("events", []) if e.get("day") == today]
    actions = [e for e in events if e.get("kind") in {"journey", "rift", "node"}]
    confirmed = [e for e in actions if e.get("confirmed")]
    journey, board = state.get("journey") or {}, state.get("board") or {}
    lines = [f"【天星宗探索报告】{today} 北京时间", f"@{USERNAME}",
             f"深入游历：服务器 {journey.get('count', '?')}/{journey.get('limit', '?')}；本流程确认 {sum(e['kind']=='journey' for e in confirmed)} 次深入"]
    wild_events = [e for e in confirmed if e["kind"] == "journey"]
    if wild_events:
        wins = sum(e.get("status") == "victory" for e in wild_events)
        losses = sum(e.get("status") in {"defeat", "failure"} for e in wild_events)
        saved = sum((e.get("fate") or {}).get("protection_consumed") is True for e in wild_events)
        lines.append(f"游历结算：胜利 {wins}，失败 {losses}，改命触发 {saved}（触发数可与胜负重叠）")
    deltas = [e["cultivation_delta"] for e in wild_events if type(e.get("cultivation_delta")) in {int, float}]
    if deltas:
        lines.append(f"深入游历已确认修为变化：{sum(deltas):+g}" + ("（另有未确认项）" if len(deltas) < len(wild_events) else ""))
    loot = {}
    for event in wild_events:
        for item in event.get("loot", []):
            if item.get("name") and type(item.get("quantity")) is int:
                loot[item["name"]] = loot.get(item["name"], 0) + item["quantity"]
    if loot:
        lines.append("深入游历物品：" + "、".join(f"{name}×{quantity}" for name, quantity in loot.items()))
    for kind, name in (("rift", "裂缝"), ("node", "节点")):
        rows = [e for e in confirmed if e["kind"] == kind]
        existing = (state.get("existing") or {}).get(kind)
        if rows:
            for row in rows:
                label = {"success": "成功", "rescued": "改命救回", "settled": "已结算", "settled_failure": "失败结算"}.get(row["status"], row["status"])
                lines.append(f"{name} · {row.get('star') or '?'}：{label}；{row.get('summary') or '已确认结算'}")
        elif existing and day(existing["at"]) == today:
            lines.append(f"{name}：已核对当日既有回执，本流程未重复执行")
        else:
            lines.append(f"{name}：本流程暂无已确认结算")
    preparation = [e for e in events if e.get("kind") == "preparation" and e.get("status") == "confirmed"]
    predictions = sum(e.get("command") == ".推命 探索" for e in preparation)
    protections = sum(e.get("command") == ".改命 探索" for e in preparation)
    rescues = sum((e.get("fate") or {}).get("protection_consumed") is True for e in confirmed)
    lines.append(f"本流程补推命 {predictions} 次，补改命 {protections} 次；回执确认改命触发 {rescues} 次")
    recovered_preparations = sum(e.get('kind') == 'preparation' and e.get('status') == 'reconciled_from_board' for e in events)
    if recovered_preparations:
        lines.append(f"另有 {recovered_preparations} 条准备命令由查盘恢复，费用未逐条确认")
    start = state.get("board_start") or {}
    lines.append(f"天机值 {start.get('points', '?')} → {board.get('points', '?')}；逆命劫 {start.get('debt', '?')} → {board.get('debt', '?')}（命盘／已确认回执）")
    calls = state.get("calls", {})
    lines.append(f"调用：Mini App 查询 {calls.get('miniapp_reads', 0)}，操作 {calls.get('miniapp_writes', 0)}；群发送 {calls.get('group_messages', 0)}")
    if state.get("error"):
        lines.append(f"待处理：{state['error']}；未确认的动作不会重复发送")
    if state.get("pending"):
        lines.append(f"结果待确认：{state['pending'].get('command', '')}")
    from .protocol import TZ
    from datetime import datetime
    if state.get("rift_next_at", 0) > state.get("updated_at", 0):
        at = datetime.fromtimestamp(state["rift_next_at"], TZ).strftime("%m-%d %H:%M:%S")
        lines.append(f"裂缝下次到期：{at}")
    if state.get("node_done_day") != today:
        lines.append("节点仍有待办，以官方回执为准")
    if state.get("next_due_at"):
        at = datetime.fromtimestamp(state["next_due_at"], TZ).strftime("%m-%d %H:%M:%S")
        lines.append(f"下次检查：{at}")
    return "\n".join(lines)[:3400]


def queue_report(store):
    state = store.data
    content = {"day": state["day"], "count": (state.get("journey") or {}).get("count"),
               "events": [e.get("id") for e in state.get("events", []) if e.get("day") == state["day"]],
               "rift_next": state.get("rift_next_at"), "node_day": state.get("node_done_day"),
               "error": state.get("error"), "pending": (state.get("pending") or {}).get("id")}
    key = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()[:24]
    if any(row["key"] == key for row in state["outbox"]):
        return
    state["outbox"] = (state["outbox"] + [{"key": key, "text": report_text(state), "status": "queued", "at": store.clock()}])[-100:]
    store.save()
