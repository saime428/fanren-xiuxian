"""One production tick. --inspect is read-only; --check is offline."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

from account_lifecycle import assert_account_allowed
from miniapp_shared.dwelling import make_bridge
from miniapp_shared.notify import Notifier
from miniapp_shared.priority import yield_to_qyz
from miniapp_shared.state import LockBusy, exclusive_lock, write_json
from state_store import is_paused

from . import ACCOUNT, FLOW, PLAYER_ID, VERSION
from .client import LiveClient
from .protocol import ExplorationError, day
from .runtime import Cycle, Store, queue_report

ROOT = Path(__file__).resolve().parents[1]


def drain_reports(store, notifier):
    for row in store.data["outbox"]:
        if row["status"] not in {"queued", "failed"} or row.get("retry_at", 0) > store.clock():
            continue
        row["status"] = "sending"
        store.save()
        result = notifier.send_once(row["key"], row["text"])
        if result.get("status") in {"delivered", "already_delivered"}:
            row["status"] = "sent"
            receipt = result.get("receipt") or {}
            row["message_id"] = receipt.get("message_id")
        elif "unconfirmed" in str(result.get("error", "")):
            row["status"] = "delivery_uncertain"
        else:
            row["status"] = "failed"
            row["retry_at"] = store.clock() + 300
        store.save()


async def execute(root=ROOT, *, inspect_only=False, bridge_factory=make_bridge,
                  client_factory=LiveClient, notifier_factory=Notifier, clock=time.time):
    root = Path(root)
    directory = root / "state/accounts" / ACCOUNT / FLOW
    def guard():
        if is_paused(directory, FLOW)[0]:
            raise ExplorationError("flow_paused")
        assert_account_allowed(ACCOUNT, path=root / "state/account_lifecycle.json")
        if yield_to_qyz(FLOW, clock(), root):
            raise ExplorationError("yielding_to_qyz")
    try:
        with exclusive_lock(directory / "send.lock"):
            store = Store(root, clock=clock, persist=not inspect_only)
            state = store.data
            notifier = notifier_factory(root, FLOW)
            try:
                guard()
            except ExplorationError as exc:
                if exc.code in {"yielding_to_qyz", "flow_paused"}:
                    return {"status": exc.code, "account": ACCOUNT, "version": VERSION}
                raise
            if not inspect_only and state.get("next_due_at", 0) > clock():
                drain_reports(store, notifier)
                return {"status": "scheduled_wait", "version": VERSION,
                        "account": ACCOUNT, "next_due_at": state["next_due_at"],
                        "pending": bool(state.get("pending"))}
            try:
                with exclusive_lock(root / "state/miniapp_commands/api.lock"):
                    async with bridge_factory(root, ACCOUNT) as bridge:
                        client = client_factory(root, bridge, state["calls"], clock=clock)
                        cycle = Cycle(store, client, clock=clock, guard=guard)
                        try:
                            result = await asyncio.wait_for(cycle.run(read_only=inspect_only), timeout=240)
                        except Exception as exc:
                            code = getattr(exc, "code", type(exc).__name__)
                            count = state.get("consecutive_errors", 0) + 1
                            state["consecutive_errors"] = count
                            wait = 60 if code in {"yielding_to_qyz", "flow_paused"} else 120 if code == "awaiting_server_daily_reset" else (300, 900, 3600)[min(count - 1, 2)]
                            result = cycle.result("awaiting_confirmation" if state.get("pending") else "blocked",
                                                  next_due=clock() + wait, error=str(code))
                        else:
                            state["consecutive_errors"] = 0
            except LockBusy:
                return {"status": "api_busy", "account": ACCOUNT, "version": VERSION}
            except Exception as exc:
                code = str(getattr(exc, "code", type(exc).__name__))
                state.update(status="blocked", error=code, next_due_at=clock()+300)
                store.save()
                result = {"status": "blocked", "account": ACCOUNT, "version": VERSION,
                          "error": code, "next_due_at": state["next_due_at"]}
            if not inspect_only:
                if state.get("journey") or state.get("error") not in {"awaiting_server_daily_reset", "yielding_to_qyz", "flow_paused"}:
                    queue_report(store)
                drain_reports(store, notifier)
                result["notification"] = [{"key": row["key"], "status": row["status"], "message_id": row.get("message_id")}
                                          for row in state["outbox"][-3:]]
                result["updated_at"] = clock()
                write_json(directory / "summary.json", result)
            else:
                result["board"] = state.get("board")
                result["observed_day"] = state.get("observed_day")
                result["existing"] = state.get("existing")
            return result
    except LockBusy:
        return {"status": "lock_busy", "account": ACCOUNT, "version": VERSION}
    except Exception as exc:
        return {"status": "blocked", "account": ACCOUNT, "version": VERSION,
                "error": str(getattr(exc, "code", type(exc).__name__))}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--account", default=ACCOUNT, choices=[ACCOUNT])
    args = parser.parse_args(argv)
    if args.check:
        result = {"ok": True, "version": VERSION, "account": ACCOUNT, "actor_id": PLAYER_ID,
                  "daily_start": "00:00 Asia/Shanghai", "journey_mode": "deep", "node_daily_limit": 1,
                  "rift_priority": ["贪狼", "太阴"], "journey_priority": ["太阴", "贪狼"],
                  "node_priority": ["贪狼", "太阴"], "miniapp_first": True}
    else:
        result = asyncio.run(execute(inspect_only=args.inspect))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 1 if result.get("status") in {"blocked", "awaiting_confirmation"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
