"""Bounded, redacted message facts for unknown-send recovery."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

_SECRET = re.compile(r"(?i)(tgwebappdata|token|authorization|api_hash|cookie|session)\s*[=:]\s*[^\s,;]+")
_MAX_TEXT = 600
_MAX_BYTES = 256 * 1024
_MAX_LINES = 5000


def _safe_text(value: object) -> str:
    return _SECRET.sub(r"\1=[REDACTED]", str(value or ""))[:_MAX_TEXT]


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def _fact_key(fact: dict[str, Any]) -> str:
    return "|".join(str(fact.get(key) or "") for key in ("event_type", "chat_id", "message_id", "revision", "text_digest"))


def normalize_message_fact(message: object = None, *, event_type: str = "message", identity_id: int = 0, chat_id: int = 0, message_id: int = 0, sender_id: int = 0, text: str = "", ts: float | None = None, revision: int = 0, **extra: object) -> dict[str, Any]:
    if message is not None:
        chat_id = int(getattr(message, "chat_id", chat_id) or chat_id or 0)
        message_id = int(getattr(message, "id", message_id) or message_id or 0)
        sender_id = int(getattr(message, "sender_id", sender_id) or sender_id or 0)
        text = str(getattr(message, "message", text) or text or "")
        date = getattr(message, "date", None)
        if ts is None and date is not None:
            with_ts = getattr(date, "timestamp", None)
            ts = float(with_ts()) if callable(with_ts) else None
    safe_text = _safe_text(text)
    fact = {
        "ts": float(ts if ts is not None else time.time()),
        "event_type": str(event_type or "message"),
        "identity_id": int(identity_id or 0),
        "chat_id": int(chat_id or 0),
        "message_id": int(message_id or 0),
        "sender_id": int(sender_id or 0),
        "text": safe_text,
        "text_digest": _digest(safe_text),
        "revision": max(0, int(revision or 0)),
    }
    for key, value in extra.items():
        if key not in fact and value is not None:
            fact[key] = _safe_text(value) if isinstance(value, str) else value
    return fact


class MessageFactStore:
    def __init__(self, path: Path, *, max_bytes: int = _MAX_BYTES, max_lines: int = _MAX_LINES):
        self.path = Path(path)
        self.max_bytes = max(1024, int(max_bytes))
        self.max_lines = max(1, int(max_lines))

    def append(self, fact: dict[str, Any]) -> bool:
        item = normalize_message_fact(**fact)
        existing = self.read()
        key = _fact_key(item)
        if any(_fact_key(row) == key for row in existing):
            return False
        existing.append(item)
        existing = existing[-self.max_lines:]
        raw_lines = [json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in existing]
        while sum(len(line.encode("utf-8")) + 1 for line in raw_lines) > self.max_bytes and raw_lines:
            raw_lines.pop(0)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text("\n".join(raw_lines) + ("\n" if raw_lines else ""), encoding="utf-8")
        os.replace(tmp, self.path)
        return True

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()[-self.max_lines:]
        except OSError:
            return []
        rows = []
        for line in lines:
            try:
                item = json.loads(line)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(item, dict):
                rows.append(item)
        return rows


__all__ = ["MessageFactStore", "normalize_message_fact"]
