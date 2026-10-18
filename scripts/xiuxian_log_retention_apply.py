#!/usr/bin/env python3
"""Guarded one-week retention for Xiuxian rolling logs.

Rolling launchd logs stay open while their LaunchAgent is alive, so this
truncates them in place (rather than replacing/removing the path) and keeps
the most recent tail.  It never follows symlinks and does not touch state,
sessions, result JSON, or per-run artifacts.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE_ROOT = PROJECT_ROOT / "state" / "accounts"
DEFAULT_OUTPUT = PROJECT_ROOT.parent / "state" / "xiuxian-log-retention-apply.json"
DEFAULT_KEEP_BYTES = 10 * 1024 * 1024


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


def _write_all(fd: int, data: bytes | bytearray, offset: int) -> None:
    view = memoryview(data)
    while view:
        written = os.pwrite(fd, view, offset)
        view, offset = view[written:], offset + written


def trim_rolling_logs(state_root: Path, *, keep_bytes: int = DEFAULT_KEEP_BYTES) -> dict[str, Any]:
    """Trim ordinary *.log files in place, preserving their current inode.

    This is deliberately safe for active launchd writers: data is copied from
    the current file descriptor's tail back onto the same descriptor, followed
    by ftruncate. Symlinks, non-regular files, and paths outside state_root are
    skipped. Callers must schedule this; no network or gameplay action occurs.
    """
    root = Path(state_root)
    keep = max(0, int(keep_bytes))
    if root.is_symlink() or not root.is_dir():
        return {"ok": False, "reason": "invalid_state_root", "trimmed_count": 0, "items": []}
    items: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.log")):
        if path.is_symlink() or not _within(path, root):
            continue
        try:
            stat_before = path.stat(follow_symlinks=False)
        except OSError:
            continue
        if not path.is_file() or stat_before.st_size <= keep:
            continue
        try:
            fd = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            continue
        try:
            current = os.fstat(fd)
            if current.st_ino != stat_before.st_ino or current.st_size <= keep:
                continue
            start = max(0, current.st_size - keep)
            os.lseek(fd, start, os.SEEK_SET)
            tail = bytearray()
            while len(tail) < keep:
                block = os.read(fd, min(1024 * 1024, keep - len(tail)))
                if not block:
                    break
                tail.extend(block)
            _write_all(fd, tail, 0)
            # Writers keep appending (O_APPEND) while the tail is copied. Move
            # whatever arrived past the old end behind the copied tail before
            # cutting the file, so those lines are kept, not truncated away.
            # ponytail: the gap between the last fstat and ftruncate remains
            # (microseconds); closing it needs writer cooperation (a lock).
            end, length = current.st_size, len(tail)
            while (size := os.fstat(fd).st_size) > end:
                extra = os.pread(fd, size - end, end)
                _write_all(fd, extra, length)
                end, length = end + len(extra), length + len(extra)
            os.ftruncate(fd, length)
            os.fsync(fd)
            items.append({
                "path": str(path),
                "inode": current.st_ino,
                "before_bytes": current.st_size,
                "after_bytes": length,
                "reclaimed_bytes": max(0, end - length),
            })
        finally:
            os.close(fd)
    return {
        "ok": True,
        "state_root": str(root),
        "keep_bytes": keep,
        "trimmed_count": len(items),
        "reclaimed_bytes": sum(int(item["reclaimed_bytes"]) for item in items),
        "items": items,
    }


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Trim Xiuxian rolling logs in place to a retained tail.")
    parser.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    parser.add_argument("--keep-mib", type=int, default=10)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = trim_rolling_logs(args.state_root, keep_bytes=max(0, args.keep_mib) * 1024 * 1024)
    result["ran_at"] = time.time()
    _write_atomic(args.output, result)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
