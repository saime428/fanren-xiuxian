#!/usr/bin/env python3
"""Atomic JSON state, flock helpers and Asia/Shanghai clock helpers."""
from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


class LockBusy(RuntimeError):
    pass


def shanghai_now(ts: float | None = None) -> datetime:
    return datetime.fromtimestamp(time.time() if ts is None else float(ts), SHANGHAI)


def day_key(ts: float | None = None) -> str:
    return shanghai_now(ts).date().isoformat()


def next_midnight_ts(ts: float | None = None) -> float:
    now = shanghai_now(ts)
    following = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return following.timestamp()


def clock_reached(ts: float | None, hour: int, minute: int = 0) -> bool:
    now = shanghai_now(ts)
    return (now.hour, now.minute) >= (int(hour), int(minute))


def read_json(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {}
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("state_path_invalid")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "malformed_state"}
    return data if isinstance(data, dict) else {"status": "malformed_state"}


def write_json(path: Path, data: dict[str, Any]) -> Path:
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=directory)
    tmp = Path(raw_tmp)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    return path


@contextmanager
def exclusive_lock(path: Path, *, blocking: bool = False) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise LockBusy(str(path)) from exc
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
