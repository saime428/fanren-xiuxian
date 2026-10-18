"""Parse Nanlonghou choices and receipts without performing Telegram actions."""
from __future__ import annotations

import re

from miniapp_shared.accounts import accounts_for

TRADE_FLOW_COMMANDS = {
    "jiaohuan_fabao": ".交换 法宝",
    "jiaohuan_gongfa": ".交换 功法",
}
TRADE_FLOWS = frozenset(TRADE_FLOW_COMMANDS)
TRADE_COMMANDS = frozenset({*TRADE_FLOW_COMMANDS.values(), ".拒绝交易"})
TRADE_MAX_AGE_SECONDS = 600
TRADE_PENDING = frozenset({"awaiting_trade_result", "trade_result_unconfirmed"})
COMMAND_RE = re.compile(r"\.(?:交换\s+(?:法宝|功法)|拒绝交易)(?![\w])")
COMPANION_RE = re.compile(r"侍妾[【\[]([^】\]\n]+)[】\]]")
RESULT_RE = re.compile(
    r"【天机异闻\s*[·・]\s*南陇侯的交易】\s*"
    r"道友\s+@(?P<username>[A-Za-z0-9_]+)\s+经过深思熟虑，"
    r"选择将侍妾【(?P<companion>[^】\n]+)】与南陇侯交换！\s*"
    r"作为回报，南陇侯赐予了其(?P<reward_description>[^【\n]+)"
    r"【(?P<reward>[^】\n]+)】[！!。]?\s*\Z"
)


def expected_trade_flow(account: str) -> str:
    """The account's configured choice: 功法 if it enables jiaohuan_gongfa, else 法宝."""
    return "jiaohuan_gongfa" if account in accounts_for("jiaohuan_gongfa") else "jiaohuan_fabao"


def offered_trade_commands(text: str) -> set[str]:
    return {re.sub(r"\s+", " ", command) for command in COMMAND_RE.findall(text or "")}


def trade_companion(text: str) -> str | None:
    matches = COMPANION_RE.findall(text or "")
    return matches[0] if len(set(matches)) == 1 else None


def parse_trade_result(text: str) -> dict | None:
    match = RESULT_RE.fullmatch((text or "").strip())
    if not match:
        return None
    result = match.groupdict()
    description = result.pop("reward_description")
    result["command"] = (".交换 法宝" if "至宝" in description or "法宝" in description
                         else ".交换 功法" if "功法" in description else None)
    return result


def trade_message_kind(text: str) -> str | None:
    if parse_trade_result(text):
        return "trade_result"
    if (any(name in text for name in ("南陇侯", "南隴侯"))
            and re.search(r"回[复覆復]本(?:消息|訊息)", text)
            and offered_trade_commands(text) & set(TRADE_FLOW_COMMANDS.values())):
        return "question"
    if (any(name in text for name in ("南陇侯", "南隴侯"))
            and any(marker in text for marker in ("强横神念", "強橫神念"))):
        return "notice"
    return None


def validate_trade_policy(payload: dict, account: str, flow: str, command: str) -> None:
    # Keep registry validation and runtime authorization on the same schema.
    from flow_registry import active_entries

    entries = [row for row in active_entries(payload)
               if row["account"] == account and row["flow"] in TRADE_FLOWS and "send" in row["kinds"]]
    if (len(entries) != 1 or entries[0]["flow"] != flow or flow != expected_trade_flow(account)
            or TRADE_FLOW_COMMANDS.get(flow) != command):
        raise ValueError("Nanlonghou requires exactly one enabled account choice matching its command")
