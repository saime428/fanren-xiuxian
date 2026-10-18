#!/usr/bin/env python3
"""Per-flow completion summaries for the operator notification.

Each builder receives the fleet summary written by the flow's ``run_cycle``
and returns ``(key, text)`` when a notification is due, else ``None``.

Rules:
* 琉璃问心塔 / 游历 are daily flows: one "all accounts complete" message per
  Asia/Shanghai day, plus one "day-end partial" message after 23:45 when some
  account never completed.
* 星宫灵圃 is continuous: one message per maintenance batch, sent when every
  configured account has settled (waiting on the server timer) and at least
  one account performed a confirmed action in that batch.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .accounts import ACCOUNT_USERNAMES
from .state import clock_reached, shanghai_now

DAY_END_HOUR, DAY_END_MINUTE = 23, 45


def _label(account: str) -> str:
    return f"@{ACCOUNT_USERNAMES.get(account, account)}"


def _day_end(now_ts: float) -> bool:
    return clock_reached(now_ts, DAY_END_HOUR, DAY_END_MINUTE)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:16]


# ---------------------------------------------------------------- 琉璃问心塔
TOWER_DONE = {"completed", "daily_already_terminal"}


def tower_notification(summary: dict[str, Any], *, now_ts: float) -> tuple[str, str] | None:
    accounts = summary.get("accounts") or []
    day = summary.get("day") or shanghai_now(now_ts).date().isoformat()
    if not accounts:
        return None
    done = [a for a in accounts if a.get("status") in TOWER_DONE]
    complete = len(done) == len(accounts)
    if not complete and not _day_end(now_ts):
        return None
    key = f"{day}:{'complete' if complete else 'partial'}"
    head = (f"【琉璃问心塔】{day} 全部账号完成 {len(done)}/{len(accounts)}" if complete
            else f"【琉璃问心塔】{day} 日终统计：完成 {len(done)}/{len(accounts)}（未完成账号见下）")
    lines = [head]
    for account in accounts:
        name = _label(account.get("account", ""))
        if account.get("status") in TOWER_DONE:
            for identity in account.get("identities") or []:
                lines.append(f"{name} {identity.get('identity', '')}: {identity.get('text') or identity.get('status')}")
            if not account.get("identities"):
                lines.append(f"{name}: 已完成")
        else:
            lines.append(f"{name}: 未完成（{account.get('status')}{' ' + str(account.get('error')) if account.get('error') else ''}）")
    return key, "\n".join(lines)


# ---------------------------------------------------------------- 游历
JOURNEY_DONE = {"daily_complete", "daily_already_complete"}


def journey_notification(summary: dict[str, Any], *, now_ts: float) -> tuple[str, str] | None:
    accounts = summary.get("accounts") or []
    day = summary.get("day") or shanghai_now(now_ts).date().isoformat()
    if not accounts:
        return None
    done = [a for a in accounts if a.get("status") in JOURNEY_DONE]
    complete = len(done) == len(accounts)
    if not complete and not _day_end(now_ts):
        return None
    key = f"{day}:{'complete' if complete else 'partial'}"
    target = summary.get("target_daily_count") or 8
    head = (f"【游历·谨慎】{day} 全部账号完成 {len(done)}/{len(accounts)}，每账号 {target} 次" if complete
            else f"【游历·谨慎】{day} 日终统计：完成 {len(done)}/{len(accounts)}")
    lines = [head]
    for account in accounts:
        name = _label(account.get("account", ""))
        identities = account.get("identities") or []
        if not identities:
            lines.append(f"{name}: {'已完成' if account.get('status') in JOURNEY_DONE else '未完成（' + str(account.get('status')) + ')'}")
            continue
        for identity in identities:
            count, goal = identity.get("daily_count", 0), identity.get("target", target)
            note = identity.get("server_note") or identity.get("error") or ""
            status = "完成" if identity.get("status") == "daily_complete" else str(identity.get("status") or "")
            lines.append(f"{name} {identity.get('identity', '')}: {count}/{goal} {status}{' ' + note if note else ''}")
    return key, "\n".join(lines)


# ---------------------------------------------------------------- 星宫灵圃
STAR_SETTLED = {"wait", "inspect", "action_limit"}
STAR_FAILED = {"action_failed", "action_unconfirmed", "unknown_mutation_outcome", "runtime_error",
               "account_identity_mismatch", "sect_mismatch", "invalid_state", "refresh_failed"}


def _hours(seconds: Any) -> str:
    try:
        value = max(0, int(seconds or 0))
    except (TypeError, ValueError):
        value = 0
    if value >= 3600:
        return f"{value / 3600:.1f} 小时"
    return f"{value // 60} 分钟"


def star_notification(summary: dict[str, Any], *, now_ts: float) -> tuple[str, str] | None:
    accounts = summary.get("accounts") or []
    if not accounts:
        return None
    if any(a.get("status") in {"scheduled_wait", "lock_busy"} for a in accounts):
        # sample_peer_12 is still due later in this batch; report when the batch settles.
        acted_now = [a for a in accounts if sum((a.get("action_counts") or {}).values()) > 0]
        if not acted_now:
            return None
    settled = all(a.get("status") in STAR_SETTLED | STAR_FAILED for a in accounts
                  if a.get("status") not in {"scheduled_wait", "lock_busy"})
    if not settled:
        return None
    acted = [a for a in accounts if sum((a.get("action_counts") or {}).values()) > 0]
    failed = [a for a in accounts if a.get("status") in STAR_FAILED]
    if not acted and not failed:
        return None
    day = shanghai_now(now_ts).date().isoformat()
    key = f"{day}:{_fingerprint([(a.get('account'), a.get('status'), a.get('action_counts'), a.get('updated_at')) for a in accounts])}"
    head = f"【星宫灵圃】{shanghai_now(now_ts).strftime('%m-%d %H:%M')} 本轮完成（{len(acted)} 个账号有操作，{len(failed)} 个失败）"
    lines = [head]
    for account in accounts:
        name = _label(account.get("account", ""))
        if account.get("identity"):
            name = f"{name} {account['identity']}"
        counts = account.get("action_counts") or {}
        parts = []
        if counts.get("soothe"):
            parts.append(f"安抚{counts['soothe']}")
        if counts.get("collect"):
            parts.append(f"收集{counts['collect']}")
        if counts.get("pull"):
            parts.append(f"牵引天雷星{counts['pull']}")
        status = account.get("status")
        if status in STAR_FAILED:
            lines.append(f"{name}: 失败 {status}{' ' + str(account.get('error')) if account.get('error') else ''}")
        elif status in {"scheduled_wait", "lock_busy"}:
            lines.append(f"{name}: 等待中")
        else:
            plots = account.get("plots") or []
            empty = sum(1 for p in plots if p.get("empty"))
            action_text = " ".join(parts) if parts else "无需操作"
            wait_text = f"，下次约 {_hours(account.get('min_wait_sec'))} 后" if account.get("min_wait_sec") else ""
            lines.append(f"{name}: {action_text}，{len(plots)} 盘（空 {empty}）{wait_text}")
    return key, "\n".join(lines)


# ---------------------------------------------------------------- 灵溪垂钓
FISHING_DAILY_CASTS = 5


CHUM_NAMES = {"rice": "米糠小窝", "grass": "灵草窝", "demon": "妖腥窝"}
# Operator 2026-09-28: every cast of every account, with what it caught.
FISHING_MISS = "空竿"
# Hermes sends at most 3500 characters; beyond this the casts drop their
# times and loot, then collapse to one tally line per account.
FISHING_TEXT_LIMIT = 3400


def _catch_text(catch: dict[str, Any], *, detail: bool = True) -> str:
    """One cast, e.g. ``灵鱼 银须灵鲢 2.88斤 +幸运符×1`` or ``空竿``."""
    if catch.get("caught") is False or catch.get("grade") == FISHING_MISS:
        return "空竿（超时）" if catch.get("reason") == "timeout" else FISHING_MISS
    fish = catch.get("fish") or ("上钩（鱼名未识别）" if catch.get("caught") else "已结算（鱼获未识别）")
    parts = [catch.get("grade") or "", fish, (catch.get("weight") or "") if detail else ""]
    text = " ".join(part for part in parts if part)
    rewards = [f"{row['name']}×{row['qty']}" for row in catch.get("rewards") or []
               if isinstance(row, dict) and row.get("name")]
    return text + (" +" + "、".join(rewards) if rewards and detail else "")


def _catch_lines(catches: list[dict[str, Any]], mode: str) -> list[str]:
    tally: dict[str, int] = {}
    for catch in catches:
        label = FISHING_MISS if _catch_text(catch) == FISHING_MISS else (catch.get("grade") or "上钩")
        tally[label] = tally.get(label, 0) + 1
    exp = sum(int(catch.get("exp") or 0) for catch in catches if isinstance(catch.get("exp"), int))
    total = "  鱼获：" + "、".join(f"{label}×{count}" for label, count in tally.items()) + (
        f"，钓术经验 +{exp}" if exp else "")
    if mode == "tally":
        return [total]
    lines = [f"  {index}. " + (f"{catch.get('at')} " if mode == "full" and catch.get("at") else "")
             + _catch_text(catch, detail=mode == "full")
             for index, catch in enumerate(catches, 1)]
    return lines + [total]


def _fishing_limit(row: dict[str, Any], daily_limit: int) -> int | None:
    """A row's own ``daily_cast_limit`` wins; ``None`` means the server's limit ends the day."""
    return row["daily_cast_limit"] if "daily_cast_limit" in row else daily_limit


def _fishing_done(row: dict[str, Any], daily_limit: int) -> bool:
    limit = _fishing_limit(row, daily_limit)
    count = int(row.get("daily_cast_count") or 0)
    return row.get("server_daily_limit_reached") is True or (limit is not None and count >= limit)


def _fishing_line(row: dict[str, Any], daily_limit: int, mode: str = "full") -> str:
    name = _label(row["account"])
    count = int(row.get("daily_cast_count") or 0)
    limit = _fishing_limit(row, daily_limit)
    done = _fishing_done(row, daily_limit)
    settled = len([item for item in (row.get("confirmed_settlement_ids") or []) if isinstance(item, str)])
    status = str(row.get("status") or "")
    last = "上钩" if row.get("caught") is True else ("空竿" if row.get("caught") is False else "")
    extra = ""
    if not done:
        extra = f"，状态 {status}" + (f" {row.get('error_code')}" if row.get("error_code") else "")
    lost = max(0, count - settled) if done and limit is not None else 0
    quota = f"{count}/{limit} 竿" if limit is not None else (
        f"{count} 竿（服务器上限已到）" if done else f"{count} 竿")
    chum = row.get("chum") if isinstance(row.get("chum"), dict) else {}
    used = {key: value for key, value in (chum.get("used_today") or {}).items()
            if isinstance(value, int) and value > 0}
    nests = "、".join(f"{CHUM_NAMES.get(key, key)}×{value}" for key, value in used.items())
    catches = [item for item in row.get("catches") or [] if isinstance(item, dict)]
    head = (f"{name}: {quota}，今日结算 {settled} 次"
            + (f"，{lost} 竿被服务器计数但未确认结算" if lost else "")
            + (f"，打窝 {nests}" if nests else "")
            + (f"，窝料不足（{CHUM_NAMES.get(chum.get('chum'), chum.get('chum'))}）"
               if chum.get("status") == "unaffordable" else "")
            + (f"，最后一竿{last}" if last and not catches else "") + extra)
    return "\n".join([head, *(_catch_lines(catches, mode) if catches else [])])


def _fishing_text(head: str, rows: list[dict[str, Any]], daily_limit: int) -> str:
    """The richest per-cast detail that fits one message."""
    for mode in ("full", "compact", "tally"):
        text = "\n".join([head, *(_fishing_line(row, daily_limit, mode) for row in rows)])
        if len(text) <= FISHING_TEXT_LIMIT:
            return text
    return text


def fishing_notification(coordinator: dict[str, Any], account_results: list[dict[str, Any]], *,
                         now_ts: float, daily_limit: int = FISHING_DAILY_CASTS) -> tuple[str, str] | None:
    """Daily 灵溪垂钓 summary once every configured account has used its casts.

    ``account_results`` rows carry ``account`` plus the per-account
    ``fishing.json`` result fields (``daily_cast_count``,
    ``confirmed_settlement_ids``, ``status``, ``caught``, ``error_code``).
    A row's own ``daily_cast_limit`` wins over ``daily_limit``; ``None`` means
    the server's ``fishing_daily_limit_reached`` ends that account's day.
    """
    day = shanghai_now(now_ts).date().isoformat()
    accounts = [row for row in account_results if row.get("account")]
    if not accounts:
        return None
    complete = (str(coordinator.get("status") or "") == "daily_complete"
                and all(_fishing_done(row, daily_limit) for row in accounts))
    if not complete and not _day_end(now_ts):
        return None
    key = f"{day}:{'complete' if complete else 'partial'}"
    done = sum(1 for row in accounts if _fishing_done(row, daily_limit))
    limits = {_fishing_limit(row, daily_limit) for row in accounts}
    uniform = f"，每账号 {next(iter(limits))} 竿" if len(limits) == 1 and None not in limits else ""
    head = (f"【灵溪垂钓】{day} 全部账号完成 {done}/{len(accounts)}{uniform}" if complete
            else f"【灵溪垂钓】{day} 日终统计：完成 {done}/{len(accounts)}")
    return key, _fishing_text(head, accounts, daily_limit)


def fishing_late_notification(day: str, account_results: list[dict[str, Any]], reported: set[str], *,
                              daily_limit: int = FISHING_DAILY_CASTS) -> tuple[str, str] | None:
    """补报 for accounts that finished after the day's summary had already gone out.

    On 2026-09-26 @demo_player_01 joined fishing after the 00:27 five-account summary;
    its 15 casts were never reported because the day's key was already used.
    """
    late = [row for row in account_results
            if row.get("account") and row["account"] not in reported and _fishing_done(row, daily_limit)]
    if not late:
        return None
    key = f"{day}:late:{'+'.join(sorted(row['account'] for row in late))}"
    head = f"【灵溪垂钓】{day} 补报：{'、'.join(_label(row['account']) for row in late)} 在当日汇总发出后才完成"
    return key, _fishing_text(head, late, daily_limit)


# ---------------------------------------------------------------- 天机命脉
FATE_DONE = {"daily_complete", "daily_already_complete"}
FATE_CHOICES = {"accept": "顺势承命", "defy": "逆势改命", "hide": "藏锋避劫"}


def fate_notification(summary: dict[str, Any], *, now_ts: float) -> tuple[str, str] | None:
    """Daily 天机命脉 summary once every account has settled (or at day end)."""
    accounts = [row for row in summary.get("accounts") or [] if row.get("account")]
    day = summary.get("day") or shanghai_now(now_ts).date().isoformat()
    if not accounts:
        return None
    done = [row for row in accounts if row.get("status") in FATE_DONE]
    complete = len(done) == len(accounts)
    if not complete and not _day_end(now_ts):
        return None
    key = f"{day}:{'complete' if complete else 'partial'}"
    head = (f"【天机命脉】{day} 全部账号完成 {len(done)}/{len(accounts)}" if complete
            else f"【天机命脉】{day} 日终统计：完成 {len(done)}/{len(accounts)}")
    lines = [head]
    for row in accounts:
        name = _label(row["account"])
        identities = row.get("identities") or []
        if not identities:
            lines.append(f"{name}: {row.get('status')}{' ' + str(row.get('error')) if row.get('error') else ''}")
            continue
        for identity in identities:
            quest = identity.get("quest") or {}
            reward = identity.get("reward") or {}
            choice = FATE_CHOICES.get(identity.get("choice"), identity.get("choice") or "未命择")
            state = {"settled": "已验命", "already_settled": "已验命", "quest_pending": "任务进行中",
                     "defy_unmet": "逆势任务未达成", "failed": "失败"}.get(identity.get("status"), identity.get("status"))
            parts = [f"{name} {identity.get('identity', '')}: {choice}，{state}"]
            if quest.get("target") is not None:
                parts.append(f"{quest.get('progress')}/{quest.get('target')}{quest.get('unit') or ''}")
            if reward.get("tianjiTrace"):
                parts.append(f"天机残痕 +{reward['tianjiTrace']}")
            if reward.get("kunwuPass"):
                parts.append(f"昆吾通行令 ×{reward['kunwuPass']}")
            runs = identity.get("beast_runs") or []
            if runs:
                parts.append("噬金虫 " + "、".join(f"{run.get('score')}分" for run in runs))
            if identity.get("error"):
                parts.append(str(identity["error"]))
            lines.append("，".join(parts))
    return key, "\n".join(lines)


# ---------------------------------------------------------------- 噬金虫进化
def beast_notification(summary: dict[str, Any], *, now_ts: float) -> tuple[str, str] | None:
    """Daily 噬金虫进化 summary once every account has played all of today's runs (or at day end)."""
    accounts = [row for row in summary.get("accounts") or [] if row.get("account")]
    day = summary.get("day") or shanghai_now(now_ts).date().isoformat()
    if not accounts:
        return None
    full = [row for row in accounts if row.get("beast_status") == "full"]
    complete = len(full) == len(accounts)
    if not complete and not _day_end(now_ts):
        return None
    key = f"{day}:beast:{'complete' if complete else 'partial'}"
    head = (f"【噬金虫进化】{day} 全部账号打满 {len(full)}/{len(accounts)}" if complete
            else f"【噬金虫进化】{day} 日终统计：打满 {len(full)}/{len(accounts)}")
    lines = [head]
    for row in accounts:
        name = _label(row["account"])
        identities = [identity for identity in row.get("identities") or [] if isinstance(identity.get("beast"), dict)]
        if not identities:
            lines.append(f"{name}: {row.get('status')}{' ' + str(row.get('error')) if row.get('error') else ''}")
            continue
        for identity in identities:
            beast = identity["beast"]
            runs = [run for run in beast.get("runs") or [] if isinstance(run, dict)]
            parts = [f"{name} {identity.get('identity', '')}: {beast.get('used', '?')}/{beast.get('limit', '?')} 局"]
            scores = [str(run["score"]) for run in runs if isinstance(run.get("score"), int)]
            if runs:
                parts.append(f"今日自动 {len(runs)} 局" + (f" {'/'.join(scores)} 分" if scores else ""))
            tiers = [run["max_tier"] for run in runs if isinstance(run.get("max_tier"), int)]
            if tiers:
                parts.append(f"最高 {max(tiers)} 阶")
            trace = sum(run["trace"] for run in runs if isinstance(run.get("trace"), int))
            if trace:
                parts.append(f"天机残痕 +{trace}")
            if beast.get("status") != "full":
                parts.append(f"状态 {beast.get('status')}" + (f" {beast['error']}" if beast.get("error") else ""))
            lines.append("，".join(parts))
    return key, "\n".join(lines)


# ---------------------------------------------------------------- 宗门日常
SECT_DONE = {"completed", "daily_already_complete"}


def sect_notification(summary: dict[str, Any], *, now_ts: float) -> tuple[str, str] | None:
    accounts = summary.get("accounts") or []
    day = summary.get("day") or shanghai_now(now_ts).date().isoformat()
    if not accounts:
        return None
    done = [a for a in accounts if a.get("status") in SECT_DONE]
    complete = len(done) == len(accounts)
    if not complete and not _day_end(now_ts):
        return None
    key = f"{day}:{'complete' if complete else 'partial'}"
    head = (f"【宗门日常】{day} 全部账号完成 {len(done)}/{len(accounts)}（点卯 / 军报 / 支援慕兰）" if complete
            else f"【宗门日常】{day} 日终统计：完成 {len(done)}/{len(accounts)}")
    lines = [head]
    for account in accounts:
        name = _label(account.get("account", ""))
        if account.get("status") not in SECT_DONE:
            lines.append(f"{name}: 未完成（{account.get('status')}{'，' + str(account.get('error')) if account.get('error') else ''}）")
            continue
        checkin = account.get("checkin") or {}
        support = account.get("support") or {}
        steps = account.get("steps") or {}
        publish = steps.get("publish") or {}
        parts = []
        if checkin.get("status") == "checked_in":
            parts.append(f"点卯+{checkin.get('contribution')}贡献 连续{checkin.get('streak_days')}天")
        elif checkin.get("status") == "already_checked_in":
            parts.append("点卯已完成")
        else:
            parts.append(f"点卯 {checkin.get('status')}")
        if publish.get("status") == "true_report":
            parts.append(f"真报+{publish.get('merit')}军功")
        elif publish.get("status") in {"skipped", "already_published"}:
            parts.append("军报未公开" if publish.get("reason") == "no_known_true_report" else "军报已公开")
        if support.get("status") == "supported":
            parts.append(f"支援{support.get('route')} {support.get('outcome')} 军功+{support.get('merit_gain')}"
                         f"（累计{support.get('merit_total')}，连续{support.get('streak_days')}天）"
                         + ("押中密令" if support.get("matched_order") else ""))
        elif support.get("status") == "already_supported":
            parts.append("今日已支援")
        elif support.get("status"):
            parts.append(f"支援 {support.get('status')}")
        lines.append(f"{name}: " + "，".join(parts))
    return key, "\n".join(lines)


# ---------------------------------------------------------------- 藏娇阁
PAVILION_SETTLED = {"completed", "scheduled_wait"}


def pavilion_notification(summary: dict[str, Any], *, now_ts: float) -> tuple[str, str] | None:
    """One message per batch in which at least one companion performed a confirmed action."""
    accounts = summary.get("accounts") or []
    if not accounts:
        return None
    if any(a.get("status") == "lock_busy" for a in accounts):
        return None
    acted = []
    for account in accounts:
        if account.get("status") == "scheduled_wait":
            # Idle accounts echo their last completed cycle; it was already reported.
            continue
        for identity in account.get("identities") or []:
            confirmed = [row for row in identity.get("actions") or []
                         if row.get("status") in {"returned", "divined", "found", "missed", "protected",
                                                  "weak_protection", "assembled", "sailing", "settled"}]
            if confirmed:
                acted.append((account, identity, confirmed))
    failed = [a for a in accounts if a.get("status") in {"account_failed", "account_run_failed"}]
    if not acted and not failed:
        return None
    key = f"{shanghai_now(now_ts).date().isoformat()}:{_fingerprint([(a.get('account'), a.get('updated_at'), [i.get('identity') for i in a.get('identities') or []]) for a in accounts if a.get('status') != 'scheduled_wait'])}"
    head = f"【藏娇阁】{shanghai_now(now_ts).strftime('%m-%d %H:%M')} 本轮完成（{len(acted)} 位侍妾有操作，{len(failed)} 个账号失败）"
    lines = [head]
    labels = {"returned": "归航", "divined": "代卜", "found": "寻图✓", "missed": "寻图✗", "protected": "心劫护持",
              "weak_protection": "心劫弱护持", "assembled": "拼图", "sailing": "远航", "settled": "共历心劫"}
    for account, identity, confirmed in acted:
        parts = []
        for row in confirmed:
            label = labels.get(row["status"], row["status"])
            if row["status"] == "returned":
                label += f"+{row.get('cultivation', 0)}修为/+{row.get('spirit_stones', 0)}灵石"
            elif row["status"] == "divined" and row.get("buff"):
                label += f"({row['buff']})"
            elif row["status"] == "found":
                label += f"({row.get('chart', '')}{row.get('progress', '')}/4)"
            elif row["status"] == "sailing":
                label += f"({row['command'].split(' ')[-1]} {row.get('hours')}h)"
            elif row["status"] == "settled":
                label += f"+{row.get('cultivation')}修为/+{row.get('affection')}情缘"
            parts.append(label)
        lines.append(f"{_label(account.get('account', ''))} {identity.get('identity', '')}: " + "，".join(parts))
    for account in failed:
        lines.append(f"{_label(account.get('account', ''))}: 失败 {account.get('error')}")
    return key, "\n".join(lines)
