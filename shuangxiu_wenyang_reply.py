"""Shared, offline-only identity and receipt rules for 温养双修."""
from __future__ import annotations

import re

from miniapp_shared.accounts import ACCOUNT_USERNAMES, accounts_for, feature_options

# The one account enabling shuangxiu_wenyang initiates; its "partner" option answers.
INITIATOR_ACCOUNT = next(iter(accounts_for("shuangxiu_wenyang")), None)
INITIATOR_USERNAME = ACCOUNT_USERNAMES.get(INITIATOR_ACCOUNT, "")
PARTNER_ACCOUNT = feature_options(INITIATOR_ACCOUNT, "shuangxiu_wenyang").get("partner")
PARTNER_USERNAME = ACCOUNT_USERNAMES.get(PARTNER_ACCOUNT, "")
TRUSTED_REPLY_SENDER_IDS = frozenset({7228205882, 7965897083, 8735907987, 8816935632,
    8918867302, 8944702077, 8964348409, 8980154525, 8788895566, 8917921351, 8388633812, 8861328042})


def classify_partner_reply(text: str) -> str:
    normalized = " ".join((text or "").split())
    lower = normalized.lower()
    has_partner = re.search(r"(?<![\w@])@" + PARTNER_USERNAME + r"(?![A-Za-z0-9_])", lower) is not None
    has_initiator = re.search(r"(?<![\w@])@" + INITIATOR_USERNAME + r"(?![A-Za-z0-9_])", lower) is not None
    if "温养双修" in normalized and has_partner and has_initiator and any(
        marker in normalized for marker in ("大成", "修为增加", "灵力完美交融")
    ):
        return "success"
    if "契印感应" in normalized and "温养双修" in normalized:
        return "pending"
    if has_partner and "无法进行双修" in normalized and any(
        marker in normalized for marker in ("冷却", "心神尚未恢复")
    ):
        return "cooldown"
    return "unknown"
