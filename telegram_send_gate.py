from __future__ import annotations

import asyncio
import fcntl
import os
import time
from contextlib import suppress
from pathlib import Path
from typing import Awaitable, Callable, TypeVar

from state_store import load_json_dict, persist_json_atomic

T = TypeVar("T")

# Callers that do not export TG_GLOBAL_SEND_GATE_STATE_PATH (plists that run
# Python directly) must still share one file, never a per-flow "gate".
PROJECT_GATE_PATH = Path(__file__).resolve().parent / "state" / "global_telegram_send_gate.json"


def resolve_gate_path(state_dir: Path) -> Path:
    # The cross-account gate must live at one shared path; per-flow state dirs
    # would silently give every flow its own useless gate.
    env_path = os.getenv("TG_GLOBAL_SEND_GATE_STATE_PATH", "").strip()
    if env_path:
        return Path(env_path)
    return PROJECT_GATE_PATH


def _claim_send_slot(gate_path: Path, min_interval_seconds: float, label: str) -> float:
    """Atomically check the gate and claim a send slot.

    Returns 0.0 when the slot was claimed, otherwise the seconds to wait.
    """
    gate_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = gate_path.with_name(gate_path.name + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            now = time.time()
            state = load_json_dict(gate_path)
            last_ts = float(state.get("last_send_ts", 0) or 0)
            wait_seconds = max(0.0, float(min_interval_seconds) - max(0.0, now - last_ts))
            if wait_seconds <= 0:
                persist_json_atomic(gate_path, {"last_send_ts": now, "label": label})
                return 0.0
            return wait_seconds
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def _record_send_error(gate_path: Path, label: str) -> None:
    lock_path = gate_path.with_name(gate_path.name + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            current = load_json_dict(gate_path)
            if current.get("label") == label:
                current["last_error_ts"] = time.time()
                persist_json_atomic(gate_path, current)
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


async def run_with_global_send_gate(
    action: Callable[[], Awaitable[T]],
    *,
    label: str,
    min_interval_seconds: float,
    state_dir: Path,
    log: Callable[..., None],
) -> T:
    gate_path = resolve_gate_path(state_dir)
    while True:
        wait_seconds = await asyncio.to_thread(_claim_send_slot, gate_path, min_interval_seconds, label)
        if wait_seconds <= 0:
            break
        log(f"Global send gate waiting {wait_seconds:.1f}s for {label}")
        await asyncio.sleep(min(wait_seconds, 5.0))
    try:
        return await action()
    except Exception:
        with suppress(Exception):
            await asyncio.to_thread(_record_send_error, gate_path, label)
        raise
