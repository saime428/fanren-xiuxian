"""Health evidence owned by each existing persistent event-monitor process."""
from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from runtime import log_print
from nanlonghou_trade import TRADE_FLOWS

RUNTIME_VERSION = "event-monitor-v1"
QUESTION_MONITOR_VERSION = "question-push-v1"
JIYIN_QUESTION_MONITOR_VERSION = "question-jiyin-v5"
QUESTION_FLOWS = frozenset({"jiyin_choice", "xuangu_zuoda", "shenji_budao"}) | TRADE_FLOWS
SEND_FLOWS = frozenset({"jiaohuan_fabao", "jiaohuan_gongfa", "jiyin_choice", "shenji_budao", "xuangu_zuoda"})
OBSERVER_FLOWS = frozenset({"shendubiguan", "yuanyingchuqiao"})
# Routine reconciliation reads only what is newer than each source's
# high-water mark. New messages and edits normally arrive by push; the full
# recovery window (TG_SCAN_LIMIT) is re-read at start, after the listener is
# re-verified, and at this interval as the backstop for missed edits.
FULL_HISTORY_SECONDS = 600
HISTORY_OVERLAP_IDS = 30


def question_monitor_version(flow: str) -> str:
    if flow in TRADE_FLOWS:
        return "nanlong-trade-v4"
    if flow == "xuangu_zuoda":
        return "question-xuangu-v7"
    if flow == "shenji_budao":
        return "shenji-receipt-v4"
    return JIYIN_QUESTION_MONITOR_VERSION if flow == "jiyin_choice" else QUESTION_MONITOR_VERSION


def health_filename(kind: str) -> str:
    if kind not in {"send", "return_observer"}:
        raise ValueError("unsupported monitor kind")
    return "monitor_health.json" if kind == "send" else "observer_health.json"


class MonitorIdentityMismatch(RuntimeError):
    pass


class HistoryWindow:
    """Per-source high-water marks for incremental history reads."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self.floors: dict[Any, int] = {}
        self.full_scan_ts = 0.0
        self.full = True

    def begin(self) -> bool:
        """Start one reconciliation pass; True when it must read the full window."""
        self.full = self.clock() - self.full_scan_ts >= FULL_HISTORY_SECONDS
        return self.full

    def min_id(self, key: Any) -> int:
        """Telethon ``min_id`` for this source; 0 reads the full window."""
        floor = self.floors.get(key)
        return 0 if self.full or not floor else max(1, floor - HISTORY_OVERLAP_IDS)

    def record(self, key: Any, messages: Any) -> None:
        ids = [int(getattr(message, "id", 0) or 0) for message in messages or ()]
        if ids:
            self.floors[key] = max(self.floors.get(key, 0), *ids)

    def complete(self) -> None:
        if self.full:
            self.full_scan_ts = self.clock()

    def invalidate(self) -> None:
        self.full_scan_ts = 0.0


class EventMonitorHealth:
    def __init__(self, state_dir: Path, account: str, flow: str, kind: str,
                 source_targets: list[str | int], reconcile_seconds: float):
        if not re.fullmatch(r"tg[0-9]+", account):
            raise ValueError("event monitor requires its business account")
        self.path = state_dir / health_filename(kind)
        self.expected_user_id = int(account[2:])
        self._closed = False
        self._disconnect_waiter: asyncio.Future | None = None
        self.history = HistoryWindow()
        self.data: dict[str, Any] = {
            "runtime_version": RUNTIME_VERSION, "account": account, "flow": flow,
            "kind": kind, "pid": os.getpid(), "started_at_ts": time.time(),
            "source_targets": [str(item) for item in source_targets],
            "reconcile_seconds": reconcile_seconds, "status": "starting",
            "last_success_ts": None, "listener_verified_ts": None,
            "listener_connected": False, "consecutive_failures": 0,
            "error_type": None, "error_stage": None, "retry_not_before_ts": 0,
        }
        self._write()

    def _write(self) -> None:
        self.data["ts"] = time.time()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.data, stream, ensure_ascii=False)
                stream.write("\n")
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def failed(self, stage: str, exc: BaseException) -> None:
        failures = int(self.data["consecutive_failures"]) + 1
        self.data.update(
            status="failed", error_type=type(exc).__name__, error_stage=stage,
            consecutive_failures=failures,
            retry_not_before_ts=time.time() + min(30, 2 ** min(failures, 5)),
        )
        self._write()
        # Exception text can contain request data. Record only its type.
        log_print(f"Event monitor failed: {self.data['account']}/{self.data['flow']} "
                  f"stage={stage} error={type(exc).__name__}")

    def watch_disconnect(self, client: Any) -> None:
        if self._disconnect_waiter is not None and not self._disconnect_waiter.done():
            return
        future = client.disconnected
        self._disconnect_waiter = future

        def disconnected(done: asyncio.Future) -> None:
            if done.cancelled():
                return
            error = done.exception()  # Consume Telethon's terminal future, including failures.
            if self._closed:
                return
            self.data["listener_connected"] = False
            self.failed("push_listener_disconnected", error or ConnectionError())

        future.add_done_callback(disconnected)

    def _listener_still_verified(self, client: Any) -> bool:
        waiter = self._disconnect_waiter
        return bool(self.data["listener_connected"] and client.is_connected()
                    and waiter is not None and not waiter.done())

    async def verify_listener(self, client: Any) -> None:
        from transport import ensure_connected, force_reconnect, with_telegram_retry

        # The identity of a connection cannot change while it stays up; ask
        # Telegram (GetUsers) only at start and after a disconnect or failure.
        if not self._listener_still_verified(client):
            async def verify() -> None:
                await ensure_connected(client)
                me = await client.get_me()
                if getattr(me, "id", None) != self.expected_user_id:
                    raise MonitorIdentityMismatch("push listener identity differs from business account")

            await with_telegram_retry(verify, label="verify event push listener",
                                      reconnect=lambda: force_reconnect(client))
            # Pushes may have been missed while the listener was down.
            self.history.invalidate()
        self.data.update(listener_connected=True, listener_verified_ts=time.time())
        self.watch_disconnect(client)

    async def run_cycle(self, client: Any, reconcile: Callable[..., Awaitable[Any]]) -> None:
        wait = max(0.0, self.data["retry_not_before_ts"] - time.time())
        if wait:
            await asyncio.sleep(wait)
        self.data.update(status="checking", cycle_started_ts=time.time())
        self._write()
        completed = False

        def scan_completed() -> None:
            nonlocal completed
            completed = True

        stage = "push_listener"
        try:
            await self.verify_listener(client)
            stage = "history_scan"
            await reconcile(on_scan_complete=scan_completed)
            # A paused/locked scan is not evidence that either source was read.
            if not completed:
                return
            stage = "push_listener"
            if not client.is_connected():
                raise ConnectionError("push listener disconnected during reconciliation")
            self.data.update(status="healthy", last_success_ts=time.time(),
                             error_type=None, error_stage=None, consecutive_failures=0,
                             retry_not_before_ts=0)
            self._write()
            log_print(f"Event monitor verified: {self.data['account']}/{self.data['flow']} "
                      f"kind={self.data['kind']} sources={len(self.data['source_targets'])}")
        except (Exception, SystemExit, asyncio.CancelledError) as exc:
            # Re-verify the listener (and re-read the full window) next cycle.
            self.data["listener_connected"] = False
            self.failed(stage, exc)
            raise

    def close(self) -> None:
        self._closed = True
        if self._disconnect_waiter is not None:
            if self._disconnect_waiter.done() and not self._disconnect_waiter.cancelled():
                self._disconnect_waiter.exception()
            else:
                self._disconnect_waiter.cancel()
