#!/usr/bin/env python3
"""Account-scoped Tianji Trial execution state machine."""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from miniapp_shared.state import write_json as _write_json

from .protocol import (
    TianjiTrialError,
    solve_tianji_trial_challenge,
    tianji_trial_progress,
    tianji_trial_start_progress,
    tianji_trial_result_text,
)


@dataclass(frozen=True)
class Identity:
    player_id: int
    label: str
    source: str = "personal"


class TianjiTransport(Protocol):
    async def start(self, player_id: int) -> dict[str, Any]: ...

    async def finish(self, player_id: int, proof: dict[str, Any]) -> dict[str, Any]: ...


def _load_json(path: Path) -> dict[str, Any]:
    # ponytail: not miniapp_shared.state.read_json; a corrupt state file reads as
    # empty here (the server's own progress decides), not as malformed_state.
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


class TianjiTrialRunner:
    def __init__(
        self,
        state_dir: Path,
        transport: TianjiTransport,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.transport = transport
        self.sleep = sleep

    def _identity_path(self, identity: Identity) -> Path:
        return self.state_dir / "identities" / f"{int(identity.player_id)}.json"

    def _record(self, identity: Identity, **updates: Any) -> dict[str, Any]:
        path = self._identity_path(identity)
        state = _load_json(path)
        state.update(
            {
                "player_id": int(identity.player_id),
                "identity": identity.label,
                "source": identity.source,
                "updated_at_ts": time.time(),
                **updates,
            }
        )
        _write_json(path, state)
        return state

    async def run_identity(self, identity: Identity, *, today: str) -> dict[str, Any]:
        state = _load_json(self._identity_path(identity))
        if state and state.get("player_id") != identity.player_id:
            raise TianjiTrialError("trial_state_identity_mismatch")
        if (state.get("last_date") == today and state.get("receipt_confirmed") is True
                and state.get("status") in {"completed", "already"}
                and not state.get("pending_finish")
                and type(state.get("limit")) is int and state["limit"] > 0
                and type(state.get("completed")) is int and state["completed"] == state["limit"]):
            return {"status": "done", "completed": state["completed"], "limit": state["limit"]}

        try:
            payload = await self.transport.start(identity.player_id)
            progress = tianji_trial_start_progress(payload)
        except TianjiTrialError as exc:
            if exc.code != "trial_daily_limit":
                self._record(identity, status="retry", last_error=exc.code, attempt_date=today,
                             receipt_confirmed=False, retry_not_before_ts=time.time() + 120)
                raise
            self._record(identity, status="already", last_date=today, completed=3, limit=3,
                         receipt_confirmed=True, receipt_kind="official_daily_limit", pending_finish=None, last_error="")
            return {"status": "already", "completed": 3, "limit": 3}

        completed, limit = progress if progress is not None else (None, None)
        pending = state.get("pending_finish") or {}
        if progress is not None and completed >= limit:
            self._record(identity, status="already", last_date=today, completed=completed, limit=limit,
                         receipt_confirmed=True, receipt_kind="official_progress", pending_finish=None, last_error="")
            return {"status": "already", "completed": completed, "limit": limit}

        challenge = payload.get("challenge")
        if not isinstance(challenge, dict):
            self._record(identity, status="retry", last_error="trial_challenge_missing")
            raise TianjiTrialError("trial_challenge_missing", uncertain=True)
        if pending and pending.get("date") == today:
            before = pending.get("completed_before")
            if progress is not None and type(before) is int and completed > before:
                self._record(identity, pending_finish=None, completed=completed, limit=limit,
                             receipt_kind="official_progress_recovery", last_error="")
            elif (challenge.get("challengeId") == pending.get("challenge_id")
                  and time.time() < float(state.get("retry_not_before_ts") or 0)):
                return {"status": "retry", "completed": completed, "limit": limit,
                        "error": "finish_reply_pending", "retry_not_before_ts": state["retry_not_before_ts"]}
            elif challenge.get("challengeId") != pending.get("challenge_id"):
                # The server supplied a new active challenge. Preserve the old
                # unknown attempt; it must not be counted as a settlement.
                history = list(state.get("unconfirmed_history", []))
                history.append({**pending, "superseded_by": challenge.get("challengeId")})
                self._record(identity, unconfirmed_history=history[-20:], pending_finish=None)

        results: list[dict[str, Any]] = []
        # Without start progress, the daily three-stage rule is a work bound,
        # never an invented completed/limit value. The first finish establishes
        # progress; every following stage must advance that official count.
        for _ in range(limit - completed if progress is not None else 3):
            try:
                proof = solve_tianji_trial_challenge(challenge)
            except TianjiTrialError as exc:
                self._record(identity, status="retry", last_error=exc.code, attempt_date=today,
                             receipt_confirmed=False, retry_not_before_ts=time.time() + 120)
                raise
            pending = {"challenge_id": proof["challengeId"], "completed_before": completed,
                       "limit": limit, "date": today, "submitted_at_ts": time.time()}
            self._record(identity, status="finish_pending", pending_finish=pending,
                         attempt_date=today, start_progress_confirmed=progress is not None,
                         retry_not_before_ts=time.time() + 120, receipt_confirmed=False)
            await self.sleep((int(proof.get("durationMs") or 0) + 500) / 1000)
            try:
                settled = await self.transport.finish(identity.player_id, proof)
                new_completed, new_limit = tianji_trial_progress(settled)
                result = settled.get("result")
                returned_id = result.get("challengeId") if isinstance(result, dict) else None
                if ((limit is not None and new_limit != limit)
                        or new_completed <= 0
                        or (completed is not None and new_completed != completed + 1)
                        or not isinstance(result, dict) or not result
                        or (returned_id and returned_id != proof["challengeId"])):
                    raise TianjiTrialError("trial_settlement_unconfirmed", uncertain=True)
            except Exception as exc:
                code = exc.code if isinstance(exc, TianjiTrialError) else type(exc).__name__
                self._record(identity, status="retry", last_error=code, pending_finish=pending,
                             retry_not_before_ts=time.time() + 120, receipt_confirmed=False)
                return {"status": "retry", "completed": completed, "limit": limit, "error": code}
            results.append(settled)
            completed, limit = new_completed, new_limit
            self._record(identity, status="partial" if completed < limit else "completed",
                         completed=completed, limit=limit, pending_finish=None, receipt_confirmed=True,
                         receipt_kind="official_finish", receipt_challenge_id=proof["challengeId"], last_error="")
            challenge = settled.get("nextChallenge")
            if completed >= limit or not isinstance(challenge, dict):
                break

        if completed < limit:
            self._record(identity, status="retry", completed=completed, limit=limit, last_error="next_challenge_missing")
            return {"status": "retry", "completed": completed, "limit": limit}
        summary = tianji_trial_result_text(results)
        self._record(identity, status="completed", last_date=today, completed=completed, limit=limit,
                     summary=summary, receipt_confirmed=True, pending_finish=None, last_error="")
        return {"status": "completed", "completed": completed, "limit": limit, "summary": summary}
