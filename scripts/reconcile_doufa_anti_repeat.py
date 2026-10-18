#!/usr/bin/env python3
"""Recover exact, account-attributed duel anti-repeat replies into pause state."""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from reply_parsing import DUEL_ANTI_REPEAT_MARKERS
from transport import telegram_client

ROOT = Path(__file__).resolve().parent.parent
FLOW = "doufa_demo_player_02"
TARGET = "@example_game_group"
TOPIC_ID = 7310786


def _reply_to_message_id(message: Any) -> int:
    value = getattr(message, "reply_to_msg_id", None)
    if value:
        return int(value)
    reply = getattr(message, "reply_to", None)
    return int(getattr(reply, "reply_to_msg_id", 0) or 0)


def anti_repeat_reply_for_sent(messages: list[Any], sent_message_id: int) -> Any | None:
    matches = [
        message
        for message in messages
        if _reply_to_message_id(message) == int(sent_message_id)
        and any(marker in str(getattr(message, "message", "") or "") for marker in DUEL_ANTI_REPEAT_MARKERS)
    ]
    return max(matches, key=lambda message: int(getattr(message, "id", 0) or 0), default=None)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("export ") and "=" in line:
            key, value = line[7:].split("=", 1)
            values[key.strip()] = value.strip().strip("'\"")
    return values


async def reconcile_account(account: str) -> dict[str, Any]:
    state_dir = ROOT / "state" / "accounts" / account / FLOW
    attempt_path = state_dir / "last_attempt.json"
    pause_path = state_dir / "pause_until.json"
    attempt = _read_json(attempt_path)
    sent_id = int(attempt.get("sent_message_id") or attempt.get("message_id") or 0)
    if sent_id <= 0:
        return {"account": account, "status": "no_sent_message"}
    env = _read_env(ROOT / "config" / "accounts" / f"{account}.env")
    live_session = state_dir / "session.session"
    if not live_session.exists():
        return {"account": account, "status": "session_missing"}
    copy_dir = Path(tempfile.mkdtemp(prefix=f"doufa-reconcile-{account}-"))
    copy_base = copy_dir / "session"
    shutil.copy2(live_session, copy_base.with_suffix(".session"))
    client = telegram_client(str(copy_base), int(env["TG_API_ID"]), env["TG_API_HASH"])
    try:
        await client.start()
        entity = await client.get_entity(TARGET)
        messages = await client.get_messages(entity, ids=list(range(sent_id, sent_id + 12)))
        reply = anti_repeat_reply_for_sent([item for item in messages if item], sent_id)
        if reply is None:
            return {"account": account, "status": "no_anti_repeat_reply", "sent_message_id": sent_id}
        text = str(getattr(reply, "message", "") or "")
        reply_id = int(getattr(reply, "id", 0) or 0)
        reply_date = getattr(reply, "date", None)
        reply_ts = float(reply_date.timestamp()) if reply_date is not None else time.time()
        attempt.update(
            {
                "ts": reply_ts,
                "status": "acknowledged",
                "completion_reason": "recovered exact direct duel anti-repeat reply",
                "acked": True,
                "ack_reply_message_id": reply_id,
                "ack_reply_text": text,
                "duel_anti_repeat": True,
                # Anti-repeat receipts do not carry a fresh 今日神念 counter.
                # Never retain an older outcome's counter in this attempt.
                "daily_mind_remaining": None,
                "daily_mind_limit": None,
            }
        )
        _write_json(attempt_path, attempt)
        pause = {
            "mode": "manual_resume_required",
            "account": account,
            "scope": f"{account}:{FLOW}:{TARGET}:{TOPIC_ID}",
            "reason": "anti-repeat reply: 交锋过多 / 暂不可再次斗法",
            "reply_message_id": reply_id,
            "reply_text": text,
            "saved_at_ts": time.time(),
        }
        _write_json(pause_path, pause)
        return {"account": account, "status": "paused", "sent_message_id": sent_id, "reply_message_id": reply_id}
    finally:
        await client.disconnect()
        shutil.rmtree(copy_dir, ignore_errors=True)


async def main_async(accounts: list[str]) -> list[dict[str, Any]]:
    results = []
    for account in accounts:
        results.append(await reconcile_account(account))
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("accounts", nargs="+")
    args = parser.parse_args()
    results = asyncio.run(main_async(args.accounts))
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(result["status"] in {"paused", "no_anti_repeat_reply"} for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
