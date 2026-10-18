"""Pure parsing and decisions; no network, sessions, or game writes."""
from __future__ import annotations

import copy
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import PLAYER_ID

TZ = ZoneInfo("Asia/Shanghai")
STARS = ("紫微", "天府", "太阴", "贪狼")
COMMANDS = {"rift": ".探寻裂缝", "node": ".搜寻节点"}
RIFT_SECONDS = 12 * 3600
EFFECT_MARGIN = 10


class ExplorationError(RuntimeError):
    def __init__(self, code, *, uncertain=False):
        self.code, self.uncertain = str(code), bool(uncertain)
        super().__init__(self.code)


def day(ts):
    return datetime.fromtimestamp(float(ts), TZ).date().isoformat()


def midnight(ts):
    return (datetime.fromtimestamp(float(ts), TZ) + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()


def plain(value):
    return str(value or "").replace("**", "").replace("`", "").strip()


def duration(text):
    parts = re.findall(r"(\d+(?:\.\d+)?)\s*(天|小时|分鐘|分钟|分|秒)", plain(text))
    if not parts:
        return None
    scales = {"天": 86400, "小时": 3600, "分鐘": 60, "分钟": 60, "分": 60, "秒": 1}
    return sum(float(n) * scales[unit] for n, unit in parts)


def empty_effect():
    return {"known": True, "route": None, "expires_at": 0.0}


def parse_effect(text, now):
    value = plain(text)
    if value == "无":
        return empty_effect()
    match = re.fullmatch(r"(闭关|炼制|探索|斗法)[（(]剩余\s*(.+?)[）)]", value)
    seconds = duration(match[2]) if match else None
    if not match or seconds is None:
        raise ExplorationError("fate_effect_unrecognized")
    return {"known": True, "route": match[1], "expires_at": float(now) + seconds}


def parse_board(text, now):
    text = plain(text)
    if "【天机盘】" not in text:
        raise ExplorationError("fate_board_missing")
    fields = {}
    for line in text.splitlines():
        if re.search(r"[:：]", line):
            key, value = re.split(r"[:：]", line, maxsplit=1)
            fields[key.strip()] = value.strip()
    try:
        available = [s for s in re.findall(r"【([^】]+)】", fields["今日可选命星"]) if s in STARS]
        selected = fields["今日已定命星"].strip("【】")
        counters = [int(v.strip()) for v in fields["命中 / 落空 / 改命"].split("/")]
        points, debt = int(fields["天机值"]), int(fields["逆命劫"])
        if not available or len(counters) != 3 or min(points, debt, *counters) < 0:
            raise ValueError
        if selected not in (*STARS, "未定命"):
            raise ValueError
        return {"available": available, "star": None if selected == "未定命" else selected,
                "prediction": parse_effect(fields["当前推命"], now),
                "protection": parse_effect(fields["当前改命"], now),
                "points": points, "debt": debt, "hits": counters[0], "misses": counters[1],
                "rescues": counters[2], "read_at": float(now), "source": "server_board"}
    except (KeyError, TypeError, ValueError) as exc:
        raise ExplorationError("fate_board_unrecognized") from exc


def choose_star(kind, available):
    choices = ("太阴", "贪狼") if kind == "journey" else ("贪狼", "太阴")
    return next((star for star in choices if star in available), None)


def active(board, field, now):
    effect = board.get(field) or {}
    return (effect.get("known") is True and effect.get("route") == "探索"
            and float(effect.get("expires_at") or 0) > float(now) + EFFECT_MARGIN)


def required_preparation(board, kind, now):
    star = choose_star(kind, board.get("available", []))
    if star is None:
        raise ExplorationError("no_allowed_fate_star")
    commands = []
    if board.get("star") != star:
        commands.append(f".定命 {star}")
    for field, command in (("prediction", ".推命 探索"), ("protection", ".改命 探索")):
        effect = board.get(field) or {}
        if effect.get("known") is not True:
            raise ExplorationError("fate_state_unknown")
        if effect.get("route") not in {None, "探索"} and float(effect.get("expires_at") or 0) > now:
            raise ExplorationError("another_fate_route_occupied")
        if not active(board, field, now):
            if field == "protection" and board.get("points", -1) < 3:
                raise ExplorationError("insufficient_tianji")
            commands.append(command)
    return commands


def accept_preparation(board, command, text, now):
    """Only the corresponding official success wording advances cached effects."""
    out, text = copy.deepcopy(board), plain(text)
    if command == ".观命":
        stars = [s for s in re.findall(r"【([^】]+)】", text) if s in STARS]
        if not stars or not any(word in text for word in ("显化", "命星", "命数")):
            raise ExplorationError("observation_not_confirmed", uncertain=True)
        out["available"] = list(dict.fromkeys(stars))
    elif command.startswith(".定命 "):
        star = command.split()[1]
        if not re.search(r"你将今日命轨定在\s*【" + re.escape(star) + r"】", text):
            raise ExplorationError("star_not_confirmed", uncertain=True)
        out["star"] = star
    elif command == ".推命 探索":
        if "为 【探索】 推下一段命数" not in text or "8 小时" not in text:
            raise ExplorationError("prediction_not_confirmed", uncertain=True)
        out["prediction"] = {"known": True, "route": "探索", "expires_at": now + 8 * 3600}
    elif command == ".改命 探索":
        if ("你以 3 点天机值" not in text or "为 【探索】 预留了一次" not in text
                or "24 小时" not in text):
            raise ExplorationError("protection_not_confirmed", uncertain=True)
        out["protection"] = {"known": True, "route": "探索", "expires_at": now + 24 * 3600}
        out["points"] -= 3
    else:
        raise ExplorationError("preparation_not_allowed")
    out["source"] = "confirmed_receipts"
    return out


def action_text(reply):
    result = reply.get("actionResult") or {}
    return plain(result.get("rawMessage") or result.get("message") or "")


def parse_wild(reply, today):
    account = reply.get("account") or {}
    if type(account.get("playerId")) is not int or account["playerId"] != PLAYER_ID:
        raise ExplorationError("journey_identity_mismatch")
    wild = (account.get("journey") or {}).get("wildExperience")
    if not isinstance(wild, dict):
        raise ExplorationError("journey_state_missing")
    if wild.get("dailyDate") != today:
        raise ExplorationError("awaiting_server_daily_reset")
    count, limit = wild.get("dailyCount"), wild.get("dailyLimit")
    if type(count) is not int or type(limit) is not int or count < 0 or limit <= 0 or count > limit:
        raise ExplorationError("journey_counter_invalid")
    available = wild.get("available")
    if type(available) is not bool:
        raise ExplorationError("journey_availability_missing")
    return {"day": today, "count": count, "limit": min(8, limit), "available": available and count < min(8, limit),
            "last_at": float(wild.get("lastAt") or 0) / 1000,
            "ready_at": float(wild.get("readyAt") or 0) / 1000,
            "remaining_seconds": float(wild.get("remainingSeconds") or 0),
            "deep_available": any(m.get("key") == "deep" for m in wild.get("modes", []) if isinstance(m, dict))}


def cooldown(text):
    match = re.search(r"(?:请在|請在|还需等待|还需|冷却剩余)\s*(.+?)\s*(?:后|後|再)", plain(text))
    return duration(match[1]) if match else None


def fate_result(text, *, prediction_expected=False, protected=None, normal_complete=False):
    """A triggered rescue wins even when the same receipt also says 待发."""
    text = plain(text)
    triggered = "【改命回天】" in text or protected is True
    hit = "【推命命中】" in text
    retained = "【改命待发】" in text and not triggered
    return {"prediction_consumed": True if hit or prediction_expected else None,
            "protection_consumed": True if triggered else False if retained or (protected is False and normal_complete) else None}


def group_result(kind, text):
    text = plain(text)
    wait = cooldown(text)
    if wait is not None:
        return {"status": "cooldown", "remaining_seconds": wait, "confirmed": False}
    if kind == "rift":
        rescued = "【改命回天】" in text and any(s in text for s in ("卷回", "退回", "未损修为"))
        success = "【探寻成功】" in text and any(s in text for s in ("带来了", "满载而归", "获得"))
        failed = any(s in text for s in ("【探寻失败】", "【探寻失利】"))
        if rescued or success or failed:
            return {"status": "rescued" if rescued else "success" if success else "settled_failure",
                    "confirmed": True, "fate": fate_result(text),
                    "star": next((s for s in STARS if f"命盘【{s}】" in text), None),
                    "cultivation_delta": 0 if "本次未损修为" in text else None}
    elif kind == "node":
        if "【虚空漫游】" in text and any(s in text for s in ("不虚此行", "虚空尘埃", "一无所获")):
            return {"status": "settled", "confirmed": True, "fate": fate_result(text)}
    if any(s in text for s in ("尚未凝聚元婴", "境界不足", "尚未开启", "无法使用", "没有资格")):
        return {"status": "unavailable", "confirmed": False}
    return {"status": "unconfirmed", "confirmed": False}


def journey_result(reply, before, today):
    state = parse_wild(reply, today)
    result = reply.get("actionResult") or {}
    if reply.get("ok") is not True or result.get("ok") is False or result.get("completed") is not True:
        raise ExplorationError("journey_not_confirmed", uncertain=True)
    if result.get("type") != "wild_experience" or result.get("handled") is False:
        raise ExplorationError("journey_result_type_mismatch", uncertain=True)
    mode = result.get("mode")
    mode = mode.get("key") if isinstance(mode, dict) else mode
    if mode != "deep":
        raise ExplorationError("journey_mode_unconfirmed", uncertain=True)
    if state["count"] != before + 1 or ("dailyCount" in result and result["dailyCount"] != before + 1):
        raise ExplorationError("journey_counter_not_single_step", uncertain=True)
    protected = result.get("fateProtected") if type(result.get("fateProtected")) is bool else None
    text = action_text(reply)
    loot = [{"name": str(item.get("name") or item.get("itemId") or "")[:60], "quantity": item.get("quantity")}
            for item in result.get("loot", []) if isinstance(item, dict)]
    return {"status": str(result.get("outcome") or "settled"), "confirmed": True, "state": state,
            "mode": "deep", "fate": fate_result(text, prediction_expected=True, protected=protected, normal_complete=True),
            "cultivation_delta": result.get("cultivationDelta"), "loot": loot,
            "summary": plain(result.get("title") or result.get("message"))[:600]}


def apply_fate(board, result):
    out = copy.deepcopy(board)
    fate = result.get("fate") or {}
    for field, key, counter in (("prediction", "prediction_consumed", "hits"),
                                ("protection", "protection_consumed", "rescues")):
        used = fate.get(key)
        if used is True:
            out[field] = empty_effect()
            out[counter] += 1
            if field == "prediction":
                out["points"] += 1
        elif used is None:
            out[field]["known"] = False
    out["source"] = "confirmed_receipts"
    return out


def catalog_commands(reply):
    commands = set()
    for entry in ((reply.get("account") or {}).get("commandCenter") or {}).get("entries", []):
        if isinstance(entry, dict) and entry.get("status") == "integrated" and entry.get("targetTab") == "command":
            commands.update(c.split()[0] for c in entry.get("commands", []) if isinstance(c, str))
    return commands
