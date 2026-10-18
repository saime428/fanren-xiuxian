#!/usr/bin/env python3
"""Per-account 藏娇阁 loop over both concubines, entirely inside the dwelling MiniApp.

For each concubion (active first, then the resident one after ``.召回侍妾``):

  1. ``.远航归来``   when a voyage has ended (settles rewards)
  2. ``.天机代卜``   when its 720-minute cooldown has passed (180 修为, random buff)
  3. ``.入梦寻图``   when its 480-minute cooldown has passed (one 残纹)
  4. ``.坠魔心劫``   when its 720-minute cooldown has passed (raid 护持)
  4b. ``.共历心劫``  when its 600-minute cooldown has passed — the only step that
      runs in the group (reply chain, see ``heart_trial.py``); 稳/稳/稳
  5. ``.拼图``       only when a chart shows 4/4
  6. ``.侍妾远航 <route>`` when not sailing (均衡 8 h by default; 月殿寻痕 for 南宫婉)
     (never for companions explicitly listed in ``state.NO_SAIL``)

Steps 2-4 are refused while the concubine is sailing, so the order above lets
the 8-hour 均衡 voyage line up with the 8-hour 寻图 cooldown: every ~8 h each
concubine returns, divines/seeks/protects if due, and sails again. The loop
finishes by recalling the concubine that was active at the start.

Only the server reply (``actionResult``) and the refreshed ``dwelling.companions``
timers count as evidence; nothing is inferred from local clocks.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from miniapp_shared.accounts import ACCOUNT_USERNAMES
from miniapp_shared.dwelling import (
    DWELLING_EXTERNAL, DWELLING_START, JsonTransport, MiniAppError, error_code, open_dwelling, verify_username,
)
from miniapp_shared.state import LockBusy, day_key, next_midnight_ts

from .heart_trial import HeartTrialCommander, run_heart_trial
from .protocol import (
    COMMAND_CENTER, ROUTES, choose_route, classify, companions_from_payload, result_ok, result_text,
)
from .state import ROUTE_OVERRIDES, account_dir, account_lock, read_state, sailing_disabled, write_state

RETRY_SECONDS = 15 * 60
UNCERTAIN_RETRY_SECONDS = 30 * 60
MAX_IDLE_WAIT_SECONDS = 4 * 3600
STEP_GAP_SECONDS = 2.0
WAKE_GRACE_SECONDS = 20
# 共历心劫 is the only group step and its MiniApp cooldown only moves when a
# trial settles, so a persistent failure would otherwise retry (and post
# ``.我的侍妾``) every 15 minutes all day. Back off per consecutive failure and
# cap the starts per companion per Asia/Shanghai day; a settled trial resets.
HEART_TRIAL_BACKOFF_SECONDS = (15 * 60, 30 * 60, 60 * 60, 120 * 60)
HEART_TRIAL_DAILY_ATTEMPTS = 6


def official_transport() -> JsonTransport:
    return JsonTransport({DWELLING_START: False, DWELLING_EXTERNAL: False, COMMAND_CENTER: True}, timeout=25.0)


class PavilionClient:
    def __init__(self, transport: Any, dwelling: Any, heart_trial: Any = None) -> None:
        self.transport, self.dwelling, self.heart_trial = transport, dwelling, heart_trial
        self.last_payload: dict[str, Any] = dict(dwelling.start_payload or {})

    async def command(self, text: str) -> dict[str, Any]:
        payload = await self.transport(COMMAND_CENTER, {**self.dwelling.base_payload(), "command": text}, mutation=True)
        if isinstance(payload, dict) and payload.get("dwelling"):
            self.last_payload = payload
        return payload

    def companions(self, now_ts: float) -> list[dict[str, Any]]:
        return companions_from_payload(self.last_payload, now_ts=now_ts)


def _due(ready_ts: float, now_ts: float) -> bool:
    return ready_ts <= now_ts


def _trial_gate(entry: dict[str, Any] | None, now_ts: float) -> float:
    """Earliest start for the next 共历心劫: the failure backoff, or tomorrow once today's cap is used."""
    entry = entry or {}
    if entry.get("day") == day_key(now_ts) and int(entry.get("attempts") or 0) >= HEART_TRIAL_DAILY_ATTEMPTS:
        return next_midnight_ts(now_ts)
    return float(entry.get("retry_at") or 0)


def _trial_entry(entry: dict[str, Any] | None, trial: dict[str, Any], now_ts: float) -> dict[str, Any]:
    entry = entry or {}
    today = day_key(now_ts)
    attempts = (int(entry.get("attempts") or 0) if entry.get("day") == today else 0) + 1
    status = trial.get("status")
    if status == "yielding_to_qyz":  # the battle window, not a fault
        return {**entry, "day": today, "attempts": attempts, "retry_at": now_ts + RETRY_SECONDS}
    if status == "settled":
        return {"day": today, "attempts": attempts, "failures": 0}
    if status == "cooldown" and trial.get("retry_minutes"):
        # A definite server answer: wait for it, not for a failure backoff.
        return {"day": today, "attempts": attempts, "failures": 0, "retry_at": now_ts + trial["retry_minutes"] * 60}
    failures = int(entry.get("failures") or 0) + 1
    delay = HEART_TRIAL_BACKOFF_SECONDS[min(failures, len(HEART_TRIAL_BACKOFF_SECONDS)) - 1]
    return {"day": today, "attempts": attempts, "failures": failures, "retry_at": now_ts + delay,
            "last_status": str(status or "")}


async def run_companion(client: PavilionClient, companion: dict[str, Any], *, account: str, now_ts: float,
                        sleep: Any, trials: dict[str, Any]) -> dict[str, Any]:
    name = companion["name"]
    actions: list[dict[str, Any]] = []
    next_due: list[float] = []

    async def act(command: str) -> dict[str, Any]:
        payload = await client.command(command)
        text = result_text(payload)
        parsed = classify(command, text)
        row = {"command": command, "ok": result_ok(payload), "text": text[:400], **parsed, "at": time.time()}
        actions.append(row)
        await sleep(STEP_GAP_SECONDS)
        return row

    # 1. settle a finished voyage
    if companion["sailing"] and companion["voyage_end_ts"] and companion["voyage_end_ts"] <= now_ts:
        row = await act(".远航归来")
        if row.get("status") == "returned":
            companion = next((c for c in client.companions(now_ts) if c["name"] == name), companion)
        elif row.get("status") == "cooldown" and row.get("retry_minutes"):
            next_due.append(now_ts + row["retry_minutes"] * 60)
    if companion["sailing"] and companion["voyage_end_ts"] > now_ts:
        next_due.append(companion["voyage_end_ts"] + WAKE_GRACE_SECONDS)
        return {"identity": name, "status": "sailing", "route": companion.get("voyage_route"),
                "voyage_end_ts": companion["voyage_end_ts"], "actions": actions, "next_due_at": min(next_due)}

    # 2-4. cooldown-gated steps (only meaningful when not sailing)
    for key, command in (("divination_ready_ts", ".天机代卜"), ("dream_map_ready_ts", ".入梦寻图"),
                         ("zhuimo_ready_ts", ".坠魔心劫")):
        if _due(companion.get(key, 0.0), now_ts):
            row = await act(command)
            if row.get("status") in {"cooldown", "protection_active"} and row.get("retry_minutes"):
                next_due.append(now_ts + row["retry_minutes"] * 60)
        else:
            next_due.append(companion[key] + WAKE_GRACE_SECONDS)
    # 4b. 共历心劫 runs in the group (reply chain), still before the voyage
    trial_ready = max(companion.get("heart_trial_ready_ts", 0.0), _trial_gate(trials.get(name), now_ts))
    if client.heart_trial is not None and _due(trial_ready, now_ts):
        try:
            trial = await run_heart_trial(client.heart_trial, companion_name=name)
        except Exception as exc:  # noqa: BLE001
            code = error_code(exc)
            trial = {"status": "yielding_to_qyz" if code == "yielding_to_qyz" else "unconfirmed", "error": code}
        actions.append({"command": ".共历心劫", "ok": trial.get("status") == "settled", "at": time.time(), **trial})
        trials[name] = _trial_entry(trials.get(name), trial, now_ts)
        if trial.get("status") != "settled":
            next_due.append(trials[name]["retry_at"])
        await sleep(STEP_GAP_SECONDS)
        await client.command(".远航状态")  # refresh companion timestamps after the group action
    elif client.heart_trial is not None:
        next_due.append(trial_ready + WAKE_GRACE_SECONDS)
    refreshed = next((c for c in client.companions(now_ts) if c["name"] == name), companion)
    # 5. assemble a completed chart
    if refreshed.get("puzzle_ready"):
        await act(".拼图")
    # 6. sail again, unless the operator withheld voyages for this companion
    if sailing_disabled(account, name):
        return {"identity": name, "status": "voyage_disabled_by_operator", "affection": refreshed.get("affection"),
                "fragments": refreshed.get("fragments"), "actions": actions,
                "next_due_at": min(next_due) if next_due else now_ts + MAX_IDLE_WAIT_SECONDS}
    route = choose_route(refreshed, ROUTE_OVERRIDES.get(account))
    if not route:
        # 情缘 below every route floor: nothing to sail; wake with the next timer.
        final = refreshed
        return {"identity": name, "status": "affection_too_low_to_sail", "affection": final.get("affection"),
                "fragments": final.get("fragments"), "actions": actions,
                "next_due_at": min(next_due) if next_due else now_ts + MAX_IDLE_WAIT_SECONDS}
    row = await act(f".侍妾远航 {route}")
    pinned = name in (ROUTE_OVERRIDES.get(account) or {})
    if row.get("status") == "affection_too_low" and route != "稳妥" and not pinned:
        route = "稳妥"
        row = await act(f".侍妾远航 {route}")
    if row.get("status") == "affection_too_low":
        next_due.append(now_ts + MAX_IDLE_WAIT_SECONDS)
    if row.get("status") == "sailing":
        hours = row.get("hours") or ROUTES[route][0]
        next_due.append(now_ts + hours * 3600 + WAKE_GRACE_SECONDS)
    elif row.get("status") == "already_sailing":
        next_due.append(now_ts + RETRY_SECONDS)
    else:
        next_due.append(now_ts + RETRY_SECONDS)
    final = next((c for c in client.companions(now_ts) if c["name"] == name), refreshed)
    return {"identity": name, "status": "cycled" if row.get("status") == "sailing" else "cycle_incomplete",
            "route": route, "fragments": final.get("fragments"), "affection": final.get("affection"),
            "actions": actions, "next_due_at": min(next_due) if next_due else now_ts + RETRY_SECONDS}


async def run_account(root: Path, account: str, *, now: float, bridge_factory: Any, transport: Any,
                      force: bool = False, sleep: Any = asyncio.sleep) -> dict[str, Any]:
    prior = read_state(root, account)
    if not force and float(prior.get("next_due_at") or 0) > now:
        return {"ok": True, "account": account, "status": "scheduled_wait", "next_due_at": float(prior["next_due_at"]),
                "identities": prior.get("identities", [])}
    try:
        with account_lock(root, account):
            return await _run_locked(root, account, now=now, bridge_factory=bridge_factory, transport=transport,
                                     sleep=sleep)
    except LockBusy:
        return {"ok": True, "account": account, "status": "lock_busy"}


async def _run_locked(root: Path, account: str, *, now: float, bridge_factory: Any, transport: Any,
                      sleep: Any) -> dict[str, Any]:
    # Per-companion 共历心劫 attempt book, carried across ticks (see _trial_gate).
    trials = dict(read_state(root, account).get("heart_trials") or {})
    result: dict[str, Any] = {"ok": True, "account": account, "username": ACCOUNT_USERNAMES.get(account, ""),
                              "flow": "pavilion_loop", "updated_at": now, "identities": [], "heart_trials": trials}
    uncertain = False
    try:
        async with bridge_factory(root, account) as bridge:
            await verify_username(bridge, account)
            dwelling = await open_dwelling(bridge, transport)
            heart = HeartTrialCommander(bridge.client, state_dir=account_dir(root, account), root=root, sleep=sleep)
            client = PavilionClient(transport, dwelling, heart_trial=heart)
            companions = client.companions(now)
            if not companions:
                raise MiniAppError("no_companions")
            original_active = next((c["name"] for c in companions if c["active"]), companions[0]["name"])
            order = sorted(companions, key=lambda c: (not c["active"], c["name"]))
            for companion in order:
                if not companion["active"]:
                    row = await client.command(f".召回侍妾 {companion['name']}")
                    parsed = classify(".召回侍妾", result_text(row))
                    if parsed.get("status") != "recalled":
                        result["identities"].append({"identity": companion["name"], "status": "recall_failed",
                                                     "text": result_text(row)[:200], "next_due_at": now + RETRY_SECONDS})
                        continue
                    await sleep(STEP_GAP_SECONDS)
                    companion = next((c for c in client.companions(now) if c["name"] == companion["name"]), companion)
                try:
                    result["identities"].append(await run_companion(client, companion, account=account, now_ts=now,
                                                                    sleep=sleep, trials=trials))
                except MiniAppError as exc:
                    uncertain = uncertain or bool(getattr(exc, "uncertain", False))
                    result["identities"].append({"identity": companion["name"], "status": "identity_failed",
                                                 "error": exc.code, "next_due_at": now + RETRY_SECONDS})
            current_active = next((c["name"] for c in client.companions(now) if c["active"]), "")
            if current_active != original_active:
                await client.command(f".召回侍妾 {original_active}")
            result["active_restored"] = original_active
    except MiniAppError as exc:
        result.update(ok=False, status="account_failed", error=exc.code)
        uncertain = bool(getattr(exc, "uncertain", False))
    except Exception as exc:  # noqa: BLE001
        result.update(ok=False, status="account_failed", error=error_code(exc))
    if result.get("status") == "account_failed":
        result["next_due_at"] = now + (UNCERTAIN_RETRY_SECONDS if uncertain else RETRY_SECONDS)
    else:
        dues = [float(i.get("next_due_at") or 0) for i in result["identities"] if i.get("next_due_at")]
        result["status"] = "completed"
        result["next_due_at"] = min(min(dues) if dues else now + RETRY_SECONDS, now + MAX_IDLE_WAIT_SECONDS)
    write_state(root, account, result)
    return result
