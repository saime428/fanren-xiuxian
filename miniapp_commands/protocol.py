"""Server-result interpretation shared by scheduled, daily and event callers."""
from __future__ import annotations

import math
import re

from miniapp_shared.dwelling import MiniAppError
from reply_parsing import parse_cooldown_retry_delay_seconds, parse_duration_seconds
from .client import result_text


def number(value):
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
        return n if math.isfinite(n) and n >= 0 else None
    except (TypeError, ValueError):
        return None


def plain(value):
    return str(value or "").replace("**", "").replace("`", "")


def deep_board(reply):
    board = ((reply.get("dwelling") or {}).get("meditation") or {}).get("deepSeclusion")
    if (not isinstance(board, dict) or type(board.get("active")) is not bool
            or type(board.get("canStart")) is not bool or type(board.get("completed")) is not bool):
        raise MiniAppError("miniapp_deep_state_invalid")
    remaining = number(board.get("remainingSeconds"))
    if board["active"] and remaining is None:
        raise MiniAppError("miniapp_deep_timer_missing")
    return {**{k: board.get(k) for k in ("active", "completed", "canStart", "canSettle",
                                       "startMs", "endMs", "durationHours")}, "remainingSeconds": remaining}


def yuan_board(reply):
    text = plain(result_text(reply))
    if (reply.get("actionResult") or {}).get("ok") is not True:
        raise MiniAppError("miniapp_yuan_status_rejected")
    if any(s in text for s in ("尚未凝聚元婴", "尚未凝聚元嬰", "无法施展", "無法施展")):
        return {"status": "unavailable", "remainingSeconds": None}
    match = re.search(r"(?:状态|狀態)\s*[:：]\s*([^\n]+)", text)
    status = match.group(1).strip() if match else ""
    countdown = re.search(r"(?:归来倒计时|歸來倒計時|剩余|剩餘)\s*[:：]?\s*([^\n]+)", text)
    remaining = parse_duration_seconds(countdown.group(1)) if countdown else None
    if any(s in status for s in ("元婴闭关", "元嬰閉關")):
        return {"status": "manual_retreat", "remainingSeconds": remaining}
    if any(s in status for s in ("元神出窍", "元神出竅", "出窍", "出竅")):
        return {"status": "return_due" if remaining == 0 else "active", "remainingSeconds": remaining}
    if any(s in status for s in ("空闲", "空閒", "闲置", "閒置", "窍中温养", "竅中溫養", "已归窍", "已歸竅")):
        return {"status": "idle", "remainingSeconds": 0}
    if any(s in text for s in ("当前没有进行中的元婴任务", "當前沒有進行中的元嬰任務")):
        return {"status": "idle", "remainingSeconds": 0}
    raise MiniAppError("miniapp_yuan_state_unrecognized")


def outcome(flow, reply, *, operation=""):
    """HTTP success/handled alone is never game success."""
    text = plain(result_text(reply))
    action = reply["actionResult"]
    if type(action.get("ok")) is not bool:
        raise MiniAppError("miniapp_action_status_missing", uncertain=True)
    cooldown = None
    if re.search(r"请在|請在|冷却|冷卻|后再|後再|尚未平复|尚未平復|调息|調息", text):
        cooldown = parse_cooldown_retry_delay_seconds(text, ["冷却", "冷卻", "请在", "請在", "尚未平复", "尚未平復"])
    if cooldown is not None and cooldown > 0:
        return {"status": "cooldown", "succeeded": False, "remainingSeconds": cooldown}
    if flow == "sect_checkin":
        from sect_daily.protocol import parse_checkin
        result = parse_checkin(text)
        if result["status"] != "unknown":
            return {**result, "succeeded": result["status"] == "checked_in"}
    if flow == "yuanyingchuqiao":
        if operation == "return" and action["ok"]:
            return {"status": "return_requested", "succeeded": False}
        if any(s in text for s in ("你心念一动", "你心念一動", "元婴化作一道流光飞出", "元嬰化作一道流光飛出")) and action["ok"]:
            return {"status": "started", "succeeded": True}
        if any(s in text for s in ("你的元婴正在执行", "你的元嬰正在執行")):
            return {"status": "active", "succeeded": False}
    if flow == "shendubiguan":
        board = deep_board(reply)
        if operation == "start" and board["active"]:
            return {"status": "started" if action["ok"] and "已进入" in text else "active",
                    "succeeded": action["ok"] and "已进入" in text,
                    "remainingSeconds": board["remainingSeconds"]}
        if operation == "settle" and action["ok"] and not board["active"] and any(
                s in text for s in ("闭关总结", "閉關總結", "出关", "出關", "结算", "結算")):
            return {"status": "settled", "succeeded": True}
    success = {
        "wendao": ("【问道得宝】", "【問道得寶】", "问道成功", "問道成功"),
        "xuexishanlin": ("【血洗功成】",),
        "zhaohuanmoying": ("召唤成功，镇压成功", "召喚成功，鎮壓成功", "魔影被你成功击溃", "魔影被你成功擊潰"),
    }
    if action["ok"] and any(s in text for s in success.get(flow, ())):
        return {"status": "success", "succeeded": True,
                "cooldown_reset": any(s in text for s in ("冷却时间被重置", "冷卻時間被重置", "毫无疲惫之感"))}
    if flow == "shenji_budao":
        from event_command_receipts import fixed_event_receipt
        parsed = fixed_event_receipt(text, flow, ".神迹 布道")
        if parsed and parsed["status"] == "success" and action["ok"]:
            return {"status": "success", "succeeded": True}
    if flow == "zhaohuanmoying" and any(s in text for s in (
            "召唤成功，镇压失败", "召喚成功，鎮壓失敗", "魔影过于强大", "魔影過於強大", "修为暴跌", "修為暴跌")):
        return {"status": "settled_failure", "succeeded": False}
    if not action["ok"]:
        return {"status": "rejected", "succeeded": False}
    raise MiniAppError("miniapp_outcome_unclassified", uncertain=True)
