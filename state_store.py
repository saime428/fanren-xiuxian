#!/usr/bin/env python3
from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, TextIO

from runtime import log_print


def load_json_dict(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def persist_json_atomic(path: Path, payload: dict[str, Any], *, mode: int | None = None) -> None:
    """Replace ``path`` with ``payload``: readers see the old or the new file, never a part.

    The temporary file is flushed to disk before the rename and removed when
    anything fails. ``mode`` sets the permissions; otherwise the umask does.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
    try:
        with open(tmp_path, "x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            if mode is not None:
                os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def write_lock_metadata(lock_path: Path, payload: dict[str, Any]) -> None:
    try:
        lock_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:
        log_print(f"Warning: failed to write lock metadata to {lock_path}: {exc}")


def is_paused(state_dir: Path, flow_slug: str, extra_dirs: tuple[Path, ...] = ()) -> tuple[bool, Path]:
    directories = [state_dir, *extra_dirs]
    # state/accounts/<account>/pause_all pauses every flow of that account.
    # Only a real account directory counts, never whatever sits above a
    # non-account state dir such as the default "state".
    if state_dir.parent.parent.name == "accounts":
        directories.append(state_dir.parent)
    candidates: list[Path] = []
    for directory in directories:
        candidates.append(directory / f"pause_{flow_slug}")
        candidates.append(directory / "pause_all")
    for candidate in candidates:
        if candidate.exists():
            return True, candidate
    return False, candidates[0]


class FlowLockBusy(Exception):
    def __init__(self, lock_path: Path, meta_text: str):
        self.lock_path = lock_path
        self.meta_text = meta_text
        super().__init__(f"lock busy: {lock_path}")


@contextmanager
def flow_lock(lock_path: Path) -> Iterator[TextIO]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(3):
        handle = lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                handle.seek(0)
                meta_text = handle.read().strip()
            except Exception:
                meta_text = ""
            handle.close()
            raise FlowLockBusy(lock_path, meta_text) from None
        # The path may have been unlinked/recreated between open and flock;
        # holding a lock on a dangling inode would not exclude anyone.
        try:
            path_ino = os.stat(lock_path).st_ino
        except FileNotFoundError:
            path_ino = -1
        if path_ino != os.fstat(handle.fileno()).st_ino:
            handle.close()
            continue
        try:
            yield handle
        finally:
            # Unlink before releasing so no one can lock the dying inode.
            try:
                lock_path.unlink(missing_ok=True)
            except Exception:
                pass
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            handle.close()
        return
    raise FlowLockBusy(lock_path, "lock file kept changing while acquiring")


def load_retry_state(path: Path) -> dict[str, Any]:
    return load_json_dict(path)


def save_retry_state(path: Path, state: dict[str, Any]) -> None:
    persist_json_atomic(path, state)


def set_pending_retry(path: Path, state_key: str, payload: dict[str, Any]) -> None:
    state = load_retry_state(path)
    state[state_key] = payload
    save_retry_state(path, state)


def clear_pending_retry(path: Path, state_key: str) -> None:
    state = load_retry_state(path)
    if state_key in state:
        state.pop(state_key, None)
        save_retry_state(path, state)
