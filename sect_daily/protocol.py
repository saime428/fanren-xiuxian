#!/usr/bin/env python3
"""Reply parsing and decision rules learned from the group log (2026-09-20).

Observed official replies (all direct replies to the command message):

``.宗门点卯``
  * ``点卯成功！你获得了 **N** 点宗门贡献。 你已连续点卯 **N** 天。…``
  * ``今日已点卯，道友明日再来吧。``
``.边境军功`` → ``【慕兰烽烟】`` panel with ``【今日军议】`` (天象 / 军议密令 / 险棋窗口 /
  粮道通畅), ``今日状态：未支援|已支援``, ``最近支援：…``.
``.搜集军报`` → ``【慕兰谍影】`` box: ``状态：待公开|已公开`` and three numbered reports
  ``1. <text>（未辨）``. Before collecting the bot says ``今日尚未搜集军报``.
``.公开军报 N`` → ``【慕兰谍影·真报】…今日支援【<route>】时，将获得情报助力`` or
  ``【慕兰谍影·假报】…今日支援慕兰时，前线警戒会额外提高``.
``.支援慕兰 <斥候|破灯|护阵|奇袭>`` → ``【慕兰烽烟 · <route>】<result>`` with
  ``边境军功 **+N**，累计 **N**`` and ``连续支援 **N** 天``; when already done:
  ``你今日已支援过边境战线（1/1）。请待明日上海时间刷新。``

The four true reports (confirmed by ``·真报`` verdicts in the log) map to one
route each; every other report text in the box is a decoy. Publishing a known
true report before supporting its route lowers that route's risk, which is the
order the operator uses manually: 点卯 → 军功面板 → 搜集 → 公开真报 → 支援该路线.
"""
from __future__ import annotations

import re
from typing import Any

CHECKIN_COMMAND = ".宗门点卯"
PANEL_COMMAND = ".边境军功"
COLLECT_COMMAND = ".搜集军报"
PUBLISH_COMMAND = ".公开军报"
SUPPORT_COMMAND = ".支援慕兰"

ROUTE_ACTIONS = {
    "斥候探草原": "斥候",
    "破慕兰圣灯": "破灯",
    "固守边境法阵": "护阵",
    "夜袭法士营": "奇袭",
}
ACTION_ROUTES = {action: route for route, action in ROUTE_ACTIONS.items()}
# Report text (without trailing 。) -> route it strengthens. Verified from
# 【慕兰谍影·真报】 replies on 2026-09-20.
TRUE_REPORTS = {
    "法士营北帐换防，附灵蛇胆与妖丹暂存在同一灵袋": "夜袭法士营",
    "有小股法士借草沟绕行，似在寻找黄龙山外阵缺口": "斥候探草原",
    "边境粮道将过西岭，阵师缺人护送一批阵旗": "固守边境法阵",
    "今夜圣灯换焰，主灯会短暂离开护灯法士三十息": "破慕兰圣灯",
}
FALLBACK_ACTION = "护阵"

_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _plain(text: Any) -> str:
    return _BOLD.sub(r"\1", str(text or "")).replace("\r", "")


def _route_in(text: str) -> str:
    for route in ROUTE_ACTIONS:
        if route in text:
            return route
    for action, route in ACTION_ROUTES.items():
        if action in text:
            return route
    return ""


# ------------------------------------------------------------------ 宗门点卯
def parse_checkin(text: Any) -> dict[str, Any]:
    plain = _plain(text)
    if "点卯成功" in plain:
        contribution = re.search(r"获得了\s*(\d+)\s*点宗门贡献", plain)
        streak = re.search(r"连续点卯\s*(\d+)\s*天", plain)
        stipend = re.search(r"俸禄\s*(\d+)\s*块", plain)
        return {"status": "checked_in", "contribution": int(contribution.group(1)) if contribution else None,
                "streak_days": int(streak.group(1)) if streak else None,
                "stipend": int(stipend.group(1)) if stipend else None}
    if "今日已点卯" in plain:
        return {"status": "already_checked_in"}
    if "尚未加入" in plain or "没有宗门" in plain or "未加入宗门" in plain:
        return {"status": "no_sect"}
    return {"status": "unknown"}


# ------------------------------------------------------------------ 边境军功
def parse_panel(text: Any) -> dict[str, Any]:
    plain = _plain(text)
    if "慕兰烽烟" not in plain or "今日状态" not in plain:
        return {"status": "unknown"}
    def field(label: str) -> str:
        match = re.search(rf"{label}[：:]\s*([^\n]+)", plain)
        return match.group(1).strip() if match else ""
    council = {}
    for label in ("天象", "军议密令", "险棋窗口", "粮道通畅"):
        match = re.search(rf"-\s*{label}[：:]\s*([^，,\n]+)", plain)
        council[label] = match.group(1).strip() if match else ""
    state = field("今日状态")
    return {
        "status": "supported" if "已支援" in state else ("not_supported" if state else "unknown"),
        "cultivator": field("修士").lstrip("@"),
        "council": council,
        "merit": field("累计边境军功"),
        "streak": field("连续支援"),
        "latest_support": field("最近支援"),
    }


# ------------------------------------------------------------------ 搜集军报
def parse_report_box(text: Any) -> dict[str, Any]:
    plain = _plain(text)
    if "慕兰谍影" not in plain:
        return {"status": "unknown", "reports": []}
    if "今日尚未搜集军报" in plain:
        return {"status": "not_collected", "reports": []}
    reports = []
    for match in re.finditer(r"^\s*(\d+)[.、]\s*(.+?)(?:（(未辨|可信|可疑|已公开|真|假)[^）]*）)?\s*$", plain, re.M):
        reports.append({"number": int(match.group(1)), "text": match.group(2).strip().rstrip("。"),
                        "mark": match.group(3) or ""})
    state = re.search(r"状态[：:]\s*([^\n]+)", plain)
    state_text = state.group(1).strip() if state else ""
    status = "published" if "已公开" in state_text else ("pending" if reports else "unknown")
    return {"status": status, "state": state_text, "reports": reports}


def known_true_report(reports: list[dict[str, Any]]) -> dict[str, Any] | None:
    for report in reports:
        body = str(report.get("text") or "").rstrip("。")
        for true_text, route in TRUE_REPORTS.items():
            if true_text in body:
                return {"number": int(report["number"]), "text": true_text, "route": route}
    return None


# ------------------------------------------------------------------ 公开军报
def parse_publish(text: Any) -> dict[str, Any]:
    plain = _plain(text)
    if "慕兰谍影·真报" in plain or "慕兰谍影 · 真报" in plain:
        merit = re.search(r"边境军功\s*\+\s*(\d+)", plain)
        route = re.search(r"今日支援【(.+?)】", plain)
        return {"status": "true_report", "merit": int(merit.group(1)) if merit else None,
                "route": route.group(1) if route else ""}
    if "慕兰谍影·假报" in plain or "慕兰谍影 · 假报" in plain:
        return {"status": "false_report"}
    if (
        "已公开" in plain
        or "已经公开" in plain
        or "已有军报公开" in plain
    ):
        return {"status": "already_published"}
    if "尚未搜集军报" in plain:
        return {"status": "not_collected"}
    return {"status": "unknown"}


# ------------------------------------------------------------------ 支援慕兰
def parse_support(text: Any) -> dict[str, Any]:
    plain = _plain(text)
    if "今日已支援" in plain or "已支援过" in plain:
        return {"status": "already_supported"}
    match = re.search(r"【慕兰烽烟\s*[·・]\s*(.+?)】\s*([^\n]*)", plain)
    if not match:
        return {"status": "unknown"}
    merit = re.search(r"边境军功\s*\+\s*(\d+)[，,]\s*累计\s*(\d+)", plain)
    streak = re.search(r"连续支援\s*(\d+)\s*天", plain)
    return {"status": "supported", "route": match.group(1).strip(), "outcome": match.group(2).strip(),
            "merit_gain": int(merit.group(1)) if merit else None,
            "merit_total": int(merit.group(2)) if merit else None,
            "streak_days": int(streak.group(1)) if streak else None,
            "matched_order": "押中" in plain, "risky_success": "险棋得手" in plain,
            "intel_bonus": "真军报正在发挥作用" in plain}


# ------------------------------------------------------------------ decision
def choose_support_action(council: dict[str, str] | None, published_route: str = "") -> dict[str, Any]:
    """Pick the 支援 route the way the operator does by hand.

    Priority: the route strengthened by a published true report (risk lowered,
    情报助力), then the 军议密令 route (extra merit when matched), then 粮道通畅,
    then 天象; the 险棋窗口 route never adds score. Ties prefer 护阵 (lowest risk),
    and with no signal at all the flow falls back to 护阵.
    """
    scores: dict[str, int] = {}
    reasons: list[str] = []
    def add(route: str, score: int, reason: str) -> None:
        if route in ROUTE_ACTIONS:
            scores[route] = scores.get(route, 0) + score
            reasons.append(reason)
    council = council or {}
    if published_route:
        add(published_route, 5, f"已公开真军报，{published_route}获得情报助力")
    order = _route_in(council.get("军议密令", ""))
    if order:
        add(order, 4, f"军议密令 {order}")
    grain = _route_in(council.get("粮道通畅", ""))
    if grain:
        add(grain, 2, f"粮道通畅 {grain}")
    sky = _route_in(council.get("天象", "")) or _route_in(council.get("天象", "").replace("护阵", "固守边境法阵"))
    if sky:
        add(sky, 1, f"天象 {sky}")
    risky = _route_in(council.get("险棋窗口", ""))
    if not scores:
        return {"route": "固守边境法阵", "action": FALLBACK_ACTION, "reasons": ["无军议信号，默认护阵"]}
    best = max(scores, key=lambda route: (scores[route], route == "固守边境法阵"))
    if risky and risky == best and scores[best] < 5:
        reasons.append(f"{risky} 为险棋窗口，仅在真军报助力时选择")
        others = {r: s for r, s in scores.items() if r != risky}
        if others:
            best = max(others, key=lambda route: (others[route], route == "固守边境法阵"))
    return {"route": best, "action": ROUTE_ACTIONS[best], "reasons": reasons, "scores": scores}
