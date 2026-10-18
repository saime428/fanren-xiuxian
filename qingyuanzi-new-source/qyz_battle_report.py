#!/usr/bin/env python3
"""Per-event 青元子 fleet battle report, delivered once through Hermes.

Read-only: it reads ``state/world_boss_shared_entry.json`` and the six
``state/accounts/<account>/world_boss_state.json`` journals written by the host.
When every account has a terminal record for the latest entry (or the entry is
older than ``REPORT_DEADLINE_SECONDS``), one report keyed by the event
fingerprint is sent via ``miniapp_shared.notify.Notifier``; the official
settlement post usually lands after it, and its ranks then follow once as a
【青元子贡献榜】 message. Later ticks are no-ops for the same event. Credentials never enter the report: only statuses,
counts, server grades/damage and sanitized error codes.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from miniapp_shared.accounts import ACCOUNT_USERNAMES, accounts_for  # noqa: E402
from miniapp_shared.state import read_json  # noqa: E402
from qyz_state import FAILED_EVENT_STATUSES, SETTLED_STATUSES  # noqa: E402

ACCOUNTS = {account: ACCOUNT_USERNAMES[account] for account in accounts_for("qingyuanzi")}

STATE_ROOT = ROOT / "state"
SHARED_ENTRY = STATE_ROOT / "world_boss_shared_entry.json"
FLOW = "qyz_battle_report"
REPORT_DEADLINE_SECONDS = 15 * 60
SH = timezone(timedelta(hours=8))
TERMINAL_STATUSES = SETTLED_STATUSES | FAILED_EVENT_STATUSES | {"cancelled_before_mutation"}


def is_terminal(status: Any) -> bool:
    text = str(status or "")
    return text in TERMINAL_STATUSES or text.startswith("settled_") or text.startswith("blocked_")


def _entry_ts(entry: dict[str, Any]) -> float:
    text = str(entry.get("published_at") or "")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SH)
    return parsed.timestamp()


def _damage_text(value: Any) -> str:
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        return ""
    if not amount > 0:
        return ""
    # Lazy: the reference module imports Telethon (~100 ms) and the report
    # only needs it for a settled line.
    from qyz_reference.world_boss_features import WorldBossMonitor
    return WorldBossMonitor._format_damage_yi(amount)


def _begin_delay(record: dict[str, Any], entry_ts: float) -> str:
    for phase in record.get("confirmed_phases") or []:
        if isinstance(phase, dict) and phase.get("phase") == "begin_confirmed" and entry_ts > 0:
            try:
                return f"，开战 +{float(phase.get('at')) - entry_ts:.0f}s"
            except (TypeError, ValueError):
                return ""
    return ""


def account_line(account: str, record: dict[str, Any] | None, entry_ts: float) -> tuple[str, dict[str, Any]]:
    name = f"@{ACCOUNTS.get(account, account)}"
    if not record:
        return f"{name}: 无本场记录", {"terminal": False, "hits": 0, "perfects": 0, "windows": 0, "finished": False}
    status = str(record.get("status") or "")
    result = record.get("protocol_result") if isinstance(record.get("protocol_result"), dict) else {}
    server = result.get("server_result") if isinstance(result.get("server_result"), dict) else {}
    windows = int(record.get("expected_windows") or result.get("expected_windows") or 0)
    receipts = record.get("receipts") if isinstance(record.get("receipts"), list) else []
    hits = int(record.get("accepted_hits") or result.get("accepted_hits") or len(receipts))
    perfects = int(record.get("server_perfects") or result.get("server_perfects")
                   or sum(1 for row in receipts if isinstance(row, dict) and row.get("server_perfect")))
    finished = bool(result.get("finish_confirmed"))
    phases = {row.get("phase") for row in record.get("confirmed_phases") or [] if isinstance(row, dict)}
    began = "begin_confirmed" in phases or bool(result.get("begin_confirmed")) or hits > 0 or status.startswith("settled_")
    facts = {"terminal": is_terminal(status), "hits": hits, "perfects": perfects, "windows": windows,
             "finished": finished, "status": status, "began": began}
    if status.startswith("settled_"):
        grade = str(server.get("grade") or "")
        score = server.get("score")
        damage = _damage_text(server.get("realtime_damage_yi"))
        multiplier = result.get("server_damage_multiplier_min")
        label = {"settled_perfect": "全PERFECT", "settled_partial": "部分完美",
                 "settled_damage_reduced": "被降伤"}.get(status, status)
        text = (f"{name}: {label} 命中 {hits}/{windows} PERFECT {perfects}"
                + (f" {grade} {int(score)}分" if grade and isinstance(score, (int, float)) else "")
                + (f" 伤害 {damage}" if damage else "")
                + (f" 倍率 {multiplier}" if isinstance(multiplier, (int, float)) and multiplier < 1 else "")
                + _begin_delay(record, entry_ts))
        return text, facts
    error = str(record.get("error") or "")
    diagnostics = result.get("diagnostics") if isinstance(result.get("diagnostics"), dict) else {}
    browser = diagnostics.get("browser") if isinstance(diagnostics.get("browser"), dict) else {}
    browser_event = str(browser.get("browser_event") or "")
    phase = str(result.get("phase") or "")
    if not facts["terminal"]:
        return f"{name}: 进行中（{status}）", facts
    if began:
        detail = f"已开战未正式结算，命中 {hits}/{windows} PERFECT {perfects}"
    else:
        detail = "未开战"
    text = f"{name}: {detail}，{error or status}" + (f"（{phase}）" if phase else "") \
        + (f"，浏览器 {browser_event}" if browser_event else "") + _begin_delay(record, entry_ts)
    return text, facts


BOARD_MATCH_WINDOW_SECONDS = 30 * 60


def board_belongs_to_entry(board: dict[str, Any], entry: dict[str, Any]) -> bool:
    """Match original Telegram publication times, never edit/observation times."""
    entry_ts, board_ts = _entry_ts(entry), _entry_ts(board)
    if entry_ts <= 0 or board_ts <= 0:
        return False
    if board.get("chat_id") and entry.get("chat_id") and board["chat_id"] != entry["chat_id"]:
        return False
    if board.get("message_id") and entry.get("message_id") and int(board["message_id"]) <= int(entry["message_id"]):
        return False
    same_day = datetime.fromtimestamp(board_ts, SH).date() == datetime.fromtimestamp(entry_ts, SH).date()
    return same_day and 0 <= board_ts - entry_ts <= BOARD_MATCH_WINDOW_SECONDS


def build_missed_event_report(board: dict[str, Any], entry: dict[str, Any]) -> tuple[str, str] | None:
    """Report an unmatched official settlement without inventing participation.

    On 2026-09-21 the host dropped entry 12436550 before any account queued, so
    no journal, no shared entry and therefore no report existed while the
    official board showed a 30-player battle. The board is the only server
    evidence of such an event, so it drives its own once-only report.
    """
    rows = board.get("rows") if isinstance(board.get("rows"), list) else []
    message_id = board.get("message_id")
    if not rows or not message_id or board_belongs_to_entry(board, entry):
        return None
    board_ts = _entry_ts(board)
    when = datetime.fromtimestamp(board_ts, SH).strftime("%m-%d %H:%M") if board_ts > 0 else "时间未知"
    own_names = {name.casefold() for name in ACCOUNTS.values()}
    own_ranked = any(str(row.get("username") or "").casefold() in own_names for row in rows)
    summary = ("本方已有账号上榜，入口未匹配，参战数量待核对" if own_ranked
               else "未记录对应入口，本场参战情况待核实")
    head = f"【青元子战报】{when} 结算榜 {message_id}：{summary}"
    return f"board:{int(message_id)}:missed", "\n".join([head, *board_lines(board, board_ts)])


def build_report(entry: dict[str, Any], records: dict[str, dict[str, Any] | None], *,
                 now_ts: float) -> tuple[str, str] | None:
    fingerprint = str(entry.get("fingerprint") or "")
    message_id = entry.get("message_id")
    board = entry.get("board") if isinstance(entry.get("board"), dict) else {}
    if len(fingerprint) != 64 or not message_id:
        return build_missed_event_report(board, entry) if board else None
    entry_ts = _entry_ts(entry)
    if board and not board_belongs_to_entry(board, entry) and _entry_ts(board) > entry_ts:
        # A newer official settlement than the last admitted entry: that event
        # was missed entirely. Report it instead of re-checking the old entry.
        missed = build_missed_event_report(board, entry)
        if missed is not None:
            return missed
    lines = []
    facts = []
    for account in ACCOUNTS:
        text, fact = account_line(account, records.get(account), entry_ts)
        lines.append(text)
        facts.append(fact)
    all_terminal = all(fact["terminal"] for fact in facts)
    overdue = entry_ts > 0 and now_ts - entry_ts >= REPORT_DEADLINE_SECONDS
    if not all_terminal and not overdue:
        return None
    started = sum(1 for fact in facts if fact.get("began"))
    finished = sum(1 for fact in facts if fact["finished"])
    hits = sum(fact["hits"] for fact in facts)
    perfects = sum(fact["perfects"] for fact in facts)
    expected = max((fact["windows"] for fact in facts), default=0) or 16
    windows = sum(fact["windows"] or expected for fact in facts)
    when = datetime.fromtimestamp(entry_ts, SH).strftime("%m-%d %H:%M") if entry_ts > 0 else "时间未知"
    head = (f"【青元子战报】{when} 入口 {message_id}：开战 {started}/{len(facts)}，正式结算 {finished}/{len(facts)}，"
            f"命中 {hits}/{windows}，PERFECT {perfects}" + ("" if all_terminal else "（超时汇总，含无记录或未结束账号）"))
    key = f"{fingerprint[:16]}:{'final' if all_terminal else 'deadline'}"
    return key, "\n".join([head, *lines, *board_lines(board, entry_ts)])


def build_board_followup(entry: dict[str, Any], reported_at: float) -> tuple[str, str] | None:
    """The event's ranks, once its settlement post lands after the report went out.

    Battles end about three minutes after the entry and the report follows at
    once, while the official post comes about seven minutes after the entry
    (2026-09-26: report 13:43:38, post 13:48:01), so the report itself almost
    never carries the ranks.
    """
    fingerprint = str(entry.get("fingerprint") or "")
    board = entry.get("board") if isinstance(entry.get("board"), dict) else {}
    if len(fingerprint) != 64 or not board or not board_belongs_to_entry(board, entry):
        return None
    if reported_at >= _entry_ts(board):
        return None  # the report was built after the post and already lists it
    entry_ts = _entry_ts(entry)
    lines = board_lines(board, entry_ts)
    if not lines:
        return None
    when = datetime.fromtimestamp(entry_ts, SH).strftime("%m-%d %H:%M")
    head = f"【青元子贡献榜】{when} 入口 {entry.get('message_id')}" + (f"，{board['outcome']}" if board.get("outcome") else "")
    return f"{fingerprint[:16]}:board", "\n".join([head, *lines])


def board_lines(board: dict[str, Any], entry_ts: float, *, window_seconds: float = BOARD_MATCH_WINDOW_SECONDS) -> list[str]:
    """Top three and our own ranks from the official settlement post, if it belongs to this event."""
    rows = board.get("rows") if isinstance(board.get("rows"), list) else []
    if not rows:
        return []
    board_ts = _entry_ts(board)
    if entry_ts > 0 and board_ts > 0 and not (0 <= board_ts - entry_ts <= window_seconds):
        return []
    if entry_ts <= 0 or board_ts <= 0:
        return []
    ours = {name.casefold(): slug for slug, name in ACCOUNTS.items()}
    def fmt(row: dict[str, Any]) -> str:
        return (f"{row.get('rank')}. @{row.get('username')} {row.get('score')}分"
                + (f" 强攻{row.get('attacks')}" if row.get("attacks") else "")
                + (f" 伤害{row.get('damage')}" if row.get("damage") else ""))
    top = [fmt(row) for row in rows[:3] if isinstance(row, dict)]
    mine = [fmt(row) for row in rows if isinstance(row, dict) and str(row.get("username", "")).casefold() in ours]
    lines = []
    if top:
        head = f"贡献榜前三（参战 {board.get('participants')} 人）" if board.get("participants") else "贡献榜前三"
        lines.append(head + "：" + "；".join(top))
    lines.append("本方上榜：" + ("；".join(mine) if mine else "无（前 " + str(board.get("row_count") or len(rows)) + " 名内无本方账号）"))
    try:
        best = int(rows[0].get("score") or 0)
        gaps = [best - int(row.get("score") or 0) for row in rows if isinstance(row, dict)
                and str(row.get("username", "")).casefold() in ours]
        if gaps:
            lines.append(f"与第一名分差：最小 {min(gaps)} 分，最大 {max(gaps)} 分")
    except (TypeError, ValueError):
        pass
    return lines


def collect(state_root: Path = STATE_ROOT) -> tuple[dict[str, Any], dict[str, dict[str, Any] | None]]:
    entry = read_json(state_root / "world_boss_shared_entry.json")
    board = read_json(state_root / "world_boss_shared_board.json")
    if board:
        entry = {**entry, "board": board}
    fingerprint = str(entry.get("fingerprint") or "")
    records: dict[str, dict[str, Any] | None] = {}
    for account in ACCOUNTS:
        journal = read_json(state_root / "accounts" / account / "world_boss_state.json")
        record = None
        events = journal.get("world_boss_events") or []
        if isinstance(events, dict):
            events = events.values()
        for item in events:
            if isinstance(item, dict) and item.get("fingerprint") == fingerprint:
                record = item
        records[account] = record
    return entry, records


def run_once(root: Path = ROOT, *, now_ts: float | None = None, sender=None) -> dict[str, Any]:
    from miniapp_shared.notify import Notifier
    now = time.time() if now_ts is None else float(now_ts)
    entry, records = collect(Path(root) / "state")
    due = build_report(entry, records, now_ts=now)
    if due is None:
        return {"status": "not_due", "fingerprint": str(entry.get("fingerprint") or "")[:16]}
    key, text = due
    notifier = Notifier(Path(root), FLOW, sender=sender, now=lambda: now)
    board = entry.get("board") or {}
    correction_key = f"{str(entry.get('fingerprint') or '')[:16]}:report-correction-v2"
    if notifier.delivered(correction_key):
        return {"status": "already_delivered", "key": correction_key}
    if (key.endswith((":final", ":deadline")) and board_belongs_to_entry(board, entry)
            and notifier.delivered(f"board:{board.get('message_id')}:missed")):
        corrected = text.replace("【青元子战报】", "【青元子战报更正】", 1)
        corrected += "\n更正：此前本场未参战提示有误；以上按本场账号回执和官方榜单核对。"
        return notifier.send_once(correction_key, corrected)
    if key.endswith((":final", ":deadline")):
        stem = key.rsplit(":", 1)[0]
        # One report per event: a late final after the deadline summary only
        # adds noise. Ranks that land after the report follow once.
        sent = [at for at in (notifier.delivered_at(f"{stem}:final"), notifier.delivered_at(f"{stem}:deadline"))
                if at is not None]
        if sent:
            followup = build_board_followup(entry, min(sent))
            outcome = notifier.send_once(*followup) if followup else {"status": "already_delivered", "key": key}
            return outcome
    elif notifier.delivered(key):
        return {"status": "already_delivered", "key": key}
    return notifier.send_once(key, text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--print", action="store_true", help="print the report that would be sent, do not send")
    args = parser.parse_args(argv)
    if args.print:
        entry, records = collect()
        due = build_report(entry, records, now_ts=time.time())
        print(json.dumps({"entry": entry, "report": due[1] if due else None, "key": due[0] if due else None},
                         ensure_ascii=False, indent=2))
        return 0
    result = run_once()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
