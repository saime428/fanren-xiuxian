#!/usr/bin/env python3
"""Once-only operator notifications for MiniApp flows via the Hermes send CLI.

Delivery reuses the same channel the recipe-notice monitor already uses:
``hermes send --json --to telegram:<owner> --file -``. Every notification is
keyed; a key is delivered at most once per flow, tracked in
``<root>/state/<flow>/notifications.json`` (read, send and record happen
under one lock: several cron processes report into the same file). Delivery
failures never fail a flow run; they are recorded and retried on the next tick
with the same key, except a send that may have gone out (the CLI timed out):
that key is recorded as delivered-unconfirmed and never sent again.

This module depends only on the standard library plus ``miniapp_shared.state``
so standalone flows (for example ``fishing/``) can import it without pulling in
Telethon or the dwelling adapters.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from .accounts import NOTIFY
from .state import exclusive_lock, read_json, write_json

# The accounts config's "notify" section names the Hermes recipient; without one
# operator notifications are disabled (MINIAPP_NOTIFY_* env vars still override).
DEFAULT_HERMES_BIN = str(Path(NOTIFY.get("hermes_bin") or "~/.hermes/hermes-agent/venv/bin/hermes").expanduser())
DEFAULT_TARGET = NOTIFY.get("target", "")
MAX_KEYS = 200
MAX_TEXT = 3500

Sender = Callable[[str], dict[str, Any]]


def hermes_target() -> str:
    return os.environ.get("MINIAPP_NOTIFY_TARGET", DEFAULT_TARGET).strip() or DEFAULT_TARGET


def hermes_bin() -> str:
    return os.environ.get("HERMES_SEND_BIN", DEFAULT_HERMES_BIN).strip() or DEFAULT_HERMES_BIN


def notifications_disabled() -> bool:
    return (os.environ.get("MINIAPP_NOTIFY_DISABLED", "").strip() in {"1", "true", "yes"}
            or not hermes_target())


SEND_ATTEMPTS = 3
SEND_RETRY_SECONDS = 5.0


class DeliveryUnconfirmed(RuntimeError):
    """The message may have been sent; resending the key could duplicate it."""


def deliver_with_hermes(text: str, *, target: str | None = None, binary: str | None = None,
                        attempts: int = SEND_ATTEMPTS, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Send one plain-text message and require a confirmed delivery receipt.

    A non-zero exit means nothing was sent, so the call is retried a bounded
    number of times: the gateway's Telegram path flaps for 10-20 s at a time
    (12 batch summaries were lost on 2026-09-20/21 to single-shot sends). An
    unconfirmed receipt after exit 0 is never retried; it may have been sent.
    """
    target = target or hermes_target()
    binary = binary or hermes_bin()
    completed = None
    for attempt in range(max(1, int(attempts))):
        if attempt:
            sleep(SEND_RETRY_SECONDS)
        try:
            completed = subprocess.run(
                [binary, "send", "--json", "--to", target, "--file", "-"],
                input=text[:MAX_TEXT], capture_output=True, text=True, timeout=30, check=False,
            )
        except subprocess.TimeoutExpired as exc:  # killed mid-send: it may have gone out
            raise DeliveryUnconfirmed("hermes_send_unconfirmed: timed out") from exc
        if completed.returncode == 0:
            break
    assert completed is not None
    if completed.returncode != 0:
        raise RuntimeError(f"hermes_send_failed: {_failure_detail(completed.stdout, completed.stderr)[:200]}")
    try:
        receipt = json.loads(completed.stdout)
        message_id = int(receipt.get("message_id") or 0)
    except (ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError("hermes_send_unconfirmed: missing delivery receipt") from exc
    if (receipt.get("success") is not True or receipt.get("skipped") or receipt.get("error")
            or isinstance(receipt.get("message_id"), bool) or message_id <= 0):
        raise RuntimeError("hermes_send_unconfirmed: no confirmed message delivery")
    return {"target": target, "message_id": message_id, "success": True}


def _failure_detail(stdout: str, stderr: str) -> str:
    """The delivery error itself, not the CLI's startup banner.

    ``hermes send --json`` prints its ``{"error": ...}`` payload on stdout; stderr
    starts with the pending-restart warning after a ``hermes update``, which on
    2026-09-21 hid every real cause (Telegram API connect errors) behind
    "⚠ A previous `hermes update` pulled new code…" in notifications.json.
    """
    try:
        payload = json.loads(stdout or "")
    except ValueError:
        payload = None
    if isinstance(payload, dict) and payload.get("error"):
        return str(payload["error"]).strip()
    lines = [line.strip() for line in (stderr or "").splitlines()
             if line.strip() and not line.lstrip().startswith("⚠") and not line.startswith("  ")]
    return " ".join(lines) or (stdout or "").strip() or "delivery failed"


class Notifier:
    def __init__(self, root: Path, flow: str, *, sender: Sender | None = None, now: Callable[[], float] = time.time):
        self.root = Path(root)
        self.flow = str(flow)
        self.sender = sender or deliver_with_hermes
        self.now = now
        self.path = self.root / "state" / self.flow / "notifications.json"

    def _load(self) -> dict[str, Any]:
        data = read_json(self.path)
        if data.get("status") == "malformed_state":
            data = {}
        data.setdefault("delivered", [])
        data.setdefault("failures", [])
        return data

    def delivered(self, key: str) -> bool:
        return any(item.get("key") == key for item in self._load()["delivered"] if isinstance(item, dict))

    def delivered_at(self, key: str) -> float | None:
        """When ``key`` was delivered, or ``None`` if it never was."""
        for item in self._load()["delivered"]:
            if isinstance(item, dict) and item.get("key") == key:
                try:
                    return float(item.get("at"))
                except (TypeError, ValueError):
                    return None
        return None

    def send_once(self, key: str, text: str) -> dict[str, Any]:
        """Deliver ``text`` for ``key`` unless already delivered. Never raises."""
        if notifications_disabled():
            return {"status": "disabled", "key": key}
        with exclusive_lock(self.path.with_name(f"{self.path.name}.lock"), blocking=True):
            state = self._load()
            if any(item.get("key") == key for item in state["delivered"] if isinstance(item, dict)):
                return {"status": "already_delivered", "key": key}
            try:
                receipt = self.sender(text)
            except DeliveryUnconfirmed as exc:
                error = f"{type(exc).__name__}: {str(exc)[:200]}"
                row = {"key": key, "at": self.now(), "unconfirmed": True, "error": error, "text_head": text[:120]}
                state["delivered"] = (state["delivered"] + [row])[-MAX_KEYS:]
                write_json(self.path, state)
                return {"status": "delivery_unconfirmed", "key": key, "error": error}
            except Exception as exc:  # noqa: BLE001 - notification must never break the flow
                failure = {"key": key, "error": f"{type(exc).__name__}: {str(exc)[:200]}", "at": self.now()}
                state["failures"] = (state["failures"] + [failure])[-50:]
                write_json(self.path, state)
                return {"status": "delivery_failed", "key": key, "error": failure["error"]}
            row = {"key": key, "at": self.now(), "receipt": receipt, "text_head": text[:120]}
            state["delivered"] = (state["delivered"] + [row])[-MAX_KEYS:]
            write_json(self.path, state)
            return {"status": "delivered", "key": key, "receipt": receipt}
