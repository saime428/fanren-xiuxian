#!/usr/bin/env python3
"""Account/identity-scoped state for the 游历 flow."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from miniapp_shared import daily
from miniapp_shared.accounts import accounts_for
from miniapp_shared.state import read_json, write_json

FLOW = "journey_miniapp"
# Every dwelling identity of every account runs the daily 谨慎 journey, within
# the shared bound-identity scope (miniapp_shared.daily.BOUND_IDENTITY).
IDENTITY_SCOPE = daily.BOUND_IDENTITY
scoped_identities = daily.scoped_identities
ALLOWED_ACCOUNTS = accounts_for("journey")


def scope_description(account: str) -> str:
    scope = IDENTITY_SCOPE.get(account)
    if not scope:
        return "all identities"
    return f"only {scope['label']} ({scope['player_id']})"


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
