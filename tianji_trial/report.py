#!/usr/bin/env python3
"""One 【天机试炼】 report per Asia/Shanghai day through Hermes.

The six Tianji jobs are separate Hermes cron entries, so no single process sees
the whole fleet. Each account run calls ``notify_if_due`` afterwards: it reads
all six ``summary.json`` files and sends once when every account has today's
confirmed completion, or once as a day-end partial after ``PARTIAL_HOUR``.
Keys live in ``state/tianji_trial/notifications.json`` (miniapp_shared.notify).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from miniapp_shared.accounts import ACCOUNT_USERNAMES, ALL_ACCOUNTS
from miniapp_shared.notify import Notifier
from miniapp_shared.state import clock_reached, day_key

FLOW = "tianji_trial"
PARTIAL_HOUR, PARTIAL_MINUTE = 3, 5  # after the last 02:55 retry slot
DONE = {"completed", "done", "already"}


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def build_report(summaries: dict[str, dict[str, Any]], *, today: str, now_ts: float) -> tuple[str, str] | None:
    rows = []
    complete = 0
    for account in ALL_ACCOUNTS:
        summary = summaries.get(account) or {}
        done_today = summary.get("last_date") == today and summary.get("receipt_confirmed") is True
        if done_today:
            complete += 1
        rows.append((account, summary, done_today))
    all_done = complete == len(ALL_ACCOUNTS)
    if not all_done and not clock_reached(now_ts, PARTIAL_HOUR, PARTIAL_MINUTE):
        return None
    key = f"{today}:{'complete' if all_done else 'partial'}"
    head = (f"【天机试炼】{today} 全部账号完成 {complete}/{len(ALL_ACCOUNTS)}" if all_done
            else f"【天机试炼】{today} 日终统计：完成 {complete}/{len(ALL_ACCOUNTS)}")
    lines = [head]
    for account, summary, done_today in rows:
        name = f"@{ACCOUNT_USERNAMES.get(account, account)}"
        identities = summary.get("identities") or []
        if not identities:
            lines.append(f"{name}: 无记录（{summary.get('status') or 'missing'}）")
            continue
        for item in identities:
            status = str(item.get("status") or "")
            label = item.get("summary") or (f"{item.get('completed')}/{item.get('limit')} {status}" if item.get("limit") else status)
            if not done_today and status in DONE:
                label = f"{label}（非今日）"
            elif status not in DONE:
                label = f"{label} {item.get('error') or ''}".strip()
            lines.append(f"{name} {item.get('identity', '')}: {label}")
    return key, "\n".join(lines)


PRODUCTION_ROOT = Path(__file__).resolve().parent.parent


def notify_if_due(project_root: Path, *, now_ts: float | None = None, sender=None) -> dict[str, Any]:
    """Send the day's report for ``project_root``.

    A caller with a private root (tests, rehearsals) must inject ``sender``:
    on 2026-09-21 the coordinator tests ran the real reporter against a
    temporary root after 03:05 and pushed seven "日终统计：完成 0/6 … 无记录"
    messages to the operator, contradicting the correct 6/6 report.
    """
    if sender is None and Path(project_root).resolve() != PRODUCTION_ROOT:
        return {"status": "skipped_non_production_root"}
    now = time.time() if now_ts is None else float(now_ts)
    today = day_key(now)
    root = Path(project_root)
    summaries = {account: _load(root / "tianji_trial" / "state" / "accounts" / account / "summary.json")
                 for account in ALL_ACCOUNTS}
    due = build_report(summaries, today=today, now_ts=now)
    if due is None:
        return {"status": "not_due"}
    key, text = due
    notifier = Notifier(root, FLOW, sender=sender)
    if notifier.delivered(key) or (key.endswith(":partial") and notifier.delivered(key.replace(":partial", ":complete"))):
        return {"status": "already_delivered", "key": key}
    return notifier.send_once(key, text)
