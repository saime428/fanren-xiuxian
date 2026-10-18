#!/usr/bin/env python3
"""Rules of the 藏娇阁 features, learned from the MiniApp 天机阁 replies (2026-09-20/21).

Every command below is accepted by ``/api/miniapp/xianxia-dwelling/command-center``
(``{"command": ".xxx"}``) and acts on the **active** (随行中) concubine. The
resident one (居于藏娇阁) becomes active with ``.召回侍妾 <名字>`` instantly and
"情缘、神通与冷却均保持不变", so the loop runs each concubine in turn.

Per-concubine timers (server text ``.我的侍妾`` + ``dwelling.companions[].raw``):

| feature | command | cooldown | blocked while sailing | notes |
| --- | --- | --- | --- | --- |
| 入梦寻图 | ``.入梦寻图`` | 480 min | yes | one 残纹 for 虚天/苍坤 (4 each); 代卜 buff 残图引路 raises drop rate |
| 天机代卜 | ``.天机代卜`` | 720 min | yes | 180 修为; random buff (残图引路 / 心劫前兆 / 避劫星芒) |
| 坠魔心劫 | ``.坠魔心劫`` | 720 min | yes | 承劫 for the 坠魔谷 raid: 护持 buff; success 70-84% |
| 拼图 | ``.拼图`` | none | no | only meaningful at 4/4 |
| 侍妾远航 | ``.侍妾远航 <route>`` | voyage length | n/a | see ROUTES |
| 远航归来 | ``.远航归来`` | after ``end_time`` | n/a | settles 修为/灵石/materials, +情缘 |

``.共历心劫`` (3-round 稳/狠/骗 dialogue, +~800 修为 +7 情缘, 600 min) is group-only
per the MiniApp catalog ("需要回复心劫消息"); it is out of the in-app loop.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

COMMAND_CENTER = "/api/miniapp/xianxia-dwelling/command-center"

HEART_TRIAL_COOLDOWN_MIN = 600
DREAM_MAP_COOLDOWN_MIN = 480
DIVINATION_COOLDOWN_MIN = 720
ZHUIMO_COOLDOWN_MIN = 720

# route -> (hours, 修为, 灵石, note)
ROUTES = {
    "稳妥": (4, 200, 20, "low yield, no risk; no affection floor observed"),
    "均衡": (8, 360, 45, "妖丹/阴魂丝, +情缘 6-7; matches the 8 h 寻图 cooldown; needs 情缘 >= 70"),
    "冒险": (12, 600, 80, "养魂木; storms can cost 情缘 -22"),
    "月殿寻痕": (6, 420, 80, "南宫婉·月影 only: 素女禁纹"),
}
# Server: "侍妾心神未定，此航线至少需要 **70** 情缘值" for 均衡 (2026-09-21). A fresh
# companion (情缘 7-14) therefore sails 稳妥 until the floor is reached; each
# 稳妥/均衡 return adds 情缘 +5..7.
# 稳妥 itself needs 30 (observed on 红莲 at 情缘 21). Below 30 a companion cannot
# sail at all; 共历心劫 (+7) and voyages are what raise 情缘, so the loop waits.
ROUTE_AFFECTION_FLOOR = {"稳妥": 30, "均衡": 70, "冒险": 70, "月殿寻痕": 0}
MIN_SAIL_AFFECTION = 30
DEFAULT_ROUTE = "均衡"
MOON_ROUTE = "月殿寻痕"
MOON_COMPANION_PREFIX = "南宫婉"

_BOLD = re.compile(r"\*\*(.+?)\*\*")


def plain(text: Any) -> str:
    return _BOLD.sub(r"\1", str(text or "")).replace("\r", "")


def result_text(payload: dict[str, Any]) -> str:
    result = payload.get("actionResult") if isinstance(payload, dict) else None
    if isinstance(result, dict):
        text = str(result.get("rawMessage") or result.get("message") or "").strip()
        if text:
            return text
        if result.get("ok") is False:
            return str(result.get("error") or "").strip()
    return str((payload or {}).get("rawMessage") or (payload or {}).get("message") or "").strip()


def result_ok(payload: dict[str, Any]) -> bool:
    result = payload.get("actionResult") if isinstance(payload, dict) else None
    return not isinstance(result, dict) or result.get("ok") is not False


def parse_iso(value: Any) -> float:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def parse_minutes(text: str) -> int | None:
    """``11小时52分钟45秒`` / ``136分钟`` -> minutes (rounded up)."""
    body = plain(text)
    total = 0
    found = False
    for value, unit in re.findall(r"(\d+)\s*(小时|分钟|秒)", body):
        found = True
        total += int(value) * {"小时": 3600, "分钟": 60, "秒": 1}[unit]
    return (total + 59) // 60 if found else None


# ------------------------------------------------------------- companions
def companions_from_payload(payload: dict[str, Any], *, now_ts: float) -> list[dict[str, Any]]:
    """Normalize ``dwelling.companions`` into loop rows (no secrets involved)."""
    dwelling = (payload or {}).get("dwelling") or {}
    rows = []
    for item in dwelling.get("companions") or []:
        raw = item.get("raw") or {}
        voyage = raw.get("voyage") or {}
        bags = {"虚天": raw.get("xutian_fragment_bag") or {}, "苍坤": raw.get("cangkun_fragment_bag") or {}}
        rows.append({
            "name": str(item.get("name") or ""),
            "active": bool(item.get("active")),
            "status": str(item.get("status") or ""),
            "affection": raw.get("affection"),
            "sailing": voyage.get("status") == "sailing",
            "voyage_route": voyage.get("route"),
            "voyage_end_ts": parse_iso(voyage.get("end_time")) if voyage else 0.0,
            "dream_map_ready_ts": parse_iso(raw.get("last_dream_map_seek_time")) + DREAM_MAP_COOLDOWN_MIN * 60,
            "divination_ready_ts": parse_iso(raw.get("last_divination_chain_time")) + DIVINATION_COOLDOWN_MIN * 60,
            "zhuimo_ready_ts": parse_iso(raw.get("last_zhuimo_trial_time")) + ZHUIMO_COOLDOWN_MIN * 60,
            "heart_trial_ready_ts": parse_iso(raw.get("last_companion_heart_tribulation_time")) + HEART_TRIAL_COOLDOWN_MIN * 60,
            "fragments": {chart: sum(int(v or 0) > 0 for v in bag.values()) for chart, bag in bags.items()},
            "fragment_total": {chart: len(bag) for chart, bag in bags.items()},
            "puzzle_ready": any(len(bag) >= 4 and all(int(v or 0) > 0 for v in bag.values()) for bag in bags.values()),
        })
    return rows


def choose_route(companion: dict[str, Any], overrides: dict[str, str] | None = None) -> str:
    name = str(companion.get("name") or "")
    try:
        affection = int(companion.get("affection") or 0)
    except (TypeError, ValueError):
        affection = 0
    if overrides and name in overrides and overrides[name] in ROUTES:
        route = overrides[name]
    elif name.startswith(MOON_COMPANION_PREFIX):
        route = MOON_ROUTE
    else:
        route = DEFAULT_ROUTE
    if overrides and name in overrides:
        return route  # pinned by the operator: never downgraded
    if affection < ROUTE_AFFECTION_FLOOR.get(route, 0):
        return "稳妥" if affection >= MIN_SAIL_AFFECTION else ""
    return route


# ------------------------------------------------------------- reply parsing
def classify(command: str, text: str) -> dict[str, Any]:
    body = plain(text)
    base = command.split(" ")[0]
    if "远航途中" in body or "仍在远航" in body or "正在远航" in body and base != ".远航状态":
        return {"status": "sailing_blocked"}
    wait = re.search(r"请在\s*([^\n]+?)\s*后再", body)
    if wait:
        return {"status": "cooldown", "retry_minutes": parse_minutes(wait.group(1))}
    if base == ".远航归来":
        if "远航·归" in body:
            xw = re.search(r"修为\s*\+(\d+)", body)
            ls = re.search(r"灵石\s*\+(\d+)", body)
            aff = re.search(r"情缘(增加|减少)\s*(\d+)", body)
            items = re.findall(r"^-\s*([^\n]+?\s*x\d+)\s*$", body, re.M)
            return {"status": "returned", "cultivation": int(xw.group(1)) if xw else 0,
                    "spirit_stones": int(ls.group(1)) if ls else 0,
                    "affection_delta": (1 if aff and aff.group(1) == "增加" else -1) * int(aff.group(2)) if aff else 0,
                    "items": items}
        if "远航尚未结束" in body:
            return {"status": "cooldown", "retry_minutes": parse_minutes(body)}
        if "并无可结算" in body or "并未执行远航" in body:
            return {"status": "nothing_to_settle"}
    if base == ".侍妾远航":
        floor = re.search(r"至少需要\s*(\d+)\s*情缘", body)
        if floor:
            return {"status": "affection_too_low", "required_affection": int(floor.group(1))}
        if "远航·启" in body:
            hours = re.search(r"预计归航时间[：:]\s*(\d+)\s*小时", body)
            return {"status": "sailing", "hours": int(hours.group(1)) if hours else None}
        if "已在远航" in body or "尚未归来" in body:
            return {"status": "already_sailing"}
        if "修为不足" in body or "灵石不足" in body:
            return {"status": "insufficient"}
    if base == ".天机代卜":
        if "天机代卜链" in body:
            buff = re.search(r"得卦【(.+?)】", body)
            return {"status": "divined", "buff": buff.group(1) if buff else ""}
    if base == ".入梦寻图":
        if "入梦寻图" in body:
            got = re.search(r"获得\s*【(.+?)】\s*残纹\s*(\S+?)（(新残纹|重复藏本)）", body)
            prog = re.search(r"当前进度[：:]\s*(\d)/4", body)
            rate = re.search(r"本次掉落率[：:]\s*(\d+)%", body)
            return {"status": "found" if got else "missed", "chart": got.group(1) if got else "",
                    "fragment": got.group(2) if got else "", "new": bool(got and got.group(3) == "新残纹"),
                    "progress": int(prog.group(1)) if prog else None, "rate": int(rate.group(1)) if rate else None}
    if base == ".坠魔心劫":
        if "护持尚在" in body or "无需重复" in body:
            return {"status": "protection_active", "retry_minutes": parse_minutes(body)}
        if "护持已成" in body:
            rate = re.search(r"本次成功率[：:]\s*(\d+)%", body)
            return {"status": "protected", "rate": int(rate.group(1)) if rate else None}
        if "未竟全功" in body:
            rate = re.search(r"本次成功率[：:]\s*(\d+)%", body)
            return {"status": "weak_protection", "rate": int(rate.group(1)) if rate else None}
    if base == ".拼图":
        if "仍缺" in body:
            return {"status": "incomplete"}
        if "拼合" in body or "副本" in body or "机缘" in body:
            return {"status": "assembled"}
    if base == ".召回侍妾":
        if "召至身边" in body:
            return {"status": "recalled"}
        if "无法召回" in body or "无人居住" in body:
            return {"status": "recall_failed"}
    return {"status": "unknown"}
