#!/usr/bin/env python3
"""Small append-only, redacted attempt ledger for account/flow automation.

This borrows mian's command-attempt/evidence idea without introducing a new
runtime database: each flow keeps a bounded JSONL ledger under its existing
isolated state directory. It is diagnostic evidence, not a success oracle.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

_SENSITIVE = re.compile(r"token|cookie|session|authorization|password|passwd|secret|init[_-]?data|api[_-]?hash", re.I)
_MAX_TEXT = 600
_MAX_DEPTH = 6
_MAX_BYTES = 256 * 1024


def _sanitize(value: Any, depth: int = 0) -> Any:
    if depth > _MAX_DEPTH:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            out[key_text] = "[REDACTED]" if _SENSITIVE.search(key_text) else _sanitize(item, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, depth + 1) for item in value[:100]]
    if isinstance(value, str):
        return value[:_MAX_TEXT]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:_MAX_TEXT]


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def append_event(path: Path, *, attempt_id: str, event: str, state: str, payload: dict[str, Any] | None = None) -> None:
    """Append one redacted transition/evidence event atomically enough for one flow."""
    path.parent.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {
        "ts": time.time(),
        "pid": os.getpid(),
        "attempt_id": str(attempt_id),
        "event": str(event),
        "state": str(state),
    }
    if payload:
        safe = _sanitize(payload)
        if isinstance(safe, dict):
            record["payload"] = safe
    record["payload_digest"] = _digest(json.dumps(record.get("payload", {}), ensure_ascii=False, sort_keys=True))
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    if len(line.encode("utf-8")) > 8192:
        record["payload"] = {"truncated": True}
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    _trim(path)


def _trim(path: Path) -> None:
    try:
        if path.stat().st_size <= _MAX_BYTES:
            return
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-1000:]
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        return


def attempt_id(flow: str, run_id: str | int | None = None) -> str:
    return f"{flow}:{run_id or int(time.time() * 1000)}"


def ledger_path(state_dir: Path) -> Path:
    return state_dir / "attempt_ledger.jsonl"


__all__ = ["append_event", "attempt_id", "ledger_path"]
