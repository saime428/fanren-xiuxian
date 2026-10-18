"""The reviewed official dwelling routes and existing business-account scope."""
from __future__ import annotations

import json
from pathlib import Path

from miniapp_shared.accounts import accounts_for

VERSION = "miniapp-commands-v1"
ORIGIN = "https://asc.aiopenai.app"
PREFIX = "/api/miniapp/xianxia-dwelling/"
STATE_FILE = "miniapp_state.json"
RECEIPT_FILE = "miniapp_last_result.json"
FLOW_COMMANDS = {
    "shendubiguan": ".深度闭关",
    "yuanyingchuqiao": ".元婴出窍",
    "wendao": ".问道",
    "xuexishanlin": ".血洗山林",
    "zhaohuanmoying": ".召唤魔影",
    "sect_checkin": ".宗门点卯",
    "shenji_budao": ".神迹 布道",
}
# The accounts enabling each flow's feature (点卯 belongs to 宗门日常).
ACCOUNTS = {flow: accounts_for("sect_daily" if flow == "sect_checkin" else flow) for flow in FLOW_COMMANDS}
PERIODS = {"shendubiguan": 28800, "yuanyingchuqiao": 28800, "wendao": 43200,
           "xuexishanlin": 14400, "zhaohuanmoying": 28800, "sect_checkin": 86400,
           "shenji_budao": 3600}
READ_COMMANDS = frozenset({".元婴状态", ".我的阴罗幡"})
WRITE_COMMANDS = frozenset(FLOW_COMMANDS[f] for f in (
    "yuanyingchuqiao", "wendao", "xuexishanlin", "zhaohuanmoying", "sect_checkin")) | {".元婴归窍"}


def migrated(root: Path, account: str, flow: str) -> bool:
    """Activation is explicit; a disabled/removed flow can never be resurrected."""
    if account not in ACCOUNTS.get(flow, ()):
        return False
    from flow_registry import registry_path  # local: flow_registry imports this catalog lazily
    path = registry_path(root)
    try:
        payload = json.loads(path.read_text())
    except FileNotFoundError:
        return False
    # Invalid configuration must raise, never silently fall back to group sends.
    if not isinstance(payload, dict):
        raise ValueError("miniapp_registry_invalid")
    if flow == "sect_checkin":
        return any(row.get("label") == "ai.hermes.xiuxian.sect-daily"
                   and row.get("enabled", True) is True
                   and ".宗门点卯" in row.get("miniapp_commands", [])
                   for row in payload.get("helpers", []))
    return any(row.get("account") == account and row.get("flow") == flow
               and row.get("enabled", True) is True and row.get("transport") == "miniapp"
               for row in payload.get("flows", []))


def state_dir(root: Path, account: str, flow: str) -> Path:
    if account not in ACCOUNTS.get(flow, ()):
        raise ValueError("miniapp_account_flow_not_allowed")
    return Path(root) / "state/accounts" / account / ("sect_daily" if flow == "sect_checkin" else flow)
