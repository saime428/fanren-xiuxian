#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import stat
import sys
from contextlib import closing, suppress
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from telethon.errors import UnauthorizedError
from telethon.tl.functions.updates import GetStateRequest


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
WORKSPACE_ROOT = PROJECT_ROOT.parent
for candidate in (PROJECT_ROOT, WORKSPACE_ROOT):
    candidate_str = str(candidate)
    if candidate_str not in sys.path:
        sys.path.insert(0, candidate_str)

from transport import _connect_with_gate, disconnect_telegram_client, telegram_client


def env(name: str, default: str = "") -> str:
    value = os.getenv(name, default).strip()
    if not value:
        raise SystemExit(f"Missing required env var: {name}")
    return value


def optional_env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def session_sqlite_path(session_name: str) -> Path:
    path = Path(session_name)
    if path.suffix == ".session":
        return path
    return path.with_suffix(".session")


async def probe_authorization(
    session_name: str,
    api_id: int,
    api_hash: str,
    *,
    timeout_seconds: float,
) -> tuple[str, bool]:
    client = telegram_client(session_name, api_id, api_hash)
    try:
        await _connect_with_gate(client, timeout_seconds)
        # Telethon's is_user_authorized() turns every RPCError (including flood
        # waits and server failures) into False. Only an explicit authorization
        # rejection is evidence that permits replacing an existing session.
        await asyncio.wait_for(client(GetStateRequest()), timeout=timeout_seconds)
        return "ok", True
    except UnauthorizedError:
        return "ok", False
    except Exception:
        return "probe_failed", False
    finally:
        with suppress(Exception):
            await disconnect_telegram_client(client, timeout_seconds=min(5.0, timeout_seconds))


def copy_sqlite_snapshot(source: Path, destination: Path) -> None:
    """Copy a session database another process may have open.

    A byte copy can catch a half-written page set and misses rows still in a
    WAL file; SQLite's backup API reads one consistent snapshot.
    """
    destination.unlink(missing_ok=True)
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src, \
            closing(sqlite3.connect(destination)) as dst:
        src.backup(dst)


def remove_sqlite_sidecars(sqlite_path: Path) -> None:
    for suffix in ("-shm", "-wal", "-journal"):
        sidecar = Path(f"{sqlite_path}{suffix}")
        if sidecar.exists():
            sidecar.unlink()


async def main() -> None:
    load_dotenv()

    api_id = int(env("TG_API_ID"))
    api_hash = env("TG_API_HASH")
    session_name = env("TG_SESSION_NAME")
    fallback_name = optional_env("TG_SESSION_FALLBACK_NAME", "")
    auth_probe_timeout_seconds = max(5.0, float(optional_env("TG_SESSION_AUTH_PROBE_TIMEOUT_SECONDS", "25")))

    session_path = Path(session_name)
    session_path.parent.mkdir(parents=True, exist_ok=True)
    target_sqlite = session_sqlite_path(session_name)
    target_existed = target_sqlite.is_file()

    target_probe_status, target_authorized = await probe_authorization(
        session_name,
        api_id,
        api_hash,
        timeout_seconds=auth_probe_timeout_seconds,
    )
    if target_authorized:
        return
    if target_probe_status == "probe_failed":
        if target_existed:
            print(
                "Authorization probe could not establish session status; "
                f"preserve existing session and continue without reseeding: {session_name}"
            )
            return
        print(f"Authorization probe unavailable; retry without reseeding: {session_name}")
        raise SystemExit(75)

    if not fallback_name:
        raise SystemExit(f"Target session is not authorized and no fallback is configured: {session_name}")

    if fallback_name == session_name:
        raise SystemExit(f"Fallback session matches target session but is not authorized: {session_name}")

    fallback_probe_status, fallback_authorized = await probe_authorization(
        fallback_name,
        api_id,
        api_hash,
        timeout_seconds=auth_probe_timeout_seconds,
    )
    if not fallback_authorized:
        if fallback_probe_status == "probe_failed":
            print(
                "Fallback authorization probe unavailable; "
                f"preserve target and retry without reseeding: {session_name}"
            )
            raise SystemExit(75)
        raise SystemExit(
            f"Target session is not authorized and fallback session is also unusable: "
            f"target={session_name} fallback={fallback_name}"
        )

    fallback_sqlite = session_sqlite_path(fallback_name)
    if not fallback_sqlite.exists():
        raise SystemExit(f"Fallback sqlite does not exist: {fallback_sqlite}")

    if target_sqlite.exists():
        backup_suffix = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = target_sqlite.with_name(f"{target_sqlite.name}.before-reseed-{backup_suffix}")
        shutil.copy2(target_sqlite, backup_path)
        print(f"Backed up unauthorized session to: {backup_path}")

    remove_sqlite_sidecars(target_sqlite)
    copy_sqlite_snapshot(fallback_sqlite, target_sqlite)
    target_sqlite.chmod(stat.S_IRUSR | stat.S_IWUSR)
    print(f"Reseeded session from fallback: {fallback_sqlite} -> {target_sqlite}")

    final_probe_status, final_authorized = await probe_authorization(
        session_name,
        api_id,
        api_hash,
        timeout_seconds=auth_probe_timeout_seconds,
    )
    if final_authorized:
        print(f"Session authorized after reseed: {session_name}")
        return
    if final_probe_status == "probe_failed":
        print(
            "Reseed completed; final authorization probe unavailable, "
            f"continuing with session sqlite in place: {session_name}"
        )
        return
    raise SystemExit(f"Reseed completed but target session is still not authorized: {session_name}")


if __name__ == "__main__":
    asyncio.run(main())
