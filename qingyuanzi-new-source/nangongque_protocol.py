#!/usr/bin/env python3
"""南宫阙 MiniApp protocol/reducer boundary.

This module is deliberately network-free. It validates scoped entries, builds
single-attempt requests, reduces captured server responses, and produces
redacted shadow-replay evidence. Unknown mechanics remain fail-closed until a
captured fixture defines their exact server contract.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

API_BASE_URL = "https://asc.aiopenai.app"
ENDPOINTS = {
    "start": "/api/miniapp/xianxia-nangongque-boss/start",
    "state": "/api/miniapp/xianxia-nangongque-boss/state",
    "input": "/api/miniapp/xianxia-nangongque-boss/input",
    "claim": "/api/miniapp/xianxia-nangongque-boss/claim",
}
TOKEN_PATTERN = re.compile(r"nqb_[A-Za-z0-9_-]{4,160}\Z")
TERMINAL_ERRORS = {
    "nangongque_not_active",
    "nangongque_join_closed",
    "nangongque_identity_invalid",
    "nangongque_public_entry_required",
    "nangongque_room_missing",
    "invalid_token",
    "invalid_init_data",
    "missing_init_data",
    "cultivator_missing",
}
SETTLED_STATUSES = {"settled", "completed", "finished", "ended"}


class ProtocolError(RuntimeError):
    pass


@dataclass(frozen=True)
class Entry:
    bot_username: str
    token: str = field(repr=False)


@dataclass(frozen=True)
class Request:
    endpoint: str
    path: str
    payload: dict[str, Any] = field(repr=False)
    transport_attempts: int = 1

    def safe_summary(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "path": self.path,
            "payload_keys": sorted(self.payload),
            "input_keys": sorted(self.payload.get("input", {})) if isinstance(self.payload.get("input"), dict) else [],
            "transport_attempts": self.transport_attempts,
        }


@dataclass(frozen=True)
class AccountPlan:
    account: str
    credential_source: str
    state_dir: Path
    deadline_ts: float = 0.0


@dataclass(frozen=True)
class AdmissionBatch:
    entry: Entry = field(repr=False)
    entry_ts: float
    deadline_ts: float
    plans: tuple[AccountPlan, ...]


def load_config(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ProtocolError("config_invalid")
    accounts = payload.get("accounts")
    sources = payload.get("credential_sources")
    if payload.get("enabled") is not False or payload.get("mode") != "shadow":
        raise ProtocolError("config_must_ship_disabled_shadow")
    if not isinstance(accounts, list) or len(accounts) != 6 or len(set(accounts)) != 6:
        raise ProtocolError("account_set_invalid")
    if not isinstance(sources, dict) or set(sources) != set(accounts):
        raise ProtocolError("credential_sources_invalid")
    return payload


def build_account_plans(config: dict[str, Any], *, state_root: Path, deadline_ts: float = 0.0) -> tuple[AccountPlan, ...]:
    accounts = tuple(config.get("accounts") or ())
    sources = config.get("credential_sources") or {}
    plans = tuple(
        AccountPlan(
            account=account,
            credential_source=str(sources[account]),
            state_dir=state_root / "accounts" / account / "nangongque_boss",
            deadline_ts=float(deadline_ts),
        )
        for account in accounts
    )
    if len({plan.account for plan in plans}) != len(plans):
        raise ProtocolError("duplicate_account")
    if len({plan.state_dir for plan in plans}) != len(plans):
        raise ProtocolError("duplicate_state_dir")
    return plans


def coordinator_check(config_path: Path, *, state_root: Path) -> dict[str, Any]:
    config = load_config(config_path)
    plans = build_account_plans(config, state_root=state_root)
    return {
        "status": "disabled_shadow",
        "enabled": False,
        "mode": "shadow",
        "network_enabled": bool(config.get("network_enabled", False)),
        "accounts": [plan.account for plan in plans],
        "credential_sources": {plan.account: plan.credential_source for plan in plans},
    }


def validate_entry(url: str, *, allowed_bot_patterns: tuple[str, ...]) -> Entry:
    parsed = urlsplit(str(url or "").strip())
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in {"t.me", "telegram.me"}:
        raise ProtocolError("entry_origin_invalid")
    bot = parsed.path.strip("/").split("/", 1)[0].lstrip("@").lower()
    if not bot or not any(re.fullmatch(pattern, bot) for pattern in allowed_bot_patterns):
        raise ProtocolError("entry_bot_invalid")
    query = parse_qs(parsed.query)
    token = str((query.get("startapp") or query.get("start_param") or [""])[0]).strip()
    if not TOKEN_PATTERN.fullmatch(token):
        raise ProtocolError("entry_token_invalid")
    return Entry(bot_username=bot, token=token)


def build_admission_batch(
    config: dict[str, Any],
    *,
    entry_url: str,
    entry_ts: float,
    now: float,
    join_window_seconds: float,
    state_root: Path,
) -> AdmissionBatch:
    deadline_ts = float(entry_ts) + float(join_window_seconds)
    if float(now) >= deadline_ts:
        raise ProtocolError("entry_expired")
    patterns = tuple(str(item) for item in config.get("allowed_bot_patterns") or ())
    entry = validate_entry(entry_url, allowed_bot_patterns=patterns)
    plans = build_account_plans(config, state_root=state_root, deadline_ts=deadline_ts)
    return AdmissionBatch(entry=entry, entry_ts=float(entry_ts), deadline_ts=deadline_ts, plans=plans)


def _require_text(value: Any, code: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ProtocolError(code)
    return text


def _require_player(value: Any) -> int:
    try:
        player_id = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ProtocolError("player_id_invalid") from exc
    if player_id <= 0:
        raise ProtocolError("player_id_invalid")
    return player_id


def build_request(
    endpoint: str,
    *,
    init_data: str = "",
    token: str = "",
    player_id: Any = None,
    session_token: str = "",
    room_id: str = "",
    input_payload: dict[str, Any] | None = None,
) -> Request:
    endpoint = str(endpoint or "").strip()
    if endpoint not in ENDPOINTS:
        raise ProtocolError("endpoint_invalid")
    if endpoint == "start":
        clean_token = _require_text(token, "token_missing")
        if not TOKEN_PATTERN.fullmatch(clean_token):
            raise ProtocolError("token_invalid")
        payload: dict[str, Any] = {
            "token": clean_token,
            "initData": _require_text(init_data, "init_data_missing"),
            "playerId": _require_player(player_id),
        }
    else:
        payload = {"sessionToken": _require_text(session_token, "session_token_missing")}
        if endpoint in {"state", "input"}:
            payload.update({
                "roomId": _require_text(room_id, "room_id_missing"),
                "playerId": _require_player(player_id),
            })
        if endpoint == "input":
            if not isinstance(input_payload, dict) or not input_payload:
                raise ProtocolError("input_missing")
            payload["input"] = dict(input_payload)
    return Request(endpoint=endpoint, path=ENDPOINTS[endpoint], payload=payload)


def classify_error(error: Any) -> str:
    raw = str(error or "").strip().lower()
    for code in sorted(TERMINAL_ERRORS):
        if raw == code or code in raw:
            return code
    if "settlement_not_ready" in raw:
        return "settlement_not_ready"
    if "rate_limit" in raw or "429" in raw:
        return "rate_limited"
    return "failed"


def _response_error(response: Any) -> str:
    if not isinstance(response, dict):
        return "invalid_response"
    if response.get("ok") is False:
        return classify_error(response.get("error") or response.get("code") or response.get("message"))
    return ""


def _mapping_id(mapping: Any) -> str:
    if not isinstance(mapping, dict):
        return ""
    return str(mapping.get("id") or mapping.get("roomId") or mapping.get("playerId") or "").strip()


@dataclass
class NangongqueStateMachine:
    account: str
    player_id: int
    phase: str = "new"
    terminal: bool = False
    error: str = ""
    room_id: str = ""
    session_token: str = field(default="", repr=False)
    last_state: dict[str, Any] = field(default_factory=dict, repr=False)
    decision: dict[str, Any] = field(default_factory=dict, repr=False)
    settlement: dict[str, Any] = field(default_factory=dict, repr=False)

    def _fail(self, code: str) -> None:
        self.phase = "failed"
        self.terminal = True
        self.error = classify_error(code)

    def observe_error(self, error: Any) -> None:
        code = classify_error(error)
        self._fail(code)

    def observe_start(self, response: dict[str, Any]) -> None:
        error = _response_error(response)
        if error:
            self._fail(error)
            raise ProtocolError(error)
        session_token = str(response.get("sessionToken") or "").strip()
        room = response.get("room") if isinstance(response.get("room"), dict) else {}
        player = response.get("player") if isinstance(response.get("player"), dict) else {}
        room_id = str(response.get("roomId") or _mapping_id(room)).strip()
        actual_player = response.get("playerId") or _mapping_id(player)
        try:
            actual_player_id = int(actual_player)
        except (TypeError, ValueError, OverflowError):
            actual_player_id = 0
        if not session_token or not room_id:
            self._fail("nangongque_room_missing")
            raise ProtocolError("start_contract_incomplete")
        if actual_player_id != int(self.player_id):
            self._fail("nangongque_identity_invalid")
            raise ProtocolError("identity_mismatch")
        self.session_token = session_token
        self.room_id = room_id
        self.phase = "needs_state"

    def observe_state(self, response: dict[str, Any]) -> None:
        if self.phase not in {"needs_state", "input_uncertain", "needs_decision", "blocked_unknown_mechanic"}:
            raise ProtocolError("state_out_of_order")
        error = _response_error(response)
        if error:
            if error in TERMINAL_ERRORS:
                self._fail(error)
            raise ProtocolError(error)
        room = response.get("room") if isinstance(response.get("room"), dict) else {}
        player = response.get("player") if isinstance(response.get("player"), dict) else {}
        room_id = _mapping_id(room) or str(response.get("roomId") or "").strip()
        actual_player = _mapping_id(player) or str(response.get("playerId") or "").strip()
        if room_id and room_id != self.room_id:
            self._fail("nangongque_room_missing")
            raise ProtocolError("room_mismatch")
        if actual_player and int(actual_player) != int(self.player_id):
            self._fail("nangongque_identity_invalid")
            raise ProtocolError("identity_mismatch")
        self.last_state = dict(response)
        self.decision = {}
        status = str(room.get("status") or response.get("status") or "").strip().lower()
        if status in SETTLED_STATUSES or isinstance(response.get("settlement"), dict):
            self.phase = "needs_claim"
            return
        mechanic = response.get("mechanic")
        if isinstance(mechanic, dict) and mechanic:
            self.phase = "blocked_unknown_mechanic"
            return
        self.phase = "needs_decision"

    def set_decision(self, decision: dict[str, Any]) -> None:
        if self.phase != "needs_decision":
            raise ProtocolError("decision_out_of_order")
        if not isinstance(decision, dict) or not decision:
            raise ProtocolError("decision_invalid")
        self.decision = dict(decision)

    def next_input(self) -> Request:
        if self.phase != "needs_decision" or not self.decision:
            raise ProtocolError("input_not_permitted")
        request = build_request(
            "input",
            session_token=self.session_token,
            room_id=self.room_id,
            player_id=self.player_id,
            input_payload=self.decision,
        )
        self.phase = "input_uncertain"
        self.decision = {}
        return request

    def next_claim(self) -> Request:
        if self.phase != "needs_claim":
            raise ProtocolError("claim_not_permitted")
        request = build_request("claim", session_token=self.session_token)
        self.phase = "claim_uncertain"
        return request

    def observe_claim(self, response: dict[str, Any]) -> None:
        if self.phase != "claim_uncertain":
            raise ProtocolError("claim_out_of_order")
        error = _response_error(response)
        if error:
            if error in TERMINAL_ERRORS:
                self._fail(error)
            elif error == "settlement_not_ready":
                self.phase = "needs_state"
            raise ProtocolError(error)
        if response.get("claimed") is not True:
            self.phase = "needs_state"
            raise ProtocolError("claim_not_confirmed")
        settlement = response.get("settlement")
        self.settlement = dict(settlement) if isinstance(settlement, dict) else {}
        self.phase = "complete"
        self.terminal = True

    def safe_summary(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "player_id": int(self.player_id),
            "phase": self.phase,
            "terminal": self.terminal,
            "error": self.error,
            "has_room": bool(self.room_id),
            "has_session": bool(self.session_token),
            "settlement": self.settlement,
        }


def _atomic_private_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            temp_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise


def replay_fixture(fixture: dict[str, Any], *, state_root: Path) -> dict[str, Any]:
    account = str(fixture.get("account") or "")
    if not re.fullmatch(r"tg\d+", account):
        raise ProtocolError("account_invalid")
    machine = NangongqueStateMachine(account=account, player_id=_require_player(fixture.get("player_id")))
    for event in fixture.get("events") or ():
        if not isinstance(event, dict):
            raise ProtocolError("event_invalid")
        step = str(event.get("step") or "")
        response = event.get("response")
        if step == "start":
            machine.observe_start(response)
        elif step == "state":
            machine.observe_state(response)
        elif step == "decision":
            machine.set_decision(event.get("decision"))
            machine.next_input()
        elif step == "claim":
            machine.next_claim()
            machine.observe_claim(response)
        else:
            raise ProtocolError("event_step_invalid")
    output = state_root / "accounts" / account / "nangongque_boss" / "shadow_replay.json"
    _atomic_private_json(output, machine.safe_summary())
    return {"phase": machine.phase, "output": str(output)}
