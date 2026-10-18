#!/usr/bin/env python3
"""Account/identity-scoped state for the 琉璃问心塔 flow."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from miniapp_shared import daily
from miniapp_shared.accounts import accounts_for
from miniapp_shared.state import read_json, write_json

FLOW = "tower_miniapp"
ALLOWED_ACCOUNTS = accounts_for("tower")


def account_dir(root: Path, account: str) -> Path:
    return daily.account_dir(root, account, FLOW)


def account_lock(root: Path, account: str):
    return daily.account_lock(root, account, FLOW)


def read_state(root: Path, account: str) -> dict[str, Any]:
    return read_json(account_dir(root, account) / "summary.json")


def write_state(root: Path, account: str, data: dict[str, Any]) -> None:
    write_json(account_dir(root, account) / "summary.json", data)


def identity_path(root: Path, account: str, player_id: int) -> Path:
    return account_dir(root, account) / "identities" / f"{int(player_id)}.json"


def read_identity(root: Path, account: str, player_id: int) -> dict[str, Any]:
    return read_json(identity_path(root, account, player_id))


def write_identity(root: Path, account: str, player_id: int, data: dict[str, Any]) -> None:
    write_json(identity_path(root, account, player_id), data)
