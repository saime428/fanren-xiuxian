#!/usr/bin/env python3
"""Account-scoped daily journal for the 宗门日常 flow."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from miniapp_shared import daily
from miniapp_shared.accounts import accounts_for
from miniapp_shared.state import read_json, write_json

FLOW = "sect_daily"
ALLOWED_ACCOUNTS = accounts_for("sect_daily")


def account_dir(root: Path, account: str) -> Path:
    return daily.account_dir(root, account, FLOW)


def account_lock(root: Path, account: str):
    return daily.account_lock(root, account, FLOW)


def read_state(root: Path, account: str) -> dict[str, Any]:
    data = read_json(account_dir(root, account) / "state.json")
    return {} if data.get("status") == "malformed_state" else data


def write_state(root: Path, account: str, data: dict[str, Any]) -> Path:
    return write_json(account_dir(root, account) / "state.json", data)
