"""Persistent, account-scoped stop switch for shattered Xiuxian characters.

Only this module writes account_lifecycle.json. Multiple Telegram observers may
submit evidence; an advisory lock and atomic replacement serialize transitions.
No timer or generic successful command can clear a pause.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from command_confirmation import OFFICIAL_REPLY_SENDER_IDS
from miniapp_shared.accounts import (ACCOUNT_USERNAMES, GAME_CHAT_ID, GAME_GROUP, GAME_SECOND_CHAT_ID,
                                     GAME_TOPIC_ID)

ROOT = Path(__file__).resolve().parent
CHAT_ID = GAME_CHAT_ID
ANNOUNCEMENT_CHAT_ID = GAME_SECOND_CHAT_ID
READ_CHAT_IDS = (CHAT_ID, ANNOUNCEMENT_CHAT_ID)
TOPIC_ID = GAME_TOPIC_ID
TRUSTED_BOT_IDS = OFFICIAL_REPLY_SENDER_IDS  # one official bot list, not a hand-copied subset
SUCCESS = "夺舍成功！你的神魂与新肉身完美融合，所有被封存的神通宝物已尽数回归！"
SHATTER = "【元婴遁逃·虚弱】"
COOLDOWN = re.compile(r"请再温养\s*((?:\d+\s*(?:小时|分钟|秒)\s*)+)[。.]?")


class AccountPausedError(RuntimeError):
    """A definite local refusal: no command was sent."""


def state_path() -> Path:
    return Path(os.environ.get("XIU_ACCOUNT_LIFECYCLE_PATH", str(ROOT / "state/account_lifecycle.json")))


def load_state(path: Path | None = None) -> dict:
    path = path or state_path()
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return {"version": 1, "accounts": {}, "outbox": {}}
    except OSError as exc:
        raise AccountPausedError("account_lifecycle_state_unreadable: fail closed") from exc
    try:
        state = json.loads(raw)
        if (state.get("version") != 1 or not isinstance(state.get("accounts"), dict)
                or not isinstance(state.get("outbox"), dict)):
            raise ValueError("invalid lifecycle schema")
        for row in state["accounts"].values():
            if not isinstance(row, dict) or row.get("status") not in {"paused", "active"}:
                raise ValueError("invalid account status")
        return state
    except (ValueError, TypeError, AttributeError, OSError) as exc:
        raise AccountPausedError("account_lifecycle_state_unreadable: fail closed") from exc


@contextmanager
def transaction(path: Path | None = None):
    path = path or state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = load_state(path)
        yield state
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        os.replace(tmp, path)


def account_slug(account: str | int) -> str:
    value = str(account)
    return value if value.startswith("tg") else f"tg{value}"


def assert_account_allowed(account: str | int, *, path: Path | None = None) -> None:
    slug = account_slug(account)
    row = load_state(path)["accounts"].get(slug, {})
    if row.get("status") == "paused":
        raise AccountPausedError(f"account_shattered_paused:{slug}: awaiting confirmed possession")


def _text(message) -> str:
    return str(getattr(message, "raw_text", None) or getattr(message, "message", "") or "").strip()


def _timestamp(message) -> float:
    date = getattr(message, "date", None)
    return float(date.timestamp()) if date is not None else 0.0


def _topic(message) -> int:
    reply = getattr(message, "reply_to", None)
    return int(getattr(reply, "reply_to_top_id", 0) or getattr(reply, "reply_to_msg_id", 0) or 0)


def classify_recovery_announcement(message) -> dict | None:
    """Official public rebirth announcement; exact subject, never loose mentions."""
    if (getattr(message, "chat_id", None) not in READ_CHAT_IDS
            or getattr(message, "sender_id", None) not in TRUSTED_BOT_IDS):
        return None
    match = re.fullmatch(
        r"【天机异闻·夺舍重生】\s*"
        r"先前肉身陨落的 @(?P<username>[A-Za-z0-9_]{5,32}) "
        r"\(原道号：[^\n)]+\)，其元婴已成功夺舍重生！\s*"
        r"从今日起，他将以【[^】\n]+】为名，身负【[^】\n]+】的全新肉身，"
        r"继续其漫漫仙途！过往因果，一并继承！",
        _text(message),
    )
    if not match:
        return None
    account = next((slug for slug, username in ACCOUNT_USERNAMES.items()
                    if username.casefold() == match["username"].casefold()), None)
    at = _timestamp(message)
    if account is None or at <= 0 or not int(getattr(message, "id", 0) or 0):
        return None
    return {"account": account, "kind": "possessed", "at": at,
            "message_id": int(message.id), "chat_id": int(message.chat_id),
            "sender_id": int(message.sender_id), "text": _text(message)[:600],
            "recovery_format": "public_rebirth_announcement"}


def classify_reply(message, parent) -> dict | None:
    """No attribution by reader identity or loose username substring matching."""
    if (getattr(message, "chat_id", None) != CHAT_ID
            or getattr(message, "sender_id", None) not in TRUSTED_BOT_IDS
            or getattr(parent, "chat_id", None) != CHAT_ID
            or _topic(parent) != TOPIC_ID
            or getattr(message, "reply_to_msg_id", None) != getattr(parent, "id", None)):
        return None
    account = account_slug(getattr(parent, "sender_id", 0))
    if account not in ACCOUNT_USERNAMES:
        return None
    command, text = _text(parent), _text(message)
    if not command.startswith((".", "。")):
        return None
    ts = _timestamp(message)
    if ts <= 0 or not int(getattr(message, "id", 0) or 0):
        return None
    evidence = {"account": account, "at": ts, "message_id": int(message.id),
                "parent_id": int(parent.id), "parent_at": _timestamp(parent),
                "sender_id": int(message.sender_id), "text": text[:600]}
    if text == SUCCESS and re.fullmatch(r"[.。](?:重生\s+[123]|夺舍重生)", command):
        return {**evidence, "kind": "possessed"}
    if text.startswith(SHATTER) and "6小时" in text and "无法进行夺舍" in text:
        return {**evidence, "kind": "shattered", "ready_at": ts + 6 * 3600}
    if command in {".夺舍重生", "。夺舍重生"} and "无法承受夺舍" in text:
        match = COOLDOWN.search(text)
        if match:
            seconds = sum(int(n) * {"小时": 3600, "分钟": 60, "秒": 1}[unit]
                          for n, unit in re.findall(r"(\d+)\s*(小时|分钟|秒)", match[1]))
            return {**evidence, "kind": "weak", "ready_at": ts + seconds}
    # Literal single-star roleplay is the game's shattered-character fallback.
    # A bold heading or an asterisk somewhere in an ordinary reward is not it.
    if re.fullmatch(r"\*[^*].*[^*]\*", text, re.S):
        return {**evidence, "kind": "starred", "ready_at": None}
    return None


def _enqueue(state: dict, key: str, text: str) -> None:
    state["outbox"].setdefault(key, {"text": text, "delivered": False})


def _when(timestamp: float | None) -> str:
    if timestamp is None:
        return "未找到原始碎裂时间；不会从每条星号回复重新计时"
    return datetime.fromtimestamp(timestamp, ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M:%S") + "（北京时间）"


def record_evidence(evidence: dict, *, path: Path | None = None) -> bool:
    account, kind, at = evidence["account"], evidence["kind"], float(evidence["at"])
    if account not in ACCOUNT_USERNAMES:
        return False
    with transaction(path) as state:
        old = state["accounts"].get(account, {})
        if (at <= float(old.get("possessed_at", 0))
                or (kind != "possessed" and 0 < float(evidence.get("parent_at", at)) <= float(old.get("possessed_at", 0)))):
            return False
        if kind == "possessed":
            if at <= float(old.get("last_evidence_at", 0)):
                return False
            state["accounts"][account] = {"status": "active", "possessed_at": at,
                "last_evidence_at": at, "evidence": evidence}
            if old.get("status") == "paused":
                _enqueue(state, f"{account}:resumed:{evidence['message_id']}",
                         f"✅ @{ACCOUNT_USERNAMES[account]} 已收到账号匹配的夺舍成功回执，解除碎裂暂停。")
            return True
        if kind not in {"shattered", "starred", "weak"}:
            return False
        fresh = old.get("status") != "paused"
        row = dict(old) if not fresh else {"status": "paused", "detected_at": at,
            "incident_id": evidence["message_id"], "ready_at": None, "possessed_at": old.get("possessed_at", 0)}
        row["last_evidence_at"] = max(float(row.get("last_evidence_at", 0)), at)
        if kind == "shattered" and (not row.get("shattered_at") or at < row["shattered_at"]):
            ready_at = row["ready_at"] if float(row.get("cooldown_evidence_at", 0)) > at else evidence["ready_at"]
            row.update(shattered_at=at, ready_at=ready_at, evidence=evidence)
        elif kind == "weak" and at >= float(row.get("cooldown_evidence_at", 0)):
            row.update(ready_at=evidence["ready_at"], cooldown_evidence_at=at, evidence=evidence)
        elif fresh:
            row["evidence"] = evidence
        state["accounts"][account] = row
        if fresh:
            _enqueue(state, f"{account}:paused:{row['incident_id']}",
                     f"⏸️ @{ACCOUNT_USERNAMES[account]} 检测到过肉身碎裂，尚未确认夺舍成功，按规则暂停该账号在 Telegram 发送以 . 开头的指令；MiniApp 与青元子操作不受影响。\n"
                     f"可夺舍时间：{_when(row.get('ready_at'))}。到期仍不自动恢复，也不会替你选择肉身。\n"
                     f"证据：https://t.me/{GAME_GROUP}/{TOPIC_ID}/{evidence['message_id']}")
        return fresh


async def observe_reply(client, message, *, path: Path | None = None) -> bool:
    if getattr(message, "chat_id", None) not in READ_CHAT_IDS or getattr(message, "sender_id", None) not in TRUSTED_BOT_IDS:
        return False
    announcement = classify_recovery_announcement(message)
    if announcement:
        return record_evidence(announcement, path=path)
    if getattr(message, "chat_id", None) != CHAT_ID:
        return False
    text = _text(message)
    if not (text.startswith(("*", SHATTER, "夺舍成功")) or "请再温养" in text):
        return False
    parent_id = getattr(message, "reply_to_msg_id", None)
    if not parent_id or parent_id == TOPIC_ID:
        return False
    seen = getattr(client, "_lifecycle_seen_replies", None)
    if seen is None:
        seen = {}
        client._lifecycle_seen_replies = seen
    key = (int(message.id), text)
    if key in seen:
        return False
    parent = await client.get_messages(CHAT_ID, ids=parent_id)
    if parent is None:
        return False
    evidence = classify_reply(message, parent)
    changed = record_evidence(evidence, path=path) if evidence else False
    seen[key] = True
    if len(seen) > 2000:
        seen.pop(next(iter(seen)))
    return changed


def queue_ready_reminders(*, path: Path | None = None, now: float | None = None) -> None:
    now = time.time() if now is None else now
    with transaction(path) as state:
        for account, row in state["accounts"].items():
            if row["status"] == "paused" and row.get("ready_at") and now >= row["ready_at"]:
                _enqueue(state, f"{account}:ready:{row['incident_id']}",
                         f"⏰ @{ACCOUNT_USERNAMES[account]} 碎裂虚弱期预计已结束（{_when(row['ready_at'])}）。"
                         "仍保持 Telegram 点指令暂停（MiniApp 与青元子不受影响）；请手动夺舍，收到明确夺舍成功回执后才恢复。")


def flush_notifications(notifier, *, path: Path | None = None) -> list[dict]:
    """Called only by the singleton observer; tests inject a local notifier."""
    results = []
    snapshot = load_state(path)
    for key, item in snapshot["outbox"].items():
        if item.get("delivered"):
            continue
        account, event, message_id = key.split(":", 2)
        row = snapshot["accounts"].get(account, {})
        if event in {"paused", "ready"}:
            if row.get("status") != "paused" or str(row.get("incident_id")) != message_id:
                continue
        elif row.get("status") != "active" or str(row.get("evidence", {}).get("message_id")) != message_id:
            continue
        result = notifier.send_once(key, item["text"])
        results.append(result)
        if result.get("status") in {"delivered", "already_delivered"}:
            with transaction(path) as state:
                state["outbox"][key]["delivered"] = True
    return results
