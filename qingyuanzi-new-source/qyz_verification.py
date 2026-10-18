"""Run the supplied browser worker in the host, with one-shot memory handoffs."""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import math
import re
import secrets
import stat
import threading
import time
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from qyz_humanize import BrowserInput, Humanization

from qyz_reference.world_boss_browser import (
    AutomaticTurnstileWorker, BrowserVerificationError, NativeTurnstileBrowser, find_chrome,
    ORIGIN,
)
from qyz_reference.world_boss_turnstile import (
    BROWSER_EVENTS, FINISHED_STATUSES, MAX_TOKEN_LENGTH, STALE_RETENTION_SECONDS,
    TurnstileRequestError, WorldBossTurnstileBroker, _safe_id, _safe_text, _time_text,
)

LOG = logging.getLogger(__name__)

PREPARE_DEADLINE_SECONDS = 40
SCREENSHOT_CLIP = {"x": 0, "y": 0, "width": 1, "height": 1, "scale": 1}


def _is_symlink(path):
    try:
        return stat.S_ISLNK(Path(path).lstat().st_mode)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ValueError("verification_profile_path_invalid") from exc


class MemoryTurnstileBroker(WorldBossTurnstileBroker):
    """The reference broker contract, stored only in the owning host's memory."""

    def __init__(self, *, clock=time.time, request_ttl_seconds=180):
        self.clock = clock
        self.request_ttl_seconds = max(30, min(600, int(request_ttl_seconds)))
        self._thread_lock = threading.RLock()
        self._rows: dict[str, dict[str, Any]] = {}
        self._tokens: dict[str, str] = {}

    def _public_request(self, request):
        row = super()._public_request(request)
        row["browser_events"] = [dict(item) for item in request.get("browser_events", [])]
        return row

    def _cleanup(self):
        now = float(self.clock())
        for key, row in list(self._rows.items()):
            if row["status"] not in FINISHED_STATUSES and now >= row["expires_epoch"]:
                self._tokens.pop(key, None)
                row.update(status="expired", token_available=False, updated_epoch=now)
            if row["status"] in FINISHED_STATUSES and now - row["updated_epoch"] >= STALE_RETENTION_SECONDS:
                self._tokens.pop(key, None)
                self._rows.pop(key, None)

    def _row(self, request_id):
        self._cleanup()
        return self._rows.get(_safe_id(request_id))

    def create_request(self, *, event_fingerprint, message_id, account, identity,
                       challenge_id, origin, ttl_seconds=None):
        if origin != "https://asc.aiopenai.app":
            raise TurnstileRequestError("turnstile_origin_invalid")
        now = float(self.clock())
        ttl = self.request_ttl_seconds if ttl_seconds is None else max(30, min(600, int(ttl_seconds)))
        row = {
            "version": 1, "request_id": secrets.token_urlsafe(24),
            "event_fingerprint": _safe_text(event_fingerprint, 64),
            "message_id": int(message_id or 0), "account": _safe_text(account, 32),
            "identity": _safe_text(identity, 80), "challenge_id": _safe_text(challenge_id, 160),
            "origin": origin, "created_epoch": now, "created_at": _time_text(now),
            "expires_epoch": now + ttl, "expires_at": _time_text(now + ttl),
            "updated_epoch": now, "status": "pending", "token_available": False,
        }
        with self._thread_lock:
            self._cleanup()
            self._rows[row["request_id"]] = row
            return self._public_request(row)

    def get_request(self, request_id):
        with self._thread_lock:
            row = self._row(request_id)
            return self._public_request(row) if row else None

    def list_requests(self, *, include_finished=False):
        with self._thread_lock:
            self._cleanup()
            return [self._public_request(row) for row in self._rows.values()
                    if include_finished or row["status"] in {"pending", "submitted"}]

    def record_browser_event(self, request_id, event, error_code="", *, source="automatic"):
        code = str(error_code or "")
        if (event not in BROWSER_EVENTS or source not in {"manual", "automatic"}
                or (code and not re.fullmatch(r"[0-9]{3,6}", code))):
            raise TurnstileRequestError("turnstile_browser_event_invalid")
        with self._thread_lock:
            row = self._row(request_id)
            if not row:
                raise TurnstileRequestError("turnstile_request_not_found")
            if row["status"] == "pending":
                row.setdefault("browser_events", []).append({"event": event, "epoch": self.clock(),
                                                            "error_code": code})
                del row["browser_events"][:-24]
                row.update(browser_event=event, browser_error_code=code, browser_source=source,
                           browser_updated_at=_time_text(self.clock()), updated_epoch=self.clock())
            return self._public_request(row)

    def submit_token(self, request_id, token):
        value = str(token or "").strip()
        if not value or len(value) > MAX_TOKEN_LENGTH or any(ord(c) < 32 for c in value):
            raise TurnstileRequestError("turnstile_token_invalid")
        with self._thread_lock:
            row = self._row(request_id)
            if not row:
                raise TurnstileRequestError("turnstile_request_not_found")
            if row["status"] == "expired":
                raise TurnstileRequestError("turnstile_request_expired")
            if row["status"] != "pending":
                raise TurnstileRequestError("turnstile_request_already_submitted")
            self._tokens[request_id] = value
            row.update(status="submitted", token_available=True,
                       submitted_at=_time_text(self.clock()), updated_epoch=self.clock())
            return self._public_request(row)

    def take_token(self, request_id):
        with self._thread_lock:
            row = self._row(request_id)
            if not row or row["status"] != "submitted":
                return None
            value = self._tokens.pop(request_id, None)
            row.update(status="consumed", token_available=False,
                       consumed_at=_time_text(self.clock()), updated_epoch=self.clock())
            return value

    def record_result(self, request_id, *, accepted, error="", http_status=0):
        code = str(error or "")
        if code and not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", code):
            code = "request_failed"
        with self._thread_lock:
            row = self._row(request_id)
            if not row:
                return None
            if row["status"] == "consumed":
                row.update(status="accepted" if accepted else "rejected", token_available=False,
                           result_error="" if accepted else code,
                           result_http_status=max(0, min(599, int(http_status or 0))),
                           result_at=_time_text(self.clock()), updated_epoch=self.clock())
            return self._public_request(row)

    def cancel(self, request_id, *, reason="cancelled"):
        with self._thread_lock:
            row = self._row(request_id)
            if row and row["status"] not in {"accepted", "rejected"}:
                self._tokens.pop(request_id, None)
                row.update(status="cancelled", token_available=False, cancel_reason=_safe_text(reason, 80),
                           updated_epoch=self.clock())

    def cancel_all(self):
        with self._thread_lock:
            for key in list(self._rows):
                self.cancel(key, reason="host_stopped")
            self._tokens.clear()

    def for_account(self, account):
        return AccountTurnstileBroker(self, account)


class AccountTurnstileBroker:
    def __init__(self, broker, account):
        self.broker, self.account = broker, account

    def create_request(self, **kwargs):
        if kwargs.get("account") != self.account:
            raise TurnstileRequestError("turnstile_account_mismatch")
        return self.broker.create_request(**kwargs)

    def _check(self, request_id):
        row = self.broker.get_request(request_id)
        if row and row["account"] != self.account:
            raise TurnstileRequestError("turnstile_account_mismatch")

    def get_request(self, request_id):
        self._check(request_id)
        return self.broker.get_request(request_id)

    def take_token(self, request_id):
        self._check(request_id)
        return self.broker.take_token(request_id)

    def record_browser_event(self, request_id, event, error_code="", *, source="automatic"):
        self._check(request_id)
        return self.broker.record_browser_event(
            request_id, event, error_code, source=source,
        )

    def submit_token(self, request_id, token):
        self._check(request_id)
        return self.broker.submit_token(request_id, token)

    def record_result(self, request_id, **kwargs):
        self._check(request_id)
        return self.broker.record_result(request_id, **kwargs)

    def cancel(self, request_id, **kwargs):
        self._check(request_id)
        return self.broker.cancel(request_id, **kwargs)

    def list_requests(self, **kwargs):
        return [row for row in self.broker.list_requests(**kwargs) if row["account"] == self.account]

    def cancel_all(self):
        for row in self.list_requests():
            self.cancel(row["request_id"], reason="host_stopped")


class PreparedNativeTurnstileBrowser(NativeTurnstileBrowser):
    def __init__(self, *args, path_validator=None, widget_timeout=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._path_validator = path_validator
        self.widget_timeout = widget_timeout
        self._human_input = BrowserInput()

    def verify(self, origin=ORIGIN, *, timeout=None, **kwargs):
        if timeout is None:
            timeout = self.widget_timeout if self.widget_timeout is not None else 55
        return super().verify(origin, timeout=timeout, **kwargs)

    def park(self):
        """Leave the official page while no widget is needed.

        Its idle animation keeps ~20% of a core busy (0.3% on about:blank);
        the next verify() loads it again in 5-9 s.
        """
        if self.connection is None or self.origin != ORIGIN:
            return False
        self.command("Page.navigate", {"url": "about:blank"})
        self.origin = ""
        return True

    def command(self, method, params=None):
        if method == "Input.dispatchMouseEvent":
            return self._human_input.dispatch(
                params or {}, lambda event: super(PreparedNativeTurnstileBrowser, self).command(method, event),
            )
        if method == "Page.captureScreenshot" and not (params or {}).get("clip"):
            # The reference captures, and discards, a frame right before its
            # one checkbox click. The whole window on this 4K display is a
            # 4.5 MB PNG; receiving it froze every battle thread in the host
            # (09-24 13:41:23.7-26.7: two strikes lost, one hold cut to 312 ms;
            # offline: timer lateness 270 ms vs 10 ms). One pixel still forces
            # the frame.
            params = {**(params or {}), "clip": SCREENSHOT_CLIP}
        return super().command(method, params)

    def close(self):
        try:
            return super().close()
        finally:
            self._human_input = BrowserInput()

    def start(self):
        if self._path_validator is not None:
            self._path_validator()
        if self.connection is not None:
            return None
        return super().start()

    def prepare(self):
        """Load only the public page during admission; do not render a widget."""
        self.start()
        if self.origin == ORIGIN:
            return
        self.command("Page.navigate", {"url": ORIGIN + "/miniapp/xianxia-world-boss"})
        deadline = time.monotonic() + PREPARE_DEADLINE_SECONDS
        while time.monotonic() < deadline:
            if self.evaluate("Boolean(window.turnstile && window.__QYZ_TURNSTILE_CONFIG__)"):
                self.origin = ORIGIN
                return
            time.sleep(self._human_input.profile.interval(.25))
        raise BrowserVerificationError("turnstile_script_unavailable")


class VerificationService:
    def __init__(self, profile_dir: Path, *, browser=None, broker=None,
                 account_order=(), monotonic=time.monotonic):
        self.profile_dir = Path(profile_dir)
        self.broker = broker or MemoryTurnstileBroker()
        self.browser = browser or PreparedNativeTurnstileBrowser(self.profile_dir)
        self.now = monotonic
        self.account_order = tuple(account_order)
        self._account_priority = {account: index for index, account in enumerate(self.account_order)}
        self._event_first = {}
        self._pending_since = {}
        self._started_events = set()
        self.worker = AutomaticTurnstileWorker(self.broker, self.browser, clock=monotonic)
        self.worker.request_order = self._request_order
        self._preparation_lock = threading.RLock()
        self._preparation = {}
        self.task: asyncio.Task | None = None
        self.stopping = False
        self._humanization = Humanization()

    def prepare_event(self, event_fingerprint, *, valid_for_seconds):
        """Called only after the host validates a fresh official entry and actor.

        This signal contains no WebView URL, account credential or callback.
        Keep the original single worker and generate tokens only on its normal
        account/challenge-bound requests after the server requires verification.
        """
        if not re.fullmatch(r"[0-9a-f]{64}", str(event_fingerprint)):
            raise ValueError("event_fingerprint_invalid")
        if (isinstance(valid_for_seconds, bool) or not isinstance(valid_for_seconds, (int, float))
                or not math.isfinite(valid_for_seconds) or valid_for_seconds <= 0):
            return
        until = self.now() + min(120, valid_for_seconds)
        with self._preparation_lock:
            if self._preparation.get("event_fingerprint") == event_fingerprint:
                self._preparation["until"] = max(until, self._preparation["until"])
            else:
                self._preparation = {"event_fingerprint": event_fingerprint, "until": until,
                                     "attempted": False, "ready": False, "error": ""}

    def _request_order(self, row):
        # FIFO between events, declared account order within one event. The
        # reference worker retains its attempt limit and first-attempt priority.
        return (self._event_first.get(row.get("event_fingerprint"), str(row.get("created_at", ""))),
                self._account_priority.get(row.get("account"), len(self.account_order)),
                str(row.get("created_at", "")))

    def _run_once(self):
        with self._preparation_lock:
            preparation = dict(self._preparation)
        if preparation.get("until", 0) > self.now():
            if not preparation.get("attempted"):
                started = self.now()
                ready, error = False, ""
                try:
                    prepare = getattr(self.browser, "prepare", None)
                    if callable(prepare):
                        prepare()
                        ready = True
                except Exception as exc:
                    error = exc.code if isinstance(exc, BrowserVerificationError) else "public_page_prepare_failed"
                    try:
                        self.browser.close()
                    except Exception:
                        pass
                with self._preparation_lock:
                    if self._preparation.get("event_fingerprint") == preparation["event_fingerprint"]:
                        self._preparation.update(attempted=True, ready=ready, error=error,
                                                 duration_ms=round((self.now() - started) * 1000))
            # The original idle close remains in force outside the short,
            # validated admission period. Preparation never creates a token.
            self.worker.last_work = self.now()
        rows = [row for row in self.broker.list_requests() if row.get("status") == "pending"]
        live_events = {row.get("event_fingerprint") for row in rows}
        self._pending_since = {key: at for key, at in self._pending_since.items() if key in live_events}
        self._started_events.intersection_update(live_events)
        self._event_first = {key: min(str(row.get("created_at", "")) for row in rows
                                     if row.get("event_fingerprint") == key) for key in live_events}
        if rows and self.account_order:
            event = min(live_events, key=lambda key: self._event_first[key])
            since = self._pending_since.setdefault(event, self.now())
            has_main = any(row.get("account") == self.account_order[0]
                           and row.get("event_fingerprint") == event for row in rows)
            if event not in self._started_events and not has_main and self.now() - since < 1:
                return False
            self._started_events.add(event)
        return self.worker.run_once()

    def capability(self):
        try:
            find_chrome(getattr(self.browser, "chrome", None))
            if importlib.util.find_spec("websocket") is None:
                raise BrowserVerificationError("websocket_client_missing")
        except BrowserVerificationError as exc:
            return {"available": False, "error": exc.code}
        return {"available": self.task is not None and not self.task.done(), "error": ""}

    def start(self):
        if self.task is None or self.task.done():
            self.stopping = False
            self.task = asyncio.create_task(self._run(), name="qyz_reference_verifier")

    async def _run(self):
        try:
            while not self.stopping:
                try:
                    await asyncio.to_thread(self._run_once)
                except Exception as exc:
                    # The reference loop survives any worker exception; losing this
                    # task silently left all six accounts without tokens until a
                    # supervisor restart. Close the browser and carry on.
                    logging.getLogger(__name__).error("verification cycle interrupted: %s", type(exc).__name__)
                    with suppress(Exception):
                        await asyncio.to_thread(self.browser.close)
                    await asyncio.sleep(self._humanization.interval(2))
                    continue
                await asyncio.sleep(self._humanization.interval(.25))
        finally:
            await asyncio.to_thread(self.browser.close)

    async def close(self):
        self.stopping = True
        self.broker.cancel_all()
        if self.task:
            await self.task

    def snapshot(self):
        rows = self.broker.list_requests(include_finished=True)
        with self._preparation_lock:
            preparation = {key: value for key, value in self._preparation.items() if key != "until"}
        return {**self.capability(), "pending": sum(row["status"] == "pending" for row in rows),
                "callbacks": sum(row.get("browser_event") == "token_generated" for row in rows),
                "accepted": sum(row["status"] == "accepted" for row in rows),
                "preparation": preparation, "recent": rows[-12:]}


# v24 (2026-09-23 review): on every event since v19 (09-17..09-23) the six
# account verifiers plus the fallback rendered their widgets together at
# 13:41 and the first attempts timed out (55 s each); tokens arrived only once
# the shards had given up and the one fallback browser solved alone, 8-10 s
# apiece, which is too late for the last accounts (2/6 missed on 09-23). The
# serial v18 verifier solved 12/12 at 13:41 on 09-15/16, and an offline replay
# of the same profiles solves in 2-18 s. So: one warm browser, one widget at a
# time. The token is not bound to a challenge (static action, no cData) and
# lives 300 s, so tokens are solved ahead and handed to each account's first
# verification request.
#
# v25 (2026-09-24 review, 6/6 fought): loading the page after admission took
# 19 s, so only four tokens were ready when the room opened and @demo_player_06's was
# solved during the battles, where its widget froze the other battle threads.
# The browser is now warm before the daily entry and one token per account is
# solved from 13:39:50, ahead of the ~13:40 entry and its ~48 s joining wait;
# admission still asks for any account left without one.
#
# v27 (2026-09-27 review): the first widget after the warm-up is always the
# slow one (12-15 s on 09-25/26). On 09-27 the page was warm at 13:39:24 but
# sat idle until 13:39:50, then that first widget timed out (35 s); the last
# token (@demo_player_06) was still being solved when the room opened and its battle
# began 5.8 s after the others. The warm-up now starts at 13:38:30 and the
# presolve follows the warm page at once, so even one failed widget leaves
# every token ready before the ~13:41 room; the oldest is ~130 s old when it
# is handed out, inside PRESOLVED_MAX_AGE_SECONDS.
SERIAL_WIDGET_TIMEOUT_SECONDS = 35
SERIAL_MAX_ATTEMPTS = 3
PRESOLVE_DEMAND_SECONDS = 150
PRESOLVED_MAX_AGE_SECONDS = 240
SHANGHAI = ZoneInfo("Asia/Shanghai")
DAILY_WARM_START = (13, 38, 30)
DAILY_PRESOLVE_AT = DAILY_WARM_START
DAILY_WARM_END = (13, 47, 0)
WARM_RETRY_SECONDS = 15


class PresolveTurnstileBroker(MemoryTurnstileBroker):
    """Memory broker that can satisfy a new request with a token solved in advance.

    Tokens stay in this process's memory only, belong to one account, are
    handed out once and expire after ``PRESOLVED_MAX_AGE_SECONDS``.
    """

    def __init__(self, *, clock=time.time, request_ttl_seconds=180):
        super().__init__(clock=clock, request_ttl_seconds=request_ttl_seconds)
        self._demand: dict[str, float] = {}
        self._presolved: dict[str, tuple[str, float]] = {}
        self.presolve_stats = {"solved": 0, "attached": 0, "expired": 0}

    def _cleanup(self):
        super()._cleanup()
        now = float(self.clock())
        for account, until in list(self._demand.items()):
            if now >= until:
                self._demand.pop(account, None)
        for account, (_token, solved) in list(self._presolved.items()):
            if now - solved > PRESOLVED_MAX_AGE_SECONDS:
                self._presolved.pop(account, None)
                self.presolve_stats["expired"] += 1

    def _has_live_request(self, account):
        return any(row["account"] == account and row["status"] in {"pending", "submitted"}
                   for row in self._rows.values())

    def want_presolve(self, account, *, valid_for_seconds=PRESOLVE_DEMAND_SECONDS):
        account = _safe_text(account, 32)
        with self._thread_lock:
            self._cleanup()
            if account in self._presolved or self._has_live_request(account):
                return
            until = float(self.clock()) + max(0.0, min(float(PRESOLVE_DEMAND_SECONDS), float(valid_for_seconds)))
            self._demand[account] = max(until, self._demand.get(account, 0.0))

    def presolve_demand(self):
        """Accounts still wanting a token, oldest demand first."""
        with self._thread_lock:
            self._cleanup()
            for account in [a for a in self._demand if a in self._presolved or self._has_live_request(a)]:
                self._demand.pop(account, None)
            return list(self._demand)

    def presolve_useful(self, account):
        """A token being solved for this account can still be stored or handed to its request."""
        account = _safe_text(account, 32)
        with self._thread_lock:
            self._cleanup()
            return account in self._demand or any(
                row["account"] == account and row["status"] == "pending" for row in self._rows.values())

    def store_presolved(self, account, token):
        value = str(token or "").strip()
        if not value or len(value) > MAX_TOKEN_LENGTH or any(ord(c) < 32 for c in value):
            raise TurnstileRequestError("turnstile_token_invalid")
        account = _safe_text(account, 32)
        with self._thread_lock:
            self._cleanup()
            self._demand.pop(account, None)
            self.presolve_stats["solved"] += 1
            # A request that arrived while this widget was solving takes the token at once.
            pending = [row for row in self._rows.values()
                       if row["account"] == account and row["status"] == "pending"]
            if pending:
                self._attach(pending[0]["request_id"], value)
                return "attached"
            self._presolved[account] = (value, float(self.clock()))
            return "stored"

    def _attach(self, request_id, token):
        self.record_browser_event(request_id, "token_generated", source="automatic")
        self.submit_token(request_id, token)
        self._rows[request_id]["presolved"] = True
        self.presolve_stats["attached"] += 1

    def create_request(self, **kwargs):
        row = super().create_request(**kwargs)
        account = row["account"]
        with self._thread_lock:
            self._cleanup()
            self._demand.pop(account, None)
            item = self._presolved.pop(account, None)
            if item is None:
                return row
            self._attach(row["request_id"], item[0])
            return self._public_request(self._rows[row["request_id"]])

    def _public_request(self, request):
        row = super()._public_request(request)
        row["presolved"] = bool(request.get("presolved"))
        return row

    def cancel_all(self):
        with self._thread_lock:
            self._demand.clear()
            self._presolved.clear()
        super().cancel_all()

    def presolve_snapshot(self):
        with self._thread_lock:
            self._cleanup()
            return {**self.presolve_stats, "waiting": len(self._demand), "ready": len(self._presolved)}


class SerialVerificationService(VerificationService):
    """One warm native browser on the proven shared profile serves every account.

    Real verification requests always go first, in declared account order, with
    the reference worker's attempt accounting. When none is pending, the same
    browser solves one token for each account still waiting for one. During the
    daily battle window the page stays loaded and every account gets a token
    before the entry.
    """

    def __init__(self, profile_dir: Path, *, account_order, trusted_root=None, browser=None,
                 broker=None, monotonic=time.monotonic, wall_clock=time.time):
        accounts = tuple(str(account) for account in account_order)
        if (not accounts or not all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", a) for a in accounts)
                or len(accounts) != len({a.casefold() for a in accounts})):
            raise ValueError("verification_accounts_invalid")
        self.trusted_root = Path(trusted_root) if trusted_root is not None else Path(profile_dir).parent
        self._profile_path = Path(profile_dir)
        self._validate_profile_path()
        super().__init__(
            profile_dir,
            browser=browser or PreparedNativeTurnstileBrowser(
                profile_dir, path_validator=self._validate_profile_path,
                widget_timeout=SERIAL_WIDGET_TIMEOUT_SECONDS),
            broker=broker or PresolveTurnstileBroker(),
            account_order=accounts, monotonic=monotonic,
        )
        self.worker.max_attempts = SERIAL_MAX_ATTEMPTS
        self.wall = wall_clock
        self._accounts = set(accounts)
        self._presolve_failures: dict[str, int] = {}
        self._warm = {"day": "", "retry_at": 0.0, "presolve_day": "", "error": ""}

    def _validate_profile_path(self):
        trusted, target = self.trusted_root, self._profile_path
        try:
            relative = target.relative_to(trusted)
            if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
                raise ValueError
            current = trusted
            if _is_symlink(current):
                raise ValueError
            for part in relative.parts:
                current = current / part
                if _is_symlink(current):
                    raise ValueError
            target.resolve(strict=False).relative_to(trusted.resolve(strict=False))
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError("verification_profile_path_invalid") from exc

    def prepare_event(self, event_fingerprint, *, valid_for_seconds, account=None):
        super().prepare_event(event_fingerprint, valid_for_seconds=valid_for_seconds)
        if account is None or str(account) not in self._accounts:
            return
        if (isinstance(valid_for_seconds, bool) or not isinstance(valid_for_seconds, (int, float))
                or not math.isfinite(valid_for_seconds) or valid_for_seconds <= 0):
            return
        self._presolve_failures.pop(str(account), None)
        self.broker.want_presolve(str(account), valid_for_seconds=valid_for_seconds)

    def _daily_warm(self):
        """Hold a loaded page through the daily battle window and pre-solve before the entry."""
        local = datetime.fromtimestamp(self.wall(), SHANGHAI)
        clock = (local.hour, local.minute, local.second)
        if not DAILY_WARM_START <= clock < DAILY_WARM_END:
            return
        day = local.date().isoformat()
        # The idle close would otherwise relaunch Chrome in the middle of the battles.
        self.worker.last_work = self.now()
        if self._warm["day"] != day and self.now() >= self._warm["retry_at"]:
            started = self.now()
            try:
                self.browser.prepare()
                self._warm.update(day=day, error="")
                LOG.info("QYZ verifier page warm duration_ms=%s", round((self.now() - started) * 1000))
            except Exception as exc:  # noqa: BLE001 - verify() still loads the page itself
                code = exc.code if isinstance(exc, BrowserVerificationError) else type(exc).__name__
                self._warm.update(retry_at=self.now() + WARM_RETRY_SECONDS, error=code)
                LOG.warning("QYZ verifier page warm failed: %s", code)
                try:
                    self.browser.close()
                except Exception:  # noqa: BLE001
                    pass
        if clock >= DAILY_PRESOLVE_AT and self._warm["presolve_day"] != day:
            self._warm["presolve_day"] = day
            self._presolve_failures.clear()
            for account in self.account_order:
                self.broker.want_presolve(account, valid_for_seconds=PRESOLVE_DEMAND_SECONDS)

    def _run_once(self):
        self._daily_warm()
        if super()._run_once():
            return True
        if any(row.get("status") == "pending" for row in self.broker.list_requests()):
            return False
        wanted = [account for account in self.broker.presolve_demand()
                  if self._presolve_failures.get(account, 0) < SERIAL_MAX_ATTEMPTS - 1]
        if not wanted:
            self._park_after_daily_presolve()
            return False
        order = {account: index for index, account in enumerate(self.account_order)}
        account = min(wanted, key=lambda item: order.get(item, len(order)))

        def still_wanted():
            return self.broker.presolve_useful(account)

        started = self.now()
        try:
            token = self.browser.verify(ORIGIN, still_pending=still_wanted)
            outcome = self.broker.store_presolved(account, token)
            token = ""
            LOG.info("QYZ verifier presolved account=%s outcome=%s duration_ms=%s",
                     account, outcome, round((self.now() - started) * 1000))
        except (BrowserVerificationError, TurnstileRequestError) as exc:
            code = getattr(exc, "code", type(exc).__name__)
            if code != "verification_request_finished":
                self._presolve_failures[account] = self._presolve_failures.get(account, 0) + 1
            LOG.warning("QYZ verifier presolve account=%s failed: %s CF=%s",
                        account, code, getattr(exc, "cf_code", ""))
            if isinstance(exc, BrowserVerificationError) and not exc.code.startswith("turnstile_browser_"):
                self.browser.close()
        finally:
            self.worker.last_work = self.now()
        return True

    def _park_after_daily_presolve(self):
        # Tokens are ready and nothing is waiting: keep the browser, drop the
        # page, so the battles do not share the CPU with its animation.
        today = datetime.fromtimestamp(self.wall(), SHANGHAI).date().isoformat()
        park = getattr(self.browser, "park", None)
        if self._warm["presolve_day"] != today or not callable(park):
            return
        try:
            if park():
                LOG.info("QYZ verifier parked until the next widget")
        except Exception as exc:  # noqa: BLE001 - a broken browser is relaunched by verify()
            LOG.warning("QYZ verifier park failed: %s", getattr(exc, "code", type(exc).__name__))
            self.browser.close()

    def capability(self, account=None):
        row = super().capability()
        if account is None:
            return {**row, "account_count": len(self.account_order)}
        if str(account) not in self._accounts:
            return {"available": False, "error": "verification_account_invalid", "account": str(account)}
        return {**row, "account": str(account)}

    def start_account(self, account):
        if str(account) not in self._accounts:
            raise ValueError("verification_account_invalid")
        self._validate_profile_path()
        self.start()

    async def close_account(self, account):
        # One browser serves the fleet; a single account supervisor restart
        # must not stop it or cancel the other accounts' requests.
        if str(account) not in self._accounts:
            raise ValueError("verification_account_invalid")
        with self.broker._thread_lock:
            for row in self.broker.list_requests():
                if row["account"] == str(account):
                    self.broker.cancel(row["request_id"], reason="account_closed")

    def snapshot(self):
        snap = super().snapshot()
        presolve = getattr(self.broker, "presolve_snapshot", None)
        return {**snap, "mode": "serial_shared_profile",
                "widget_timeout_seconds": SERIAL_WIDGET_TIMEOUT_SECONDS,
                "warm": dict(self._warm),
                "presolve": presolve() if callable(presolve) else None}
