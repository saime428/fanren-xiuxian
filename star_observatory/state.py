#!/usr/bin/env python3
"""Account-scoped persistent state for the 星宫灵圃 flow."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from miniapp_shared import daily
from miniapp_shared.accounts import accounts_for
from miniapp_shared.state import read_json, write_json

FLOW = "star_observatory"
ALLOWED_ACCOUNTS = accounts_for("star_observatory")
# Which dwelling identity drives the 星宫灵圃: the shared bound-identity scope
# (miniapp_shared.daily.BOUND_IDENTITY); other accounts use their personal identity.
IDENTITY_SCOPE = daily.BOUND_IDENTITY


def scoped_identity(account: str, dwelling: Any) -> Any:
    """Return the identity the flow may act on for ``account`` (raises MiniAppError)."""
    from miniapp_shared.dwelling import MiniAppError  # local import: state must stay import-light

    if account not in IDENTITY_SCOPE:
        identity = dwelling.personal_identity()
        if int(identity.player_id) != dwelling.account_id:
            raise MiniAppError("identity_mismatch")
        return identity
    matches = daily.scoped_identities(account, dwelling.identities)
    if not matches:
        raise MiniAppError("scoped_identity_missing")
    return matches[0]


def identity_label(account: str) -> str:
    scope = IDENTITY_SCOPE.get(account)
    return str(scope["label"]) if scope else ""


def account_state_dir(root: Path, account: str) -> Path:
    return daily.account_dir(root, account, FLOW, ALLOWED_ACCOUNTS)


def account_lock(root: Path, account: str):
    return daily.account_lock(root, account, FLOW, ALLOWED_ACCOUNTS)


def read_account_state(root: Path, account: str) -> dict[str, Any]:
    return read_json(account_state_dir(root, account) / "state.json")


def write_account_state(root: Path, account: str, data: dict[str, Any]) -> Path:
    return write_json(account_state_dir(root, account) / "state.json", data)
