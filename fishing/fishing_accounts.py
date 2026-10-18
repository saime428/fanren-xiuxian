"""Account env and the Telethon client factory of the fishing and dwelling MiniApp flows."""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import transport  # noqa: E402 - the root transport.py
from account_lifecycle_telegram import LifecycleTelegramClient as TelegramClient  # noqa: E402

CONFIG_ROOT = PROJECT_ROOT / "config" / "accounts"
# Shell flows source this optional private file through
# scripts/lib/account_flow_paths.sh; launchd starts the MiniApp flows' python
# directly, so the factory reads it itself.
MTPROTO_ENV = CONFIG_ROOT / "mtproto.env"
# The fleet-wide handshake gate. Fishing runs with cwd fishing/, where
# transport's cwd-relative default would be a separate, private gate.
CONNECTION_GATE = PROJECT_ROOT / "state" / "telegram_connection_gate.lock"


def _read_exports(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("export ") and "=" in line:
            key, value = line[len("export "):].split("=", 1)
            values[key.strip()] = value.strip().strip("'\"")
    return values


def read_account_env(source: str) -> dict[str, str]:
    return _read_exports(CONFIG_ROOT / f"{source}.env")


def load_transport_env() -> None:
    """Give transport the same MTProxy settings and gate as the shell flows; an explicit env wins."""
    if MTPROTO_ENV.is_file():
        for key, value in _read_exports(MTPROTO_ENV).items():
            os.environ.setdefault(key, value)
    os.environ.setdefault("TG_TELEGRAM_CONNECTION_GATE_PATH", str(CONNECTION_GATE))


STALE_SESSION_COPY_SECONDS = 3600


def sweep_stale_session_copies(root: Path, *, now: float | None = None) -> None:
    """Delete per-action session copy directories a killed run left behind.

    Each copy holds the account's auth key. A live copy's sqlite file and
    journal keep being written; one untouched for an hour belongs to no run.
    """
    # ponytail: mtime heuristic; a bridge held open >1h without a session
    # write loses its copy's directory entry (its open sqlite fd keeps working).
    cutoff = (time.time() if now is None else float(now)) - STALE_SESSION_COPY_SECONDS
    for directory in Path(root).iterdir() if Path(root).is_dir() else ():
        try:
            newest = max([directory.stat().st_mtime, *(item.stat().st_mtime for item in directory.iterdir())])
        except OSError:  # not a directory, or removed by its own run meanwhile
            continue
        if newest < cutoff:
            shutil.rmtree(directory, ignore_errors=True)


def telegram_client(session: str, api_id: int, api_hash: str, **options: Any) -> TelegramClient:
    """transport.telegram_client: MTProxy is mandatory once enabled (no silent direct
    fallback; a bad secret raises), and Telethon's auto-reconnect and FloodWait sleeps are off."""
    load_transport_env()
    return transport.telegram_client(session, api_id, api_hash, **options)
