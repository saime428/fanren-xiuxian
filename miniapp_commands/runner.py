"""Existing sender jobs own each migrated flow and its recovery journal."""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path

from account_lifecycle import assert_account_allowed
from command_confirmation import valid_receipt_identity
from miniapp_shared.dwelling import MiniAppError, error_code, make_bridge
from miniapp_shared.priority import yield_to_qyz
from miniapp_shared.state import read_json, write_json, exclusive_lock, LockBusy, next_midnight_ts, day_key
from state_store import flow_lock, FlowLockBusy, is_paused, write_lock_metadata
from .catalog import FLOW_COMMANDS, PERIODS, VERSION, STATE_FILE, RECEIPT_FILE, migrated, state_dir
from .client import CommandClient, result_text
from .protocol import deep_board, yuan_board, number, outcome
from .receipts import receipt

ROOT = Path(__file__).resolve().parents[1]
RETRY_SECONDS = 120
# Consecutive request_failed cycles (login, read or transport errors before any
# mutation) wait longer each time; the interval dispatcher re-runs every 30 s,
# so a fixed 120 s meant a login every two minutes for a whole outage. Any
# cycle that ends without an exception resets the count.
FAILURE_BACKOFF_SECONDS = (120, 300, 900, 1800)
TIMER_GRACE = 3


class Cycle:
    def __init__(self, root, account, flow, state, *, clock=time.time, read_only=False, event=None):
        self.root, self.account, self.flow = Path(root), account, flow
        self.directory = state_dir(root, account, flow)
        self.state, self.clock, self.read_only, self.event = dict(state), clock, read_only, event
        self.state.update(account=account, flow=flow, runtime_version=VERSION, transport="miniapp")
        self.state.setdefault("status", "observing")

    def save(self):
        if not self.read_only:
            self.state["updated_at"] = self.clock()
            write_json(self.directory/STATE_FILE, self.state)

    def result(self, status, *, due=None, **values):
        self.state.pop("failure_count", None)
        self.state.update(status=status, next_due_ts=due, **values)
        self.save()
        return {k: self.state.get(k) for k in ("account", "flow", "transport", "runtime_version", "status",
                    "next_due_ts", "error", "last_success_at", "last_event_key", "last_response") if k in self.state}

    def record(self, reply, operation, parsed, *, request_id=""):
        row = receipt(reply, account=self.account, flow=self.flow, operation=operation,
                      now=self.clock(), status=parsed["status"], succeeded=parsed.get("succeeded", False),
                      request_id=request_id, text=result_text(reply) if reply.get("actionResult") else "")
        self.state["last_response"] = row
        self.state["history"] = [*self.state.get("history", []), row][-12:]
        if parsed.get("succeeded"):
            self.state["last_success_at"] = self.clock()
        if not self.read_only:
            write_json(self.directory/RECEIPT_FILE, row)
        self.save()

    async def mutate(self, client, *, operation="execute"):
        if self.read_only:
            raise MiniAppError("miniapp_readonly_mutation_forbidden")
        if yield_to_qyz(self.flow, self.clock(), self.root):
            raise MiniAppError("yielding_to_qyz")
        if is_paused(self.directory, self.flow)[0]:
            raise MiniAppError("flow_paused")
        if self.event and self.clock() >= self.event["deadline"]:
            raise MiniAppError("event_expired")
        request_id = uuid.uuid4().hex
        # Persist before network I/O, including a crash or cancellation. A later
        # cycle must inspect the official state before it can retry this action.
        self.state["pending"] = {"operation": operation, "request_id": request_id,
                                  "at": self.clock(), "event_key": (self.event or {}).get("key")}
        self.state["status"] = "unconfirmed"
        self.state.pop("error", None)
        self.save()
        try:
            reply = await client.mutate(self.flow, operation=operation)
        except MiniAppError as exc:
            if not exc.uncertain:
                self.state["pending"] = None
                self.save()
            raise
        try:
            parsed = outcome(self.flow, reply, operation=operation)
        except MiniAppError as exc:
            exc.uncertain = True
            raise
        self.record(reply, operation, parsed, request_id=request_id)
        self.state["pending"] = None
        self.state.pop("rate_limit_count", None)
        self.save()
        if parsed.get("succeeded"):
            # Keep the existing rare-item notification inbox, after validating
            # both the actor and the business result (never merely HTTP 200).
            try:
                from rare_item_notice import structured_gains, spool_rewards
                gains = structured_gains({**reply["actionResult"], "message": result_text(reply)}, self.account)
                spool_rewards(self.root, gains, source="miniapp", source_id=f"{self.flow}:{request_id}")
            except Exception as exc:
                self.state["reward_notice_error"] = type(exc).__name__
                self.save()
        return reply, parsed

    async def deep(self, client):
        reply = await client.overview()
        board = deep_board(reply)
        self.record(reply, "overview", {"status": "active" if board["active"] else "idle"})
        # An in-progress retreat is never force-exited, even when a local timer
        # or an old Telegram observer says it should have finished.
        if board["active"] and not board["completed"]:
            self.state["pending"] = None
            return self.result("active", due=self.clock()+max(30, board["remainingSeconds"])+TIMER_GRACE)
        if self.read_only:
            return self.result("read_only", due=self.clock()+60, board=board)
        if board["active"]:
            if board["canSettle"] is not True:
                return self.result("unconfirmed", due=self.clock()+60)
            reply, parsed = await self.mutate(client, operation="settle")
            if parsed["status"] != "settled":
                return self.finish(parsed)
            board = deep_board(reply)
        # After a lost start/settle response, a fresh server board decides. The
        # previous operation is not blindly resent or called a success.
        if board["canStart"] is not True or board["active"]:
            return self.result("unavailable", due=self.clock()+900)
        reply, parsed = await self.mutate(client, operation="start")
        if parsed["status"] in {"started", "active"}:
            return self.result(parsed["status"], due=self.clock()+max(30, deep_board(reply)["remainingSeconds"])+TIMER_GRACE)
        return self.finish(parsed)

    async def yuan(self, client, *, probe_only=False):
        reply = await client.read_command(".元婴状态")
        board = yuan_board(reply)
        self.record(reply, "status", board)
        state, remaining = board["status"], board["remainingSeconds"]
        if state in {"active", "manual_retreat", "unavailable"}:
            self.state["pending"] = None
            wait = max(30, remaining) if remaining is not None else 60 if state == "active" else 900
            return self.result(state, due=self.clock()+wait+TIMER_GRACE)
        if self.read_only or probe_only:
            return self.result("ready" if probe_only else "read_only", due=None, board=board)
        if state == "return_due":
            await self.mutate(client, operation="return")
            reply = await client.read_command(".元婴状态")
            board = yuan_board(reply)
            self.record(reply, "status", board)
            if board["status"] != "idle":
                return self.result("unconfirmed", due=self.clock()+60)
        reply, parsed = await self.mutate(client)
        if parsed["status"] in {"started", "active"}:
            # Read the actual returned timer; never assume that every trip has
            # the default duration or use help text as evidence of an idle soul.
            status_reply = await client.read_command(".元婴状态")
            board = yuan_board(status_reply)
            self.record(status_reply, "status", board)
            if board["status"] == "active":
                return self.result(parsed["status"], due=self.clock()+max(30, board["remainingSeconds"] or 60)+TIMER_GRACE)
            return self.result("unconfirmed", due=self.clock()+60)
        return self.finish(parsed)

    def finish(self, parsed):
        now = self.clock()
        if parsed["status"] == "cooldown":
            due = now + parsed["remainingSeconds"] + TIMER_GRACE
        elif self.flow == "sect_checkin" and parsed["status"] in {"checked_in", "already_checked_in", "no_sect"}:
            due = next_midnight_ts(now)
        elif parsed["status"] == "rejected":
            due = now + 900
        else:
            due = now + (TIMER_GRACE if parsed.get("cooldown_reset") else PERIODS[self.flow])
        if self.event and parsed["status"] == "success":
            self.state["last_event_key"] = self.event["key"]
        return self.result(parsed["status"], due=due)

    async def run(self, client, *, probe_only=False):
        if self.flow == "shendubiguan":
            return await self.deep(client)
        if self.flow == "yuanyingchuqiao":
            return await self.yuan(client, probe_only=probe_only)
        if self.read_only:
            reply = await client.details()
            self.record(reply, "details", {"status": "read_only"})
            return self.result("read_only")
        if self.flow == "shenji_budao":
            if not self.event:
                raise MiniAppError("miniapp_event_required")
            if self.state.get("last_event_key") == self.event["key"]:
                return self.result("event_already_complete", due=self.state.get("next_due_ts"))
            reply = await client.details()
            world = (reply.get("account") or {}).get("smallWorld") or {}
            self.record(reply, "details", {"status": "observed"})
            if world.get("hasWorld") is not True:
                return self.result("unavailable", due=self.clock()+900)
            remaining = number((world.get("actions") or {}).get("edictRemainingSeconds"))
            if remaining is None:
                raise MiniAppError("miniapp_edict_timer_missing")
            if remaining > 0:
                # A cooldown confirms a wait, not that an uncertain sermon was
                # successful. Keep any pending journal until it is reconciled.
                return self.result("cooldown", due=self.clock()+remaining+TIMER_GRACE)
            if (self.state.get("pending") or {}).get("event_key") == self.event["key"]:
                # Sermons are not idempotent. An ambiguous result for this
                # event is retained for review, never repeated next cooldown.
                return self.result("unknown_outcome", due=self.clock()+RETRY_SECONDS)
        _, parsed = await self.mutate(client)
        return self.finish(parsed)


def _legacy_wait(directory, account, flow, now):
    if flow not in {"wendao", "xuexishanlin", "zhaohuanmoying"}:
        return None
    result = read_json(directory/"last_result.json")
    if (valid_receipt_identity(result, actor_id=int(account[2:]), flow=flow, command=FLOW_COMMANDS[flow])
            and not result.get("failed") and result.get("status") in {"acknowledged", "settled", "cooldown_pending"}):
        due = number(result.get("next_due_ts"))
        if due and due > now:
            return due
    return None


async def run_flow(root, account, flow, *, bridge_factory=make_bridge, client_factory=CommandClient,
                   clock=time.time, read_only=False, probe_only=False, event=None, lock_held=False):
    root = Path(root)
    if not migrated(root, account, flow):
        raise MiniAppError("miniapp_flow_not_enabled")
    directory = state_dir(root, account, flow)
    if is_paused(directory, flow)[0]:
        return {"status": "paused", "account": account, "flow": flow}
    yielded = yield_to_qyz(flow, clock(), root)
    if yielded:
        return yielded
    assert_account_allowed(account, path=root/"state/account_lifecycle.json")

    async def locked():
        state = read_json(directory/STATE_FILE)
        if state and (state.get("account") != account or state.get("flow") != flow
                      or state.get("runtime_version") != VERSION):
            raise MiniAppError("miniapp_state_identity_mismatch")
        if state and (not isinstance(state.get("status"), str)
                or (state.get("next_due_ts") is not None and number(state["next_due_ts"]) is None)
                or (state.get("pending") is not None and not isinstance(state["pending"], dict))
                or (state.get("pending") and not (number(state["pending"].get("at")) or 0) > 0)):
            raise MiniAppError("miniapp_state_invalid")
        cycle = Cycle(root, account, flow, state, clock=clock, read_only=read_only, event=event)
        now = clock()
        if event and state.get("last_event_key") == event["key"]:
            return {**cycle.result("event_already_complete", due=state.get("next_due_ts")),
                    "last_response": state.get("last_response")}
        if (flow == "sect_checkin" and state.get("status") in {"checked_in", "already_checked_in", "no_sect"}
                and day_key((state.get("last_response") or {}).get("received_at", 0)) == day_key(now)):
            return cycle.result(state["status"], due=state.get("next_due_ts"))
        pending = state.get("pending") or {}
        # Daily check-in is server-idempotent by date. Other non-queryable
        # mutations with unknown outcomes retain a full cooldown before retry.
        if pending and flow not in {"shendubiguan", "yuanyingchuqiao", "sect_checkin", "shenji_budao"}:
            held_until = (number(pending.get("at")) or now) + PERIODS[flow] + TIMER_GRACE
            if held_until > now and not read_only:
                return cycle.result("unknown_outcome", due=held_until)
        due = number(state.get("next_due_ts"))
        if due and due > now and not read_only:
            return {"status": "scheduled_wait", "account": account, "flow": flow,
                    "next_due_ts": due, "transport": "miniapp", "business_status": state.get("status"),
                    "last_response": state.get("last_response")}
        if not state and not read_only:
            due = _legacy_wait(directory, account, flow, now)
            if due:
                return cycle.result("legacy_wait", due=due)
            legacy_pending = read_json(directory/"command_pending.json")
            if legacy_pending.get("pending") and flow in {"wendao", "xuexishanlin", "zhaohuanmoying"}:
                cycle.state["pending"] = {"operation": "legacy_group_command", "at": now,
                                          "source": "telegram"}
                return cycle.result("unknown_outcome", due=now+PERIODS[flow]+TIMER_GRACE)
        try:
            with exclusive_lock(root/"state/miniapp_commands/api.lock"):
                async with asyncio.timeout(120):
                    async with bridge_factory(root, account) as bridge:
                        client = await client_factory(root, account, bridge).open()
                        return await cycle.run(client, probe_only=probe_only)
        except LockBusy:
            return {"status": "transport_busy", "account": account, "flow": flow}
        except Exception as exc:
            uncertain = bool(cycle.state.get("pending"))
            code = error_code(exc)
            status = "unknown_outcome" if uncertain else "yielding_to_qyz" if code == "yielding_to_qyz" else "request_failed"
            due = clock()+RETRY_SECONDS
            failures = {}
            if status == "request_failed":
                failures["failure_count"] = int(cycle.state.get("failure_count", 0))+1
                due = clock()+FAILURE_BACKOFF_SECONDS[min(failures["failure_count"], len(FAILURE_BACKOFF_SECONDS))-1]
            if code in {"external_action_rate_limited", "http_429", "rate_limited"}:
                retries = min(3, int(cycle.state.get("rate_limit_count", 0))+1)
                cycle.state["rate_limit_count"] = retries
                due = clock()+(300, 900, 3600)[retries-1]
            if uncertain and flow not in {"shendubiguan", "yuanyingchuqiao", "sect_checkin", "shenji_budao"}:
                due = (number(cycle.state["pending"].get("at")) or clock())+PERIODS[flow]+TIMER_GRACE
            return cycle.result(status, due=due, error=code, **failures)

    if lock_held:
        return await locked()
    try:
        with flow_lock(directory/"send.lock"):
            write_lock_metadata(directory/"send.lock", {"pid": os.getpid(), "flow_slug": flow,
                                "started_at_ts": clock(), "run_max_seconds": 120, "transport": "miniapp"})
            return await locked()
    except FlowLockBusy:
        return {"status": "lock_busy", "account": account, "flow": flow}


async def route_from_environment(*, probe_only=False):
    account = os.environ.get("XIU_ACCOUNT_SLUG", "")
    flow = os.environ.get("TG_FLOW_SLUG", os.environ.get("XIU_FLOW_SLUG", ""))
    if not migrated(ROOT, account, flow):
        return None
    result = await run_flow(ROOT, account, flow, probe_only=probe_only,
                            read_only=os.environ.get("TG_DRY_RUN") == "1")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return result
