#!/usr/bin/env python3
"""Account-scoped state for the 藏娇阁 loop."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from miniapp_shared import daily
from miniapp_shared.accounts import accounts_for, feature_options
from miniapp_shared.state import read_json, write_json

FLOW = "pavilion_loop"
ALLOWED_ACCOUNTS = accounts_for("pavilion")
# Per-companion route overrides from the pavilion feature's "routes" option
# (demo: @demo_player_01's 南宫婉·月影 always sails 月殿寻痕); the runner never
# substitutes another route for a companion listed here.
ROUTE_OVERRIDES: dict[str, dict[str, str]] = {
    account: {str(k): str(v) for k, v in feature_options(account, "pavilion").get("routes", {}).items()}
    for account in ALLOWED_ACCOUNTS if feature_options(account, "pavilion").get("routes")
}
# Per-companion voyage holds. On 2026-10-05 the operator resumed @demo_player_01's
# 温天心, replacing the 2026-09-22 hold. Route selection and other timers stay
# with the existing loop; future holds can still be scoped here.
NO_SAIL: dict[str, frozenset[str]] = {}


def sailing_disabled(account: str, companion_name: str) -> bool:
    return str(companion_name) in NO_SAIL.get(str(account), frozenset())


def account_dir(root: Path, account: str) -> Path:
    return daily.account_dir(root, account, FLOW)


def account_lock(root: Path, account: str):
    return daily.account_lock(root, account, FLOW)


def read_state(root: Path, account: str) -> dict[str, Any]:
    data = read_json(account_dir(root, account) / "state.json")
    return {} if data.get("status") == "malformed_state" else data


def write_state(root: Path, account: str, data: dict[str, Any]) -> Path:
    return write_json(account_dir(root, account) / "state.json", data)
