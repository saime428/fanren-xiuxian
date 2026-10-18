"""Business reply classification for fixed event commands.

The caller must verify official sender, account and exact command/event chain.
Unknown text is retained for review; it is never promoted to success.
"""
from __future__ import annotations

import re
from reply_parsing import parse_cooldown_retry_delay_seconds

FIXED_EVENT_FLOWS = frozenset({"jiyin_choice", "shenji_budao"})
COOLDOWN_MARKERS = ("冷却", "冷卻", "请在", "請在", "后再", "後再", "需再等待")
REJECTION_MARKERS = ("没有待处理", "沒有待處理", "没有可", "沒有可", "已经结束", "已經結束",
                     "已失效", "已经过期", "已經過期", "不足", "无法", "無法", "不能", "失败", "失敗")


def fixed_event_receipt(text, flow, command=""):
    text = " ".join(str(text or "").split())
    if not text:
        return None
    if flow == "jiyin_choice":
        if "长时间未做抉择" in text or "長時間未做抉擇" in text:
            return {"status": "expired", "command": ""}
        if "回复本消息" in text or "回覆本訊息" in text or "魔君降临】" in text:
            return None
        if ("极阴的欣赏" in text or "極陰的欣賞" in text) and any(
                word in text for word in ("赏赐", "賞賜", "获得", "獲得", "赐予", "賜予", "修为", "修為")):
            return {"status": "success", "command": ".献上魂魄"}
        if any(word in text for word in ("收敛", "收斂", "隐匿", "隱匿", "屏息")) and any(
                word in text for word in ("避开", "避開", "躲过", "躲過", "瞒过", "瞞過", "未发现", "未發現", "安然", "安全")):
            return {"status": "success", "command": ".收敛气息"}
    if flow == "shenji_budao":
        if "指令:" in text or "指令：" in text or "小世界·天降浩劫" in text:
            return None
        # Official direct reply 12304868, observed after command 12304866.
        if (any(title in text for title in ("【神音浩荡】", "【神音浩蕩】"))
                and re.search(r"信仰提升至\s*\d+", text)
                and re.search(r"[稳穩]定提升至\s*\d+", text)):
            return {"status": "success", "command": ".神迹 布道"}
        if (any(word in text for word in ("布道", "神谕", "神諭")) and "信仰" in text
                and (re.search(r"(?:增加|提升|提高|恢复|恢復|\+)\s*\d+", text)
                     or "布道成功" in text)):
            return {"status": "success", "command": ".神迹 布道"}
    delay = parse_cooldown_retry_delay_seconds(text, COOLDOWN_MARKERS)
    if delay is not None:
        return {"status": "cooldown", "command": command, "retry_seconds": delay}
    if any(marker in text for marker in REJECTION_MARKERS):
        return {"status": "rejected", "command": command}
    return None
