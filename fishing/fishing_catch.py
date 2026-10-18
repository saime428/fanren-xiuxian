#!/usr/bin/env python3
"""One cast's catch from the official ``/result`` reply: fish, grade, weight, companion loot.

The reply shape is not documented. The upstream client for this game
(``references/upstream-fanrenxiuxian-auto/miniapp_fishing.py``) reads
``result.caught``, ``result.fish {name, weight}``, ``result.rarityLabel`` or
``result.rarity`` (keys as in ``session.rarityLabels``), ``result.expGain`` and,
for an empty cast, ``result.message``. Two other upstream clients
(``upstream-xiuxian-mian`` and ``xianxia-companion``) also accept ``fishName``,
``grade``, ``rewards`` / ``bonusLoot`` and the result text
"…竟是【鱼名】… 品阶：… 重量：…斤 伴生机缘：【…】xN", so those are read too.
"""
from __future__ import annotations

import re
from typing import Any

# ``start.session.rarityLabels`` on 2026-09-25.
RARITY_LABELS = {"common": "凡鱼", "spirit": "灵鱼", "rare": "珍鱼", "demon": "妖鱼",
                 "legendary": "异种", "miss": "空竿"}
MISS = "空竿"
_GRADE_KEYS = ("grade", "rarityLabel", "rarity_label", "quality", "qualityLabel", "quality_label",
               "rank", "rarity", "品阶")
_NAME_KEYS = ("fishName", "fish_name", "fishTitle", "fish_title")
_WEIGHT_KEYS = ("weight", "weightJin", "weight_jin", "jin")
_REWARD_KEYS = ("rewards", "reward", "bonusLoot", "bonus_loot", "loot", "drops", "items", "materials")
_NESTED_KEYS = ("result", "details", "detail", "catch")
_TEXT_LIMIT = 40


def _text(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()[:_TEXT_LIMIT]


def _first(data: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        text = _text(data.get(key))
        if text:
            return text
    return ""


def _grade(data: dict[str, Any]) -> str:
    grade = _first(data, _GRADE_KEYS)
    return RARITY_LABELS.get(grade, grade)


def _weight(data: dict[str, Any]) -> str:
    for key in _WEIGHT_KEYS:
        value = data.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return f"{float(value):.2f}斤" if value > 0 else ""
    weight = _first(data, _WEIGHT_KEYS)
    return weight if not weight or "斤" in weight else f"{weight}斤"


def _exp(data: dict[str, Any]) -> int:
    for scope in (data, *(data.get(key) for key in _NESTED_KEYS)):
        if not isinstance(scope, dict):
            continue
        for key in ("expGain", "exp_gain", "fishingExpGain"):
            value = scope.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                return int(value)
    return 0


def _rewards(data: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[Any] = []
    for key in _REWARD_KEYS:
        value = data.get(key)
        if isinstance(value, list):
            items.extend(value)
        elif isinstance(value, dict):
            items.append(value)
        elif isinstance(value, str):
            items.append(value)
    rewards = []
    for item in items:
        if isinstance(item, dict):
            name = _first(item, ("name", "itemName", "item_name", "title", "label"))
            qty = item.get("qty", item.get("count", item.get("quantity", item.get("amount", 1))))
        else:
            name, qty = _text(item), 1
        try:
            qty = max(1, int(qty or 1))
        except (TypeError, ValueError, OverflowError):
            qty = 1
        if name:
            rewards.append({"name": name, "qty": qty})
    return rewards


def _from_mapping(data: dict[str, Any], *, nested: bool) -> dict[str, Any]:
    fish_value = data.get("fish")
    if isinstance(fish_value, dict):
        inner = _from_mapping(fish_value, nested=True)
        fish = inner.get("fish") or _first(data, _NAME_KEYS + ("name",))
        if fish:
            return {"fish": fish, "grade": inner.get("grade") or _grade(data),
                    "weight": inner.get("weight") or _weight(data),
                    "rewards": inner.get("rewards") or _rewards(data)}
    fish = _text(fish_value) if isinstance(fish_value, str) else ""
    fish = fish or _first(data, _NAME_KEYS) or (_first(data, ("name", "title", "label")) if nested else "")
    if not fish:
        return {}
    return {"fish": fish, "grade": _grade(data), "weight": _weight(data), "rewards": _rewards(data)}


def _from_text(text: str) -> dict[str, Any]:
    fish = ""
    for pattern in (r"竟是[^\n【]*【([^】]+)】", r"钓获[^\n【]*【([^】]+)】", r"鱼获[^\n【]*【([^】]+)】"):
        match = re.search(pattern, text)
        if match:
            fish = _text(match.group(1))
            break
    if not fish:
        return {}
    grade = re.search(r"品阶[:：]\s*([^\s，,。；;]+)", text)
    weight = re.search(r"重量[:：]\s*([\d,.]+)\s*斤", text)
    rewards = [{"name": _text(m.group(1)), "qty": int(m.group(2) or 1)}
               for m in re.finditer(r"伴生机缘[:：]\s*【([^】]+)】(?:[x×](\d+))?", text)]
    return {"fish": fish, "grade": _text(grade.group(1)) if grade else "",
            "weight": f"{weight.group(1)}斤" if weight else "", "rewards": rewards}


def _caught(data: dict[str, Any]) -> bool | None:
    for scope in (data, *(data.get(key) for key in _NESTED_KEYS)):
        if isinstance(scope, dict) and isinstance(scope.get("caught"), bool):
            return scope["caught"]
    return None


def extract_catch(data: Any) -> dict[str, Any]:
    """``{caught, fish, grade, weight, rewards, exp}`` for one settled cast; empty fields when unknown."""
    if not isinstance(data, dict):
        return {"caught": None, "fish": "", "grade": "", "weight": "", "rewards": [], "exp": 0}
    caught = _caught(data)
    # Preserve only observed server reason codes, never arbitrary error text.
    reasons = [scope.get("reason") for scope in (data, *(data.get(k) for k in _NESTED_KEYS))
               if isinstance(scope, dict) and isinstance(scope.get("reason"), str)
               and scope.get("reason") in {"timeout", "miss"}]
    miss_detail = {"reason": reasons[0]} if reasons else {}
    if caught is False:
        return {"caught": False, "fish": "", "grade": MISS, "weight": "", "rewards": [], "exp": _exp(data), **miss_detail}
    found: dict[str, Any] = {}
    scopes = [(data, False)] + [(data[key], True) for key in _NESTED_KEYS if isinstance(data.get(key), dict)]
    for scope, nested in scopes:
        found = _from_mapping(scope, nested=nested)
        if found:
            break
    if not found:
        for scope, _nested in scopes:
            for value in scope.values():
                if isinstance(value, str) and "【" in value:
                    found = _from_text(value)
                    if found:
                        break
            if found:
                break
    grade = found.get("grade") or next((_grade(scope) for scope, _ in scopes if _grade(scope)), "")
    if not found and grade == MISS:
        return {"caught": False, "fish": "", "grade": MISS, "weight": "", "rewards": [], "exp": _exp(data), **miss_detail}
    rewards = found.get("rewards") or next((_rewards(scope) for scope, _ in scopes if _rewards(scope)), [])
    return {"caught": True if found.get("fish") else caught, "fish": found.get("fish", ""),
            "grade": grade, "weight": found.get("weight", ""), "rewards": rewards, "exp": _exp(data)}
