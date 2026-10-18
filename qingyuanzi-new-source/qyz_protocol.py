"""Account-bound adapter executing the three user-supplied QYZ reference scripts."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import math
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class ProtocolError(RuntimeError):
    def __init__(self, code: str, *, phase: str = "", uncertain: bool = False, http_status: int = 0):
        self.code = code if re.fullmatch(r"[a-z0-9_]{1,80}", code) else "protocol_error"
        self.phase, self.uncertain, self.http_status = phase, uncertain, http_status
        self.details: dict[str, Any] = {}
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class WebViewAuth:
    account: str
    init_data: str = field(repr=False)
    entry_token: str = field(repr=False)
    entry_fingerprint: str


def number(value: Any, *, name: str, minimum: float = 0, maximum: float = 600000) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProtocolError("invalid_" + name)
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ProtocolError("invalid_" + name)
    return result


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# The host journals these phases synchronously (they decide whether a restart
# may re-dispatch the entry), so they are emitted outside the timed request.
JOURNALED_BEFORE_CLOCK = frozenset({"start", "begin"})


# Server overload replies the reference _request retries (RETRY_HTTP_STATUSES,
# TRANSIENT_WORLD_BOSS_ERRORS) and _post_until_reply does not.
OVERLOAD_RETRY_ENDPOINTS = frozenset({"start", "begin", "finish"})
OVERLOAD_CODES = frozenset({"rate_limited", "server_busy", "api_timeout"})


def server_overloaded(exc: BaseException) -> bool:
    from qyz_reference.compat import MiniAppBeastError
    from qyz_reference.world_boss_features import RETRY_HTTP_STATUSES
    if (not isinstance(exc, MiniAppBeastError) or exc.code in {"api_unreachable", "response_invalid"}
            or (exc.code == "server_error" and exc.uncertain)):
        return False  # no server reply, or _post_until_reply already retried it to its deadline
    return exc.status in RETRY_HTTP_STATUSES or exc.code in OVERLOAD_CODES


def _join_attempts(trace: dict[str, Any], earlier: list) -> None:
    if earlier:
        trace["attempts"] = earlier + list(trace.get("attempts", []))
        for index, row in enumerate(trace["attempts"], 1):
            if isinstance(row, dict):
                row["attempt"] = index


def window_count(value: Any) -> int:
    count = number(value, name="window_count", minimum=1, maximum=128)
    if not count.is_integer():
        raise ProtocolError("invalid_window_count", phase="window")
    return int(count)


class ProtocolRuntime:
    """Keep XIUXIAN identity/transport ownership around the reference lifecycle.

    The reference owns admission, CF renewal, the battle clock, streamed windows,
    charge tickets, hit timing feedback, and finish. This boundary records only
    actual server receipts, so a local perfect never becomes a server perfect.
    """
    def __init__(self, *, account: str, username: str, transport: Any,
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[..., Any] = asyncio.sleep,
                 trace: Callable[[dict[str, Any]], None] | None = None,
                 broker=None, actor=None, entry=None, state_root: Path | None = None):
        from qyz_verification import MemoryTurnstileBroker
        from qyz_combat import CombatState
        from qyz_timing import AdmissionClock
        from qyz_humanize import Humanization
        self.account, self.username, self.api = account, username, transport
        self.now, self.sleep = monotonic, sleep
        self.trace = trace or (lambda _record: None)
        self.broker = broker or MemoryTurnstileBroker()
        self.actor, self.entry, self.state_root = actor, entry, state_root
        self.phase = "preflight"
        self.receipts: list[dict[str, Any]] = []
        self.expected = 0
        self.finish_result: dict[str, Any] | None = None
        self._used = False
        self._begun = False
        self._monitor = None
        self.account_conflict: dict[str, Any] | None = None
        self.combat = CombatState(self.now, self.emit)
        self.window_states: dict[str, dict[str, Any]] = {}
        self.admission_clock = AdmissionClock(self.now)
        self._read_pool_task: asyncio.Task | None = None
        self._read_rewarm: asyncio.Task | None = None
        self._lead_evidence: dict[str, Any] | None = None
        self.humanization = Humanization()

    def _start_read_pool_warm(self, auth):
        """Open the read sockets in the background, once per battle."""
        warm = getattr(self.api, "warm_read_pool", None)
        if callable(warm) and self._read_pool_task is None:
            self._read_pool_task = asyncio.ensure_future(self._open_read_pool(warm, auth))

    async def _open_read_pool(self, warm, auth):
        try:
            opened = await warm(auth)
        except Exception:  # noqa: BLE001 - warm-up never blocks the battle
            opened = 0
        self.emit("read_pool_warmed", connections=int(opened or 0))

    async def _warm_read_pool_once(self, auth):
        """Open the read sockets, or join the attempt started before /begin.

        Waits at most ``READ_POOL_WARM_WAIT_SECONDS``; the warm-up keeps
        running if that bound passes.
        """
        from qyz_timing import READ_POOL_WARM_WAIT_SECONDS
        self._start_read_pool_warm(auth)
        task = self._read_pool_task
        if task is None or task.done():
            return
        bound = asyncio.ensure_future(self.sleep(READ_POOL_WARM_WAIT_SECONDS))
        try:
            await asyncio.wait((task, bound), return_when=asyncio.FIRST_COMPLETED)
        finally:
            bound.cancel()
            await asyncio.gather(bound, return_exceptions=True)

    async def _keep_read_pool_warm(self, auth):
        """Ping ``/state`` on the read pool while the browser solves Turnstile.

        The wait is 100-150 s; the origin closes idle sockets long before that,
        so the first window poll otherwise lands on a cold TLS handshake.
        """
        from qyz_timing import READ_POOL_KEEPALIVE_MAX_SECONDS, READ_POOL_KEEPALIVE_SECONDS
        warm = getattr(self.api, "warm_read_pool", None)
        if not callable(warm) or not auth.get("token"):
            return
        started = self.now()
        while self.now() - started < READ_POOL_KEEPALIVE_MAX_SECONDS:
            interval = self.humanization.interval(READ_POOL_KEEPALIVE_SECONDS)
            if self.now() - started + interval >= READ_POOL_KEEPALIVE_MAX_SECONDS:
                return
            await self.sleep(interval)
            try:
                await warm(auth)
            except Exception:  # noqa: BLE001
                return

    def _read_socket_warm(self):
        warm = getattr(self.api, "read_socket_warm", None)
        return callable(warm) and warm() is True

    def _rewarm_read_pool(self, auth):
        """Re-open failed read sockets off the poll path; polls keep the warm one."""
        cold = getattr(self.api, "cold_read_sockets", None)
        if not callable(cold) or cold() == 0 or not auth.get("token") or not auth.get("initData"):
            return
        if self._read_rewarm is not None and not self._read_rewarm.done():
            return
        self._read_rewarm = asyncio.ensure_future(self._rewarm_cold_read_sockets(auth))

    async def _rewarm_cold_read_sockets(self, auth):
        # No trace records here: each one is a host journal write mid-battle.
        # The /state requests themselves land in the transport diagnostics.
        from qyz_timing import READ_SOCKET_REWARM_ATTEMPTS, READ_SOCKET_REWARM_TIMEOUT_SECONDS
        for _attempt in range(READ_SOCKET_REWARM_ATTEMPTS):
            try:
                await self.api.warm_read_pool(auth, timeout=READ_SOCKET_REWARM_TIMEOUT_SECONDS, cold_only=True)
            except Exception:  # noqa: BLE001 - re-warming is best effort
                pass
            if self.api.cold_read_sockets() == 0:
                return
            if _attempt + 1 < READ_SOCKET_REWARM_ATTEMPTS:
                await self.sleep(self.humanization.retry_delay(_attempt, base=.25, cap=.5))

    def _capped_request_lead(self, request_lead_ms):
        from qyz_timing import effective_request_lead
        lead, evidence = effective_request_lead(int(request_lead_ms or 0),
                                                getattr(self.api, "request_diagnostics", None))
        if self._lead_evidence != evidence:
            self._lead_evidence = evidence
            self.emit("request_lead_reviewed", **evidence)
        return lead

    def _window_state(self, window_id, phase, **fields):
        """Snapshot one window's observed lifecycle without authentication data.

        Target times are plans. Charge confirmation and accepted hits are
        separate phases, so a timer or a release never becomes a success.
        """
        window = self.combat.windows.get(window_id)
        if window is None:
            return None
        row = self.window_states.setdefault(window_id, {
            "window_fingerprint": digest(window_id), "center_ms": window["centerMs"],
            "hit_ms": window["hitMs"], "perfect_ms": window["perfectMs"],
            "server_perfect": None, "transitions": [],
        })
        if phase in {"waiting_charge", "charging", "charge_unconfirmed"} and row.get("phase") in {
            "confirmed_perfect", "confirmed_hit", "rejected", "released_unconfirmed", "unconfirmed", "stopped", "not_sent"
        }:
            # A late scheduling observation cannot erase a business rejection
            # or an accepted hit that already arrived for this window.
            return copy.deepcopy(row)
        elapsed = self._window_elapsed()
        update = {"phase": phase, **fields}
        changed = any(row.get(k) != v for k, v in update.items())
        row.update(update, observed_elapsed_ms=elapsed, observed_at=time.time())
        target, charged, released = (row.get(key) for key in ("release_target_ms", "charge_confirmed_ms", "release_ms"))
        if all(type(value) in (int, float) for value in (target, charged, released)):
            # The timer cannot release until the charge ticket has arrived.
            # Keep this network wait separate from delay after both are ready.
            row["charge_reply_block_ms"] = max(0, charged - target)
            row["release_after_ready_ms"] = max(0, released - max(target, charged))
        if changed:
            row["transitions"].append({"phase": phase, "elapsed_ms": elapsed,
                                       **{k: row[k] for k in ("charge_target_ms", "release_target_ms") if k in row}})
            del row["transitions"][:-32]
        return copy.deepcopy(row)

    def _observe_window(self, window_id, phase, **fields):
        snapshot = self._window_state(window_id, phase, **fields)
        if snapshot is not None:
            self.emit("window_progress", window_state=snapshot)

    def _window_elapsed(self):
        if self.combat.started_at is None:
            return None
        return int(round((self.now() - self.combat.started_at) * 1000))

    async def _post_until_reply(self, endpoint, body, timeout):
        """Retry the same request while its server action window is usable.

        Proofs, tickets and CF values stay in this authenticated in-memory
        scope. A retry never fabricates a hit receipt or a new charge proof.
        """
        deadline = self.now() + (600 if endpoint == "finish" else 60)
        window = self.combat.windows.get(body.get("windowId"))
        if window and self.combat.started_at is not None:
            deadline = self.combat.started_at + (window["centerMs"] + window["hitMs"] + 80) / 1000
        attempt = 0
        while True:
            attempt += 1
            try:
                return await self.api.post_json(endpoint, body, timeout=timeout)
            except ProtocolError as exc:
                if endpoint == "window":
                    # The reveal loop owns safe reads, pacing and its deadline.
                    # A generic 60-second retry loop can lose several windows.
                    raise
                retryable = exc.code in {"transport_outcome_unknown", "read_transport_failed", "response_invalid"} or (
                    exc.code == "server_error" and exc.uncertain)
                if not retryable or self.now() >= deadline:
                    raise
                # Keep the existing minimum spacing: pure Full Jitter halves
                # the mean backoff and doubles requests during a long outage.
                # The random component is additional to this rate-limit floor.
                floor = min(5.0, .25 * 2 ** min(attempt - 1, 5))
                delay = floor + self.humanization.retry_delay(attempt - 1)
                if self.now() + delay >= deadline:
                    raise
                window = self._window_state(body.get("windowId"),
                    "awaiting_hit_reply" if endpoint == "hit" else "charge_pending",
                    retry_attempt=attempt, retry_delay_seconds=delay)
                self.emit(endpoint + "_reply_retry_pending", attempt=attempt, retry_delay_seconds=delay,
                          **({"window_state": window} if window is not None else {}))
                await self.sleep(delay)

    def emit(self, phase, **metadata):
        self.phase = phase
        self.trace({"account": self.account, "phase": phase, **metadata})

    def _conflict_error(self):
        from qyz_reference.compat import MiniAppBeastError
        error = MiniAppBeastError("boss_action_limit", 409)
        # The rejected request is known, but any manual session's battle and
        # settlement are not ours to finish. Keep this event pending review.
        error.uncertain = True
        error.details = {"account_conflict": dict(self.account_conflict or {}),
                         "expected_windows": self.expected, "accepted_hits": len(self.receipts),
                         "server_perfects": sum(row["server_perfect"] for row in self.receipts),
                         "receipts": list(self.receipts)}
        return error

    async def _reconcile_conflict(self, endpoint, body):
        if self.account_conflict is not None:
            return
        self.account_conflict = {"endpoint": endpoint, "readback_status": "pending"}
        # Set and journal the account gate *before* awaiting I/O, so other
        # windows cannot submit while the read-only reconciliation is pending.
        self.emit("account_action_conflict", account_conflict=dict(self.account_conflict))
        try:
            data = await self.api.post_json("state", {key: body[key] for key in ("token", "initData")}, timeout=3)
            if not isinstance(data, dict) or data.get("ok") is not True:
                raise ProtocolError("state_unconfirmed", phase="state")
            boss = data.get("boss") if isinstance(data.get("boss"), dict) else {}
            snapshot = {}
            if boss.get("roomStatus") in {"joining", "join", "waiting", "battle", "closed", "finished", "defeated"}:
                snapshot["roomStatus"] = boss["roomStatus"]
            for key in ("actionLimit", "actionsRemaining", "bossHp"):
                value = boss.get(key)
                if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                    snapshot[key] = value
            self.account_conflict.update(readback_status="confirmed", boss=snapshot)
        except Exception:
            # Read failure must not reopen the mutation gate, or echo request
            # bodies/URLs through a transport exception.
            self.account_conflict["readback_status"] = "failed"
        self.emit("account_action_conflict", account_conflict=dict(self.account_conflict))

    async def _send(self, origin, path, body, timeout):
        from qyz_http import ORIGIN, API_PREFIX, READ_ENDPOINTS
        from qyz_reference.compat import MiniAppBeastError
        if origin != ORIGIN or not path.startswith(API_PREFIX):
            raise MiniAppBeastError("endpoint_or_payload_rejected")
        endpoint = path[len(API_PREFIX):]
        if self.account_conflict is not None and endpoint not in READ_ENDPOINTS:
            raise self._conflict_error()
        if endpoint in {"charge-start", "hit"}:
            self.combat.advance()
            if self.combat.dead:
                raise MiniAppBeastError("local_player_dead")
        if endpoint == "hit":
            if not self.combat.release(body["windowId"], {
                "t": body["elapsedMs"], "holdMs": body["holdMs"], "stance": "强攻",
            }):
                raise MiniAppBeastError("local_player_dead")
        elif endpoint == "finish":
            body = {**body, "bossProof": self.combat.finish_proof(body["bossProof"])}
        window_id = body.get("windowId")
        window = None
        if endpoint == "charge-start":
            window = self._window_state(window_id, "charge_pending", charge_sent_ms=self._window_elapsed())
        elif endpoint == "hit":
            window = self._window_state(window_id, "awaiting_hit_reply", release_ms=self._window_elapsed(),
                                        local_hold_ms=body["holdMs"], reported_elapsed_ms=body["elapsedMs"])
        if endpoint not in JOURNALED_BEFORE_CLOCK:
            self.emit(endpoint + "_submitted", **({"window_state": window} if window is not None else {}))
        try:
            data = await self._post_until_reply(endpoint, body, timeout)
        except ProtocolError as exc:
            self._observe_window(window_id,
                ("released_unconfirmed" if endpoint == "hit" else "charge_unconfirmed") if exc.uncertain else "rejected",
                error=exc.code, reply_ms=self._window_elapsed())
            if exc.code == "boss_action_limit":
                await self._reconcile_conflict(endpoint, body)
                raise self._conflict_error() from None
            code = {"transport_outcome_unknown": "api_unreachable",
                    "read_transport_failed": "api_unreachable"}.get(exc.code, exc.code)
            error = MiniAppBeastError(code, exc.http_status)
            error.details, error.uncertain = exc.details, exc.uncertain
            raise error from None
        if not isinstance(data, dict) or data.get("ok") is not True:
            self._observe_window(window_id, "unconfirmed", error="server_response_unconfirmed")
            raise MiniAppBeastError("server_response_unconfirmed")
        if endpoint == "start" and isinstance(data.get("challenge"), dict):
            self.expected = window_count(data["challenge"].get("windowCount"))
        elif endpoint == "window" and data.get("windowCount") is not None:
            self.expected = window_count(data["windowCount"])
        elif endpoint == "charge-start":
            if not isinstance(data.get("chargeTicket"), str) or not data["chargeTicket"]:
                self._observe_window(window_id, "charge_unconfirmed", error="charge_ticket_unconfirmed")
                raise MiniAppBeastError("charge_ticket_unconfirmed")
            window = self._window_state(window_id, "charging", charge_confirmed_ms=self._window_elapsed())
        elif endpoint == "hit":
            hit = data.get("hit")
            if (not isinstance(hit, dict) or hit.get("accepted") is False or "damageYi" not in hit
                    or ("windowId" in hit and hit["windowId"] != body["windowId"])):
                self._observe_window(window_id, "released_unconfirmed", error="hit_unconfirmed")
                raise MiniAppBeastError("hit_unconfirmed")
            fingerprint = digest(body["windowId"])
            if any(row["window_fingerprint"] == fingerprint for row in self.receipts):
                raise MiniAppBeastError("hit_duplicate_receipt")
            receipt = {"index": len(self.receipts) + 1, "window_fingerprint": fingerprint,
                       "window_index": self.window_states.get(window_id, {}).get("index"),
                       "accepted": True, "elapsed_ms": body["elapsedMs"], "hold_ms": body["holdMs"],
                       "server_perfect": hit.get("perfect") is True,
                       "perfect_evidence_present": isinstance(hit.get("perfect"), bool),
                       "damage_yi": number(hit["damageYi"], name="damage", maximum=1e30)}
            for server_key, key in (("holdMs", "server_hold_ms"), ("deltaMs", "server_delta_ms")):
                value = hit.get(server_key)
                if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 600000:
                    receipt[key] = value
            self.receipts.append(receipt)
            window = self._window_state(window_id, "confirmed_perfect" if receipt["server_perfect"] else "confirmed_hit",
                reply_ms=self._window_elapsed(), server_perfect=receipt["server_perfect"],
                perfect_evidence_present=receipt["perfect_evidence_present"], damage_yi=receipt["damage_yi"],
                **{key: receipt[key] for key in ("server_hold_ms", "server_delta_ms") if key in receipt})
            self.emit("hit_accepted", expected_windows=self.expected, receipt=receipt,
                      **({"window_state": window} if window is not None else {}))
        elif endpoint == "finish":
            result = data.get("result")
            if not isinstance(result, dict) or not isinstance(result.get("grade"), str) or not result["grade"]:
                raise MiniAppBeastError("finish_unconfirmed")
            self.finish_result = {"grade": result["grade"][:80],
                                  "score": number(result.get("score"), name="score", maximum=1e30)}
            # The reference settles without player_hp too (it falls back to the
            # local value); only a reported value can say the server saw a death.
            if result.get("player_hp") is not None:
                self.finish_result["player_hp"] = number(result["player_hp"], name="player_hp", maximum=1e6)
            from qyz_reference.world_boss_features import _diagnostic_value
            for key in ("hits", "perfects", "realtime_damage_yi", "realtime_hit_count", "sample_count",
                        "combo", "damage_taken", "dead", "automation_review", "automation_risk",
                        "quality_multiplier", "duration_ms", "event_id", "phase", "phase_label", "action"):
                if key in result:
                    self.finish_result[key] = _diagnostic_value(result[key])
        self.emit(endpoint + "_confirmed", expected_windows=self.expected,
                  **({"window_state": window} if window is not None else {}))
        if endpoint == "begin":
            self._begun = True
            if body.get("turnstileToken"):
                self.emit("verification_accepted", method="reference_native_browser")
            self.emit("battle_started", expected_windows=self.expected)
        return data

    async def run(self, auth: WebViewAuth) -> dict[str, Any]:
        from qyz_http import ORIGIN, READ_ENDPOINTS
        from qyz_reference.compat import MiniAppBeastError
        from qyz_reference.world_boss_features import WorldBossEntry, WorldBossMonitor
        from qyz_timing import VpsTimingMixin
        if self._used:
            raise ProtocolError("runtime_already_consumed", phase="preflight")
        self._used = True
        if auth.account != self.account or getattr(self.api, "account", None) != self.account:
            raise ProtocolError("account_binding_mismatch", phase="preflight")
        if not auth.init_data or not auth.entry_token:
            raise ProtocolError("authentication_missing", phase="preflight")
        entry = WorldBossEntry(
            message_id=getattr(self.entry, "message_id", 0), chat_id=getattr(self.entry, "chat_id", None),
            bot_username=getattr(self.entry, "bot_username", ""), origin=ORIGIN,
            fingerprint=auth.entry_fingerprint, token=auth.entry_token,
        )
        actor = self.actor or SimpleNamespace(client=None, config={}, state={},
                                              my_info=SimpleNamespace(username=self.username))
        state_root = self.state_root
        combat = self.combat
        runtime = self
        class BoundMonitor(VpsTimingMixin, WorldBossMonitor):
            async def _wait_for_window_poll(self, requested_at):
                await self.sleep(max(0.0, requested_at + runtime.humanization.window_poll() - self.monotonic()))

            def _window_progress(self, window, window_index, battle_start, phase, **fields):
                fields = super()._window_progress(window, window_index, battle_start, phase, **fields)
                runtime._observe_window(window["id"], phase, index=window_index,
                    **{k: v for k, v in fields.items() if k != "observed_elapsed_ms"})

            async def _request(self, origin, path, payload, *, retries=0, **options):
                # _post_until_reply owns transport retries of the exact same
                # request inside its action deadline; do not multiply them.
                # The reference count is kept only for server overload replies
                # on /start, /finish and the token-free /begin probe (it passes
                # 0 for a Turnstile /begin: that token is one-shot).
                endpoint = path.rsplit("/", 1)[-1]
                overload_retries = retries if endpoint in OVERLOAD_RETRY_ENDPOINTS else 0
                if endpoint not in READ_ENDPOINTS or endpoint == "window":
                    retries = 0
                if endpoint == "window":
                    from qyz_timing import WINDOW_READ_COLD_TIMEOUT_SECONDS, WINDOW_READ_TIMEOUT_SECONDS
                    options["timeout"] = min(options.get("timeout") or self.timeout,
                                             WINDOW_READ_TIMEOUT_SECONDS)
                    if (getattr(runtime.api, "read_pool_cold_failures", 0) > 0
                            and not runtime._read_socket_warm()):
                        # The previous poll died inside its TLS handshake and no
                        # warm read socket is left. One second cannot complete a
                        # cold socket on this route, so allow one wider read
                        # instead of burning the error budget.
                        options["timeout"] = WINDOW_READ_COLD_TIMEOUT_SECONDS
                if endpoint == "start" and not isinstance(options.get("trace"), dict):
                    options["trace"] = {}
                if endpoint in JOURNALED_BEFORE_CLOCK:
                    # The host blocks this thread until the phase is on disk.
                    # Emit it before the reference starts the attempt clock, or
                    # that write lands in the /start sample and the /begin RTT
                    # that place battle_start.
                    runtime.emit(endpoint + "_submitted")
                trace = options["trace"] if isinstance(options.get("trace"), dict) else {}
                earlier: list = []
                for attempt in range(overload_retries + 1):
                    try:
                        result = await super()._request(origin, path, payload, retries=retries, **options)
                        break
                    except Exception as exc:
                        if endpoint == "window":
                            runtime._rewarm_read_pool({"token": payload.get("token"),
                                                       "initData": payload.get("initData")})
                        if attempt >= overload_retries or not server_overloaded(exc):
                            _join_attempts(trace, earlier)
                            raise
                        # Each reference call resets trace["attempts"]; keep them all.
                        earlier += trace.get("attempts", [])
                        await self.sleep(min(2.5, .45 * (attempt + 1)) + runtime.humanization.retry_delay(attempt))
                _join_attempts(trace, earlier)
                if endpoint == "start":
                    runtime.admission_clock.observe_start(options["trace"])
                return result

            async def _begin_with_turnstile(self, *args, **options):
                if not isinstance(options.get("trace"), dict):
                    options["trace"] = {}
                # args: entry, identity, init_data, session_token, challenge_id
                auth = {"token": args[3], "initData": args[2]} if len(args) >= 4 else {}
                # Open the read sockets before /begin. Opened after it, their
                # two handshakes (1.5-1.8 s on 2026-09-27) pushed window 1's
                # reveal past its charge point on four of six accounts; opened
                # during it, they would share the network with the probe whose
                # round trip sets the battle clock. The battle starts whenever
                # /begin is sent, so the short wait costs nothing.
                if auth:
                    await runtime._warm_read_pool_once(auth)
                keeper = asyncio.create_task(runtime._keep_read_pool_warm(auth))
                try:
                    result = await super()._begin_with_turnstile(*args, **options)
                finally:
                    keeper.cancel()
                    try:
                        await keeper
                    except (asyncio.CancelledError, Exception):
                        pass
                runtime.admission_clock.refine_begin(options["trace"])
                return result

            async def _fight(self, entry, init_data, session_token, payload, identity="主魂"):
                combat.configure(payload)
                return await super()._fight(entry, init_data, session_token, payload, identity)

            async def _reveal_windows(self, entry, init_data, session_token, challenge_id, battle_start,
                                      queue, reveal_log, expected_count):
                # The first poll fires immediately after /begin. Wait for any
                # read socket still opening from before /begin (or open them
                # now), outside the reveal loop's 1-second budget.
                await runtime._warm_read_pool_once({"token": session_token, "initData": init_data})
                return await super()._reveal_windows(entry, init_data, session_token, challenge_id,
                                                     battle_start, queue, reveal_log, expected_count)

            def _boss_stop_requested(self):
                combat.advance()
                # Player death belongs to this account. It must never write a
                # shared boss-defeat marker or stop other accounts' battles.
                # It also does not stop the clock: ``_send`` refuses further
                # charges and strikes, but /finish still waits for the real
                # window schedule and minimum duration. On 2026-09-22
                # @demo_player_04 died locally at 69 s, finished at once with a
                # 75 000 ms proof and got ``boss_client_clock_mismatch``: five
                # server-confirmed hits settled nothing.
                return runtime.account_conflict is not None or super()._boss_stop_requested()

            def _boss_stopped_error(self):
                if runtime.account_conflict is not None:
                    return runtime._conflict_error()
                return super()._boss_stopped_error()

            def _skipped_hit_result(self, window, window_index=0, *, reason="boss_defeated_local"):
                if runtime.account_conflict is not None:
                    reason = "account_action_conflict"
                return super()._skipped_hit_result(window, window_index, reason=reason)

            async def _hit_window(self, entry, init_data, session_token, challenge_id,
                                  battle_start, window, window_index=0, request_lead_ms=0,
                                  charge_required=True):
                combat.started_at = battle_start
                combat.register(window)
                request_lead_ms = runtime._capped_request_lead(request_lead_ms)
                result = await super()._hit_window(entry, init_data, session_token, challenge_id,
                                                   battle_start, window, window_index,
                                                   request_lead_ms, charge_required)
                current = runtime.window_states.get(window["id"], {})
                if not result.get("ok") and current.get("phase") not in {
                    "rejected", "released_unconfirmed", "charge_unconfirmed", "unconfirmed"
                }:
                    runtime._observe_window(window["id"], "stopped" if result.get("skipped") else "not_sent",
                                            index=window_index, error=result.get("error", "window_not_sent"))
                action = result.get("action")
                if action and not combat.release(window["id"], action):
                    return self._skipped_hit_result(window, window_index, reason="local_player_dead")
                return result

            def _boss_marker_path(self, current):
                if state_root is None:
                    return None
                return state_root / "battle-events" / (digest(current.fingerprint) + ".json")
        bound_broker = self.broker.for_account(self.account) if hasattr(self.broker, "for_account") else self.broker
        monitor = BoundMonitor(actor, self.account, post_json=self._send, turnstile_broker=bound_broker,
                               monotonic=self.now, sleep=self.sleep)
        self._monitor = monitor
        try:
            outcome = await monitor._participate(entry, identity="主魂", init_data=auth.init_data)
            if self.finish_result is None:
                raise ProtocolError("finish_unconfirmed", phase="finish")
            local_accepted = len(self.receipts)
            local_perfects = sum(row["server_perfect"] for row in self.receipts)

            def authoritative_count(name: str, fallback: int, maximum: int) -> int:
                raw = outcome.get(name)
                if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                    return fallback
                value = float(raw)
                if not math.isfinite(value) or not value.is_integer() or not 0 <= value <= maximum:
                    return fallback
                return int(value)

            accepted = authoritative_count("hit_count", local_accepted, self.expected)
            perfects = authoritative_count("perfect_count", local_perfects, accepted)
            missed = max(0, self.expected - accepted)
            server_dead = self.finish_result.get("player_hp", 1) <= 0
            perfect = self.expected > 0 and self.expected == accepted == perfects and not server_dead
            result = {"account": self.account, "entry_fingerprint": auth.entry_fingerprint,
                      "status": "settled_perfect" if perfect else "settled_partial",
                      "implementation": "telegram_reference_scripts", "expected_windows": self.expected,
                      "accepted_hits": accepted, "server_perfects": perfects, "missed_windows": missed,
                      "local_player_hp": self.combat.player_hp, "local_dead": self.combat.dead,
                      "local_missed_windows": len(self.combat.missed), "server_dead": server_dead,
                      "finish_confirmed": True, "server_result": self.finish_result,
                      "receipts": list(self.receipts), "reference_outcome": outcome,
                      "window_states": sorted((copy.deepcopy(v) for v in self.window_states.values()),
                                               key=lambda v: v.get("index", 0)),
                      "transport_diagnostics": list(getattr(self.api, "request_diagnostics", []))}
            from qyz_state import server_damage_multiplier_min
            multiplier = server_damage_multiplier_min(result)
            result["server_damage_multiplier_min"] = multiplier
            result["verification_requests"] = [
                {key: row[key] for key in ("account", "status", "created_at", "submitted_at", "consumed_at",
                                          "result_at", "browser_events", "result_http_status") if key in row}
                for row in bound_broker.list_requests(include_finished=True)
                if row.get("event_fingerprint") == auth.entry_fingerprint
            ]
            if multiplier is not None and multiplier < 1:
                result["status"] = "settled_damage_reduced"
            self.emit(result["status"], expected_windows=self.expected, accepted_hits=accepted,
                      server_perfects=perfects, missed_windows=missed,
                      local_player_hp=self.combat.player_hp, local_dead=self.combat.dead,
                      local_missed_windows=len(self.combat.missed), server_damage_multiplier_min=multiplier)
            return result
        except (MiniAppBeastError, ProtocolError) as exc:
            # A ProtocolError here is a strict check of a reply the server
            # already accepted (window counts, damage, finish fields): after
            # /begin its outcome is unknown, never a clean pre-battle error.
            strict = isinstance(exc, ProtocolError)
            error = ProtocolError(exc.code, phase=getattr(exc, "phase", "") or self.phase,
                                  uncertain=exc.uncertain or (strict and self._begun),
                                  http_status=exc.http_status if strict else exc.status)
            # A failed battle keeps its server evidence: the report and the
            # health view must not turn five confirmed hits into "never began".
            error.details = {**exc.details,
                             "expected_windows": self.expected, "accepted_hits": len(self.receipts),
                             "server_perfects": sum(row["server_perfect"] for row in self.receipts),
                             "begin_confirmed": self._begun, "receipts": list(self.receipts),
                             "local_missed_windows": len(self.combat.missed),
                             "local_player_hp": self.combat.player_hp, "local_dead": self.combat.dead,
                             "lead_evidence": self._lead_evidence,
                             "window_states": sorted((copy.deepcopy(v) for v in self.window_states.values()),
                                                     key=lambda v: v.get("index", 0)),
                             "transport_diagnostics": list(getattr(self.api, "request_diagnostics", []))}
            raise error from None
        finally:
            for row in bound_broker.list_requests(include_finished=True):
                if row.get("event_fingerprint") == auth.entry_fingerprint and row["status"] not in {"accepted", "rejected"}:
                    bound_broker.cancel(row["request_id"], reason="battle_task_finished")
            # The reference's own HTTP clients and executor are created only
            # without an injected post_json, so there is nothing else to close.
            for task in (self._read_rewarm, self._read_pool_task):
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
