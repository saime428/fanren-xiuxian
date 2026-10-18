#!/usr/bin/env python3
"""Six-account XIUXIAN host for the three user-supplied QYZ reference scripts."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import fcntl
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import logging
import os
import re
import signal
import time
import sys
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from telethon import events, types, utils
from telethon.tl.functions.messages import RequestMainWebViewRequest
from telethon.tl.functions.channels import GetParticipantRequest

from qyz_auth import ENTRY_ROUTER_PATH, parse_webview_auth, read_session_in_memory
from qyz_http import HttpTransport, ORIGIN, PAGE_PATH, READ_ENDPOINTS
from qyz_protocol import ProtocolError, ProtocolRuntime
from qyz_timing import TIMING_POLICY_VERSION
from qyz_verification import SerialVerificationService
from qyz_humanize import HUMANIZATION_VERSION, Humanization

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from account_lifecycle_telegram import LifecycleTelegramClient as TelegramClient
from miniapp_shared.accounts import (ACCOUNT_USERNAMES, CREDENTIAL_SOURCE, GAME_CHAT_ID, GAME_TOPIC_ID,
                                     accounts_for, feature_options)
from miniapp_shared.state import read_json, write_json
from qyz_reference.world_boss_features import _button_url
from transport import _mtproxy_connection_options, ensure_connected
from qyz_state import (FAILED_EVENT_STATUSES, HEAVY_RESULT_KEYS, RUNTIME_VERSION, SETTLED_STATUSES,
                       SOURCE_CHAT_TOPICS, event_needs_reconciliation, server_damage_multiplier_min,
                       slim_event_record)

STATE_ROOT = ROOT / "state"
SHARED_ENTRY_STATE = STATE_ROOT / "world_boss_shared_entry.json"
# One account scans Telegram history for the battle entry: the qingyuanzi account
# with "entry_discovery": true, else the first one.
ENTRY_DISCOVERY_ACCOUNT = next((account for account in accounts_for("qingyuanzi")
                                if feature_options(account, "qingyuanzi").get("entry_discovery") is True),
                               next(iter(accounts_for("qingyuanzi")), None))
SHARED_ENTRY_STATE_POLL_SECONDS = 0.25
ENTRY_RECOVERY_WINDOW_SECONDS = 60
# A failure before the first mutation is redelivered by the account itself:
# the shared signal reaches each account only once, so without this one
# Telegram RPC error, FloodWait or page fetch failure cost the whole event.
ENTRY_RETRY_SECONDS = 5
# The official page is the same for every account. Health reads it at most once
# a minute for the fleet; an entry reads it fresh and, if that fails, may use a
# result up to four minutes old.
POLICY_FALLBACK_MAX_AGE_SECONDS = 240
_SECURITY_POLICY: dict[str, Any] = {}
BEIJING_TZ = ZoneInfo("Asia/Shanghai")
HEALTH_INTERVAL_SECONDS = 60
READ_PROGRESS_PHASES = frozenset(f"{endpoint}_{stage}" for endpoint in READ_ENDPOINTS
                                for stage in ("submitted", "confirmed"))
VERIFICATION_SERVICE: SerialVerificationService | None = None
WORLD_BOSS_TOKEN_PREFIX = "qyz_"
# Each account's timed battle runs on its own thread and event loop. On
# 2026-09-22 every strike timer shared the host loop with six Telethon clients,
# the admission history scan, the verification worker and 1 MB journal writes;
# the loop stalled 150-370 ms at the same instants for several accounts
# (wake_lateness p90 129-209 ms against 3-57 ms on 09-15..09-20), and 20 of 78
# hits arrived late enough to lose PERFECT. A battle thread runs only this
# account's HTTP; the interpreter switch interval bounds what remains.
BATTLE_EXECUTOR = ThreadPoolExecutor(max_workers=6, thread_name_prefix="qyz-battle")
# Phases the battle thread waits to see journaled before its request leaves:
# the first mutations decide whether a restart may ever re-dispatch the entry.
SYNCHRONOUS_TRACE_PHASES = frozenset({"start_submitted", "begin_submitted"})
# Every other trace (window plans, charges, strikes) used to rewrite the whole
# journal too: 7-12 writes per window, 700-1000 per battle, each 5-7 ms of
# interpreter lock at 1 MB. Only the first mutations, server receipts,
# account conflicts and settlement are written at once; the rest is flushed
# at most once a second. A crash loses at most that second of progress, and
# mutation_started is already on disk by then.
DURABLE_TRACE_PHASES = SYNCHRONOUS_TRACE_PHASES | SETTLED_STATUSES | {"finish_confirmed"}
JOURNAL_FLUSH_SECONDS = 1.0
# Battles share this process's interpreter lock with Telethon, the verifier and
# journaling, and any CPU burst there delays their timers: on 2026-09-24 four
# accounts settling within 1.6 s (redaction, deep copy and JSON of 157 KB
# results) woke @demo_player_05's last strike 897 ms late and lost it. Settlement
# evidence therefore waits for the other battles, and the host hands the lock
# over every 0.5 ms instead of 5 ms (offline, a 1.1 s CPU burst delayed six
# battle-like timers by 349 ms at 5 ms, 137 ms at 1 ms, 51 ms at 0.5 ms).
SETTLEMENT_QUIET_WAIT_SECONDS = 150
BATTLE_SWITCH_INTERVAL_SECONDS = 0.0005
_BATTLES: dict[str, Any] = {"in_flight": 0, "quiet": None, "loop": None}


def _battles_quiet_event() -> asyncio.Event:
    loop = asyncio.get_running_loop()
    if _BATTLES["quiet"] is None or _BATTLES["loop"] is not loop:
        _BATTLES.update(quiet=asyncio.Event(), loop=loop)
        if _BATTLES["in_flight"] <= 0:
            _BATTLES["quiet"].set()
    return _BATTLES["quiet"]


def _battle_started() -> None:
    _BATTLES["in_flight"] += 1
    _battles_quiet_event().clear()


def _battle_finished() -> None:
    _BATTLES["in_flight"] = max(0, _BATTLES["in_flight"] - 1)
    if _BATTLES["in_flight"] == 0:
        _battles_quiet_event().set()


async def wait_for_battles_to_finish(limit_seconds: float = SETTLEMENT_QUIET_WAIT_SECONDS) -> None:
    if _BATTLES["in_flight"] <= 0:
        return
    try:
        await asyncio.wait_for(_battles_quiet_event().wait(), limit_seconds)
    except TimeoutError:
        pass


def light_protocol_result(result: dict[str, Any]) -> dict[str, Any]:
    """Counts, server verdicts and failure summary without per-window evidence."""
    light = {key: value for key, value in result.items()
             if key not in HEAVY_RESULT_KEYS and key not in {"reference_outcome", "diagnostics"}}
    outcome = result.get("reference_outcome")
    if isinstance(outcome, dict):
        light["reference_outcome"] = {key: value for key, value in outcome.items() if key != "diagnostics"}
    diagnostics = result.get("diagnostics")
    if isinstance(diagnostics, dict):
        light["diagnostics"] = {key: diagnostics[key] for key in ("browser", "account", "identity")
                                if key in diagnostics}
    return light
CANONICAL_CHAT_ID = GAME_CHAT_ID
CANONICAL_TOPIC_ID = GAME_TOPIC_ID
TARGET_CHAT_IDS = tuple(SOURCE_CHAT_TOPICS)

ACCOUNTS = {account: {"source": CREDENTIAL_SOURCE[account], "username": ACCOUNT_USERNAMES[account]}
            for account in accounts_for("qingyuanzi")}


def build_verification_service() -> SerialVerificationService:
    trusted_root = STATE_ROOT.resolve(strict=False)
    # ``browser-profile`` is the shared profile that produced every accepted
    # /begin token on 2026-09-15/16 (v18) and every fallback token since. v24
    # runs it alone: six parallel widgets at 13:41 kept timing out (09-17..23).
    return SerialVerificationService(
        trusted_root / "browser-profile", trusted_root=trusted_root,
        account_order=tuple(ACCOUNTS),
    )


@dataclass(frozen=True, slots=True)
class OfficialWorldBossEntry:
    message_id: int
    chat_id: int | None
    bot_username: str
    bot_id: int | None
    fingerprint: str
    token: str = field(repr=False)


def session_source_for(account: str) -> str:
    return str(ACCOUNTS[str(account)]["source"])


def _read_exports(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("export ") and "=" in line:
            key, value = line[len("export ") :].split("=", 1)
            values[key.strip()] = value.strip().strip("'\"")
    return values


def _read_env(account: str) -> dict[str, str]:
    return _read_exports(PROJECT_ROOT / "config" / "accounts" / f"{account}.env")


def _load_transport_env() -> None:
    path = PROJECT_ROOT / "config" / "accounts" / "mtproto.env"
    for key, value in (_read_exports(path) if path.exists() else {}).items():
        os.environ.setdefault(key, value)
    os.environ.setdefault("TG_TELEGRAM_CONNECTION_GATE_PATH", str(PROJECT_ROOT / "state" / "telegram_connection_gate.lock"))


async def official_security_policy(account: str, *, max_age: float = 0, fallback_age: float = 0) -> dict[str, Any]:
    """Read the page policy through this account and remember it for the fleet."""
    if _SECURITY_POLICY and time.time() - _SECURITY_POLICY["at"] < max_age:
        return _SECURITY_POLICY["policy"]
    try:
        async with HttpTransport(account) as api:
            policy = await api.security_policy()
    except Exception:
        cached = _SECURITY_POLICY.get("policy")
        if cached is None or time.time() - _SECURITY_POLICY["at"] > fallback_age:
            raise
        return cached
    _SECURITY_POLICY.update(policy=policy, at=time.time())
    return policy


def _telegram_client(session: str, api_id: int, api_hash: str, **options: object) -> TelegramClient:
    options.update(_mtproxy_connection_options())
    memory_session = read_session_in_memory(Path(session))
    return TelegramClient(memory_session, api_id, api_hash, **options)


def _host_session_path(account: str) -> Path:
    return STATE_ROOT / "accounts" / account / "session.session"


def _state_path(account: str) -> Path:
    return STATE_ROOT / "accounts" / account / "world_boss_state.json"


def account_scans_telegram_history(account: str) -> bool:
    return str(account) == ENTRY_DISCOVERY_ACCOUNT


def admission_scan_interval_seconds(now: datetime) -> int:
    # Naive caller times also mean Beijing, independent of the host timezone.
    local = now.replace(tzinfo=BEIJING_TZ) if now.tzinfo is None else now.astimezone(BEIJING_TZ)
    clock = (local.hour, local.minute, local.second)
    return 5 if (13, 39, 30) <= clock < (13, 41, 0) else 15


def _message_text(message: Any) -> str:
    return str(getattr(message, "raw_text", "") or getattr(message, "message", "") or "")


def extract_world_boss_entry(message: Any, *, sender: Any = None) -> OfficialWorldBossEntry | None:
    """The QYZ deep link identifies an entry; the sender is only metadata."""
    chat_id = getattr(message, "chat_id", None)
    if chat_id not in SOURCE_CHAT_TOPICS:
        return None
    reply = getattr(message, "reply_to", None)
    topic = getattr(reply, "reply_to_top_id", None) or getattr(reply, "reply_to_msg_id", None)
    required_topic = SOURCE_CHAT_TOPICS[chat_id]
    if required_topic is not None and topic != required_topic:
        return None
    message_id = int(getattr(message, "id", 0) or 0)
    if message_id <= 0:
        return None
    urls = []
    for row in getattr(message, "buttons", None) or []:
        for button in row:
            urls.append(_button_url(button))
    urls.extend(str(getattr(entity, "url", "") or "") for entity in getattr(message, "entities", None) or [])
    urls.extend(re.findall(r'https://(?:www\.)?(?:t\.me|telegram\.me)/[^\s<>"\]\)]+', _message_text(message)))
    for url in urls:
        try:
            parsed = urllib.parse.urlsplit(url)
        except ValueError:
            continue
        if parsed.scheme.lower() != "https" or parsed.hostname not in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}:
            continue
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            continue
        bot_username = parts[0].lstrip("@").casefold()
        if not re.fullmatch(r"[a-z0-9_]{5,64}", bot_username) or not bot_username.endswith("bot"):
            continue
        query = urllib.parse.parse_qs(parsed.query)
        token = str((query.get("startapp") or query.get("start_param") or [""])[0]).strip()
        if not token.startswith(WORLD_BOSS_TOKEN_PREFIX) or len(token) > 160 or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
            continue
        sender_name = str(getattr(sender, "username", "") or "").casefold()
        return OfficialWorldBossEntry(
            message_id=message_id, chat_id=chat_id, bot_username=bot_username,
            bot_id=getattr(sender, "id", None) if sender_name == bot_username else None,
            fingerprint=hashlib.sha256(token.encode("utf-8")).hexdigest(), token=token,
        )
    return None


def message_recent_enough(message: Any) -> bool:
    message_date = getattr(message, "date", None)
    if not isinstance(message_date, datetime):
        return False
    if message_date.tzinfo is None:
        message_date = message_date.replace(tzinfo=timezone.utc)
    forwarded_date = getattr(getattr(message, "fwd_from", None), "date", None)
    if isinstance(forwarded_date, datetime):
        if forwarded_date.tzinfo is None:
            forwarded_date = forwarded_date.replace(tzinfo=timezone.utc)
        message_date = min(message_date, forwarded_date)
    age = (datetime.now(timezone.utc) - message_date.astimezone(timezone.utc)).total_seconds()
    return -30 <= age <= ENTRY_RECOVERY_WINDOW_SECONDS


async def latest_source_message(client: Any, chat_id: int = CANONICAL_CHAT_ID, *, full: bool = True) -> Any:
    """Check recent links plus announcement history, with no sender filter.

    ``full=False`` reads only the newest message: enough to prove the source is
    readable for an account that never discovers entries itself.
    """
    if chat_id not in SOURCE_CHAT_TOPICS:
        return None
    topic = SOURCE_CHAT_TOPICS[chat_id]
    recent_options = {"limit": 100 if full else 1, **({"reply_to": topic} if topic is not None else {})}
    reads = [client.get_messages(chat_id, **recent_options)]
    if full:
        reads.append(client.get_messages(chat_id, limit=32, search="世界通告"))
    batches = await asyncio.gather(*reads)
    candidates = {}
    for batch in batches:
        if batch is not None and not isinstance(batch, (list, tuple)):
            batch = [batch]
        for message in batch or []:
            if getattr(message, "chat_id", None) == chat_id and extract_world_boss_entry(message):
                candidates[message.id] = message
    return candidates[max(candidates)] if candidates else None


def verification_status(account: str | None = None) -> dict[str, Any]:
    return (VERIFICATION_SERVICE.capability(account) if VERIFICATION_SERVICE
            else {"available": False, "error": "verifier_not_running"})


async def request_official_webview_url(client: Any, bot_username: str, start_param: str) -> str:
    bot = await client.get_entity(bot_username)
    if getattr(bot, "bot", False) is not True:
        raise ProtocolError("entry_target_not_bot", phase="webview")
    result = await client(RequestMainWebViewRequest(
        peer=await client.get_input_entity(bot),
        bot=utils.get_input_user(bot),
        platform="macos",
        start_param=start_param,
        theme_params=types.DataJSON(data="{}"),
    ))
    url = str(getattr(result, "url", "") or "")
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != urllib.parse.urlsplit(ORIGIN).netloc
            or parsed.path not in {PAGE_PATH, ENTRY_ROUTER_PATH}):
        raise ProtocolError("official_webview_origin_invalid", phase="webview")
    return url


async def resolve_source_channel(client: Any, chat_id: int = CANONICAL_CHAT_ID) -> types.Channel:
    """Resolve the authorized private group without persisting an entity cache."""
    if chat_id not in SOURCE_CHAT_TOPICS:
        raise ProtocolError("target_chat_unavailable", phase="source")
    try:
        channel = await client.get_entity(chat_id)
    except ValueError:
        # A new MemorySession has no entity access hashes. The user's dialogs
        # supply that exact group's authenticated entity without a disk cache.
        channel = None
        async for dialog in client.iter_dialogs(limit=200):
            if dialog.id == chat_id:
                channel = dialog.entity
                break
    if (not isinstance(channel, types.Channel) or not channel.megagroup
            or utils.get_peer_id(channel) != chat_id):
        raise ProtocolError("target_chat_unavailable", phase="source")
    return channel


async def verify_account_membership(client: Any, account: str, chat_id: int = CANONICAL_CHAT_ID) -> bool:
    """A login is not proof of group membership; ask Telegram for this exact actor."""
    try:
        channel = await resolve_source_channel(client, chat_id)
        reply = await client(GetParticipantRequest(channel=channel, participant=types.InputPeerSelf()))
        participant = reply.participant
        return (type(participant).__name__ in {
            "ChannelParticipant", "ChannelParticipantSelf", "ChannelParticipantAdmin", "ChannelParticipantCreator"
        } and getattr(participant, "user_id", None) == int(account[2:]))
    except Exception:
        return False


async def resolve_actor_target_chats(actor: Any) -> list[int]:
    """Resolve only the authorized group through this account's Telegram client."""
    if tuple(getattr(actor, "target_chat_ids", ())) != TARGET_CHAT_IDS:
        return []
    resolved = []
    for chat_id in TARGET_CHAT_IDS:
        try:
            await resolve_source_channel(actor.client, chat_id)
            resolved.append(chat_id)
        except Exception:
            continue
    return resolved


def sanitize_state(value: Any) -> Any:
    if isinstance(value, dict):
        blocked = ("token", "initdata", "webview", "chargeticket", "authorization", "cookie", "session",
                   "turnstile", "secret", "signature")
        return {str(k): sanitize_state(v) for k, v in value.items()
                if str(k).lower() != "response_body"
                and not re.sub(r"[^a-z]", "", str(k).lower()).endswith("url")
                and not any(part in re.sub(r"[^a-z]", "", str(k).lower()) for part in blocked)}
    if isinstance(value, (list, tuple)):
        return [sanitize_state(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return type(value).__name__


def _message_published_at(message: Any) -> str | None:
    # Telegram edit_date and our observation time are not publication time.
    value = getattr(message, "date", None)
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(BEIJING_TZ).isoformat(timespec="seconds")


def _write_shared_entry(message: Any, entry: OfficialWorldBossEntry) -> None:
    payload = {
        "chat_id": int(getattr(message, "chat_id", 0) or 0),
        "message_id": entry.message_id,
        "fingerprint": entry.fingerprint,
        "published_at": _message_published_at(message),
        "timestamp_source": "telegram_message_date",
    }
    if not payload["chat_id"] or not payload["message_id"] or not payload["fingerprint"] or not payload["published_at"]:
        return
    write_json(SHARED_ENTRY_STATE, payload)


SHARED_BOARD_STATE = STATE_ROOT / "world_boss_shared_board.json"
_BOARD_ROW = re.compile(r"^\s*(?P<rank>\d+)\.\s*@(?P<username>[A-Za-z0-9_]+)\s*-\s*(?P<score>[\d,]+)\s*分"
                        r"(?:\s*｜\s*强攻\s*(?P<attacks>\d+))?(?:\s*｜\s*伤害\s*(?P<damage>[^\n\r｜]+))?", re.M)


def parse_settlement_board(text: str) -> dict[str, Any] | None:
    """Parse the official 【世界通告｜真仙试锋…】 settlement post (贡献榜 + 参战)."""
    raw = str(text or "")
    if "贡献榜" not in raw or "真仙试锋" not in raw:
        return None
    rows = [{"rank": int(m.group("rank")), "username": m.group("username"),
             "score": int(m.group("score").replace(",", "")), "attacks": int(m.group("attacks") or 0),
             "damage": (m.group("damage") or "").strip()} for m in _BOARD_ROW.finditer(raw)]
    participants = re.search(r"参战[:：]\s*\**(\d+)", raw)
    outcome = re.search(r"结果[:：]\s*\**([^\n*]+)", raw)
    return {"rows": rows[:50], "participants": int(participants.group(1)) if participants else None,
            "outcome": outcome.group(1).strip() if outcome else "", "row_count": len(rows)}


def _write_shared_board(message: Any, board: dict[str, Any]) -> None:
    payload = {**board, "chat_id": int(getattr(message, "chat_id", 0) or 0), "message_id": int(getattr(message, "id", 0) or 0),
               "published_at": _message_published_at(message), "timestamp_source": "telegram_message_date"}
    if not payload["chat_id"] or not payload["message_id"] or not board.get("rows") or not payload["published_at"]:
        return
    write_json(SHARED_BOARD_STATE, payload)


class AccountActor:
    def __init__(self, account: str, client: TelegramClient) -> None:
        self.account_key = account
        self.client = client
        self.target_chat_ids = TARGET_CHAT_IDS
        self.state_file = str(_state_path(account))
        # A malformed journal restarts empty; a symlinked one is refused.
        state = read_json(Path(self.state_file))
        self.state = {} if state == {"status": "malformed_state"} else state
        self.my_info: Any = None

    def save_state(self) -> None:
        path = Path(self.state_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        # This journal is also written immediately before timed mutations.
        # Compact encoding keeps historical diagnostics from blocking strikes.
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(path)


class OfficialWorldBossMonitor:
    def __init__(self, actor: AccountActor, account: str, logger: logging.Logger | None = None) -> None:
        self.actor = actor
        self.client = actor.client
        self.account = account
        self.log = logger or logging.getLogger(account)
        self.target_chats: list[int] = []
        self._tasks: set[asyncio.Task[Any]] = set()
        self._inflight: set[str] = set()
        self._account_lock = asyncio.Lock()
        self._new_handler: Any = None
        self._edit_handler: Any = None
        self._new_builder: Any = None
        self._edit_builder: Any = None
        self._disconnect_waiter: asyncio.Future | None = None
        self._journal_flush: asyncio.TimerHandle | None = None
        # Production constructs this monitor only after acquiring the account
        # process lease. A previous process's unfinished journal is never live.
        recovered = False
        for record in self.actor.state.get("world_boss_events", []):
            # Ageless on purpose: a phase status left by a dead process must be
            # rewritten as unknown no matter how old it is.
            if (event_needs_reconciliation(record, window_seconds=None)
                    and record.get("status") not in FAILED_EVENT_STATUSES):
                record.update(recovered_from_status=record.get("status"),
                              status="unknown_mutation_outcome",
                              error="interrupted_before_settlement",
                              recovered_at=datetime.now().isoformat(timespec="seconds"))
                recovered = True
            if self._archived(record) and slim_event_record(record):
                recovered = True
        if recovered:
            self.actor.save_state()

    def _archived(self, record: dict[str, Any]) -> bool:
        """True when the immutable battle_diagnostics snapshot of this event exists."""
        state_file = getattr(self.actor, "state_file", None)
        message_id = record.get("message_id") if isinstance(record, dict) else None
        if not state_file or isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
            return False
        return (Path(state_file).parent / "battle_diagnostics" / f"{message_id}.json").is_file()

    def _record(self, entry: OfficialWorldBossEntry, status: str, error: str = "", result: Any = None,
                *, persist: bool = True, retry_at_ts: float | None = None) -> None:
        records = self.actor.state.setdefault("world_boss_events", [])
        record = next((item for item in records if item.get("fingerprint") == entry.fingerprint), None)
        if record is None:
            record = {"message_id": entry.message_id, "fingerprint": entry.fingerprint,
                      "chat_id": entry.chat_id, "bot_username": entry.bot_username, "bot_id": entry.bot_id}
            records.append(record)
        record.update({"status": status, "error": error, "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        if retry_at_ts is None:
            record.pop("retry_at_ts", None)
        else:
            record["retry_at_ts"] = retry_at_ts
        if result is not None:
            record["protocol_result"] = sanitize_state(result)
        if status.endswith("_submitted") and status.removesuffix("_submitted") in {"start", "begin", "charge-start", "hit", "finish"}:
            record["mutation_started"] = True
        self.actor.state["world_boss_execution_owner"] = "official_protocol"
        self.actor.state["world_boss_last_error"] = error
        self._health(status, phase=status, mutation_started=bool(record.get("mutation_started")),
                     event_fingerprint=entry.fingerprint, message_id=entry.message_id,
                     business_ready=status == "settled_perfect", blockers=[error] if error else [],
                     **({k: result[k] for k in ("expected_windows", "accepted_hits", "server_perfects", "finish_confirmed",
                                              "missed_windows", "local_missed_windows", "local_player_hp", "local_dead",
                                              "server_damage_multiplier_min") if k in result}
                        if isinstance(result, dict) else {}))
        records[:] = [item for index, item in enumerate(records)
                      if index >= len(records) - 20 or event_needs_reconciliation(item)]
        if persist:
            self._save_journal()

    def _save_journal(self) -> None:
        if self._journal_flush is not None:
            self._journal_flush.cancel()
            self._journal_flush = None
        self.actor.save_state()

    def _defer_journal(self) -> None:
        if self._journal_flush is None:
            self._journal_flush = asyncio.get_running_loop().call_later(JOURNAL_FLUSH_SECONDS, self._save_journal)

    async def _archive_event(self, entry: OfficialWorldBossEntry) -> None:
        state_file = getattr(self.actor, "state_file", None)
        if not state_file:
            return
        record = next((item for item in self.actor.state.get("world_boss_events", [])
                       if item.get("fingerprint") == entry.fingerprint), None)
        if not record or not record.get("mutation_started"):
            return
        try:
            from qyz_diagnostics import archive_battle

            def archive() -> Path:
                # This account's battle is over, so nothing mutates the record
                # while the copy, redaction and fsync run off the loop.
                payload = sanitize_state({"schema_version": 1, "account": self.account,
                    "username": ACCOUNTS[self.account]["username"], "runtime_version": RUNTIME_VERSION,
                    "entry_message_id": entry.message_id, "source_chat_id": entry.chat_id,
                    "entry_fingerprint": entry.fingerprint, "record": copy.deepcopy(record)})
                return archive_battle(Path(state_file), payload)

            await asyncio.to_thread(archive)
            # The archive now holds the full per-window evidence; the live
            # journal keeps statuses, counts, receipts and health inputs only.
            if slim_event_record(record):
                self.actor.save_state()
        except Exception as exc:
            self.log.error("QYZ diagnostic archive failed account=%s entry=%s error=%s",
                           self.account, entry.message_id, type(exc).__name__)

    def _health(self, status: str, **fields: Any) -> None:
        self.actor.state["world_boss_execution_owner"] = "official_protocol"
        health = self.actor.state.setdefault("world_boss_health", {})
        if fields.get("event_fingerprint") and fields["event_fingerprint"] != health.get("event_fingerprint"):
            # Receipt counters belong to one battle. A newly queued event must
            # never inherit the previous battle's finish/perfect confirmation.
            for key in ("expected_windows", "accepted_hits", "server_perfects", "missed_windows",
                        "local_missed_windows", "local_player_hp", "local_dead", "finish_confirmed",
                        "server_damage_multiplier_min"):
                health.pop(key, None)
            health["finish_confirmed"] = False
        health.update({"account": self.account, "flow": "qyz_miniapp", "status": status,
                       "timestamp": time.time(), "ts": time.time(), "pid": os.getpid(),
                       "execution_owner": "official_protocol", "runtime_version": RUNTIME_VERSION,
                       "timing_policy_version": TIMING_POLICY_VERSION,
                       "humanization_version": HUMANIZATION_VERSION,
                       "monitor_active": bool(self.actor.state.get("world_boss_monitor_active")),
                       "monitored_chat_ids": list(self.target_chats), **fields})

    def _watch_disconnect(self) -> None:
        if self._disconnect_waiter is not None and not self._disconnect_waiter.done():
            return
        future = getattr(self.client, "disconnected", None)
        if not asyncio.isfuture(future):
            return
        self._disconnect_waiter = future

        def consume(done: asyncio.Future) -> None:
            if not done.cancelled():
                done.exception()

        future.add_done_callback(consume)

    async def refresh_health(self) -> None:
        """Read-only admission checks; never create WebViews or consume game entries."""
        self._watch_disconnect()
        health = self.actor.state.get("world_boss_health", {})
        connected = bool(self.client.is_connected())
        if self._account_lock.locked():
            self._health(str(health.get("status", "battle_in_progress")), connected=connected)
            self.actor.save_state()
            return
        fields: dict[str, Any] = {"connected": connected, "identity_verified": False,
                                  "membership_verified": False, "source_verified": False,
                                  "source_checks": [], "entry_fresh": False,
                                  "latest_source_message_id": None, "latest_source_chat_id": None,
                                  "latest_source_bot_username": None, "latest_source_bot_id": None,
                                  "verification_required": None, "verification_available": False}
        blockers = []
        try:
            async with asyncio.timeout(25):
                # Telethon stops reconnecting after its bounded retry budget.
                # Reuse this account's client and handlers so the monitor can
                # recover without replacing a process or cancelling a battle.
                await ensure_connected(self.client)
                self._watch_disconnect()
                me = await self.client.get_me()
                fields["identity_verified"] = (getattr(me, "id", None) == int(self.account[2:])
                    and str(getattr(me, "username", "")).casefold() == ACCOUNTS[self.account]["username"])
                source_checks = fields["source_checks"]
                found = []
                for chat_id in TARGET_CHAT_IDS:
                    check = {"chat_id": chat_id, "topic_id": SOURCE_CHAT_TOPICS[chat_id],
                             "membership_verified": False, "source_verified": False,
                             "entry_found": False, "message_id": None}
                    source_checks.append(check)
                    try:
                        check["membership_verified"] = await verify_account_membership(self.client, self.account, chat_id)
                        message = await latest_source_message(
                            self.client, chat_id, full=account_scans_telegram_history(self.account))
                    except Exception as exc:
                        check["error"] = type(exc).__name__
                        continue
                    # An authenticated history read proves the source is available.
                    # An empty group or no QYZ announcement is a normal event wait.
                    check["source_verified"] = True
                    entry = extract_world_boss_entry(message, sender=getattr(message, "sender", None)) if message else None
                    check.update(entry_found=entry is not None, message_id=entry.message_id if entry else None)
                    if entry:
                        found.append((message, entry))
                fields["membership_verified"] = all(row["membership_verified"] for row in source_checks)
                fields["source_verified"] = all(row["source_verified"] for row in source_checks)
                fields["source_checked_at"] = time.time()
                if not fields["source_verified"]:
                    blockers.append("source_unavailable")
                if found:
                    message, entry = max(found, key=lambda pair: pair[0].date.timestamp())
                    fields["entry_fresh"] = message_recent_enough(message)
                    fields["latest_source_message_id"] = getattr(message, "id", None)
                    fields["latest_source_chat_id"] = entry.chat_id
                    fields["latest_source_bot_username"] = entry.bot_username if entry else None
                    fields["latest_source_bot_id"] = entry.bot_id if entry else None
        except Exception:
            blockers.append("source_check_failed")
        fields["connected"] = bool(self.client.is_connected())
        if not fields["identity_verified"]:
            blockers.insert(0, "identity_unverified")
        if not fields["membership_verified"]:
            blockers.append("membership_unverified")
        if not fields["connected"]:
            blockers.insert(0, "telegram_disconnected")
        try:
            policy = await official_security_policy(self.account, max_age=HEALTH_INTERVAL_SECONDS)
            verifier = verification_status(self.account)
            fields.update(verification_required=policy["enabled"],
                          verification_available=policy["enabled"] is False or verifier["available"],
                          verification_method="reference_native_browser",
                          policy_checked_at=time.time(), page_sha256=policy.get("page_sha256"))
            if policy["enabled"] and not verifier["available"]:
                blockers.append("official_verification_required")
        except Exception:
            blockers.append("security_policy_unavailable")
        # An event may have started while the read-only probes awaited I/O.
        if self._account_lock.locked():
            return
        records = self.actor.state.get("world_boss_events", [])
        unresolved = [item for item in records if event_needs_reconciliation(item)]
        latest_status = records[-1].get("status") if records else None
        multiplier = server_damage_multiplier_min(records[-1]) if records else None
        damage_reduced = multiplier is not None and multiplier < 1
        fields["server_damage_multiplier_min"] = multiplier
        if unresolved:
            blockers.append("unfinished_battle_requires_reconciliation")
        elif damage_reduced:
            blockers.append("server_damage_reduced")
        elif latest_status in FAILED_EVENT_STATUSES:
            blockers.append("latest_battle_not_successful")
        status = "blocked_" + blockers[0] if blockers else "idle_waiting_event"
        if unresolved:
            status = "unknown_mutation_outcome"
        elif damage_reduced:
            # Old v16 records retain their original status and server receipts;
            # only the fresh health view corrects the previous false green.
            status = "settled_damage_reduced"
        elif latest_status in FAILED_EVENT_STATUSES:
            status = latest_status
        self._health(status, **fields, blockers=blockers, business_ready=not blockers,
                     unresolved_event_count=len(unresolved))
        self.actor.state["world_boss_last_error"] = blockers[0] if blockers else ""
        self.actor.save_state()

    def _trace(self, entry: OfficialWorldBossEntry, record: dict[str, Any]) -> None:
        if record.get("account") != self.account:
            raise ProtocolError("trace_account_mismatch")
        phase = str(record["phase"])
        self._record(entry, phase, persist=False)
        target = next(item for item in self.actor.state["world_boss_events"] if item["fingerprint"] == entry.fingerprint)
        if "receipt" in record:
            target.setdefault("receipts", []).append(sanitize_state(record["receipt"]))
        if "missed_window" in record:
            target.setdefault("missed_window_records", []).append(sanitize_state(record["missed_window"]))
        if isinstance(record.get("window_state"), dict):
            window = sanitize_state(record["window_state"])
            fingerprint = window.get("window_fingerprint")
            if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
                raise ProtocolError("window_trace_invalid")
            target.setdefault("window_states", {})[fingerprint] = window
        if "account_conflict" in record:
            target["account_conflict"] = sanitize_state(record["account_conflict"])
        for key in ("expected_windows", "accepted_hits", "server_perfects", "missed_windows",
                    "local_missed_windows", "local_player_hp", "local_dead"):
            if key in record:
                target[key] = record[key]
        if phase in {"start_confirmed", "begin_confirmed", "verification_accepted", "finish_confirmed"}:
            target.setdefault("confirmed_phases", []).append({"phase": phase, "at": time.time()})
        self._health("battle_in_progress", phase=phase)
        # Read polling changes no game state: the next write carries it.
        if phase in READ_PROGRESS_PHASES:
            return
        if phase in DURABLE_TRACE_PHASES or "receipt" in record or "account_conflict" in record:
            self._save_journal()
        else:
            self._defer_journal()

    async def install(self) -> bool:
        self.target_chats = await resolve_actor_target_chats(self.actor)
        if not self.target_chats:
            self.actor.state["world_boss_monitor_active"] = False
            self.actor.state["world_boss_last_error"] = "target_chat_unavailable"
            self._health("blocked_target_chat_unavailable", business_ready=False)
            self.actor.save_state()
            return False
        self._new_handler = self._on_message
        self._edit_handler = self._on_message
        self._new_builder = events.NewMessage(chats=self.target_chats)
        self._edit_builder = events.MessageEdited(chats=self.target_chats)
        self.client.add_event_handler(self._new_handler, self._new_builder)
        self.client.add_event_handler(self._edit_handler, self._edit_builder)
        self.actor.state["world_boss_monitor_active"] = True
        self.actor.state["world_boss_execution_owner"] = "official_protocol"
        self.actor.state["world_boss_last_error"] = ""
        self._health("starting", business_ready=False, blockers=["admission_unverified"])
        self.actor.save_state()
        return True

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._disconnect_waiter is not None:
            if self._disconnect_waiter.done() and not self._disconnect_waiter.cancelled():
                self._disconnect_waiter.exception()
            else:
                self._disconnect_waiter.cancel()
        remover = getattr(self.client, "remove_event_handler", None)
        if callable(remover):
            for handler, builder in (
                (self._new_handler, self._new_builder),
                (self._edit_handler, self._edit_builder),
            ):
                if handler is not None and builder is not None:
                    remover(handler, builder)
        self.actor.state["world_boss_monitor_active"] = False
        self._health("stopped", business_ready=False)
        self._save_journal()

    async def _on_message(self, event: Any) -> None:
        await self.process_message(getattr(event, "message", event), source="telegram_update")

    async def _run_exclusive(self, entry: OfficialWorldBossEntry) -> dict[str, Any]:
        async with self._account_lock:
            return await self.run_entry(entry)

    async def run_entry(self, entry: OfficialWorldBossEntry) -> dict[str, Any]:
        self._record(entry, "preparing_webview")
        result: dict[str, Any] = {}
        # Settled or mid-battle failures journal a light result at once; the
        # full evidence and the archive wait until no battle is running.
        deferred: dict[str, Any] | None = None
        try:
            # Re-read the exact new announcement after taking the account mutex.
            # A stale queued object or shared-state record is never an authority.
            message = await self.client.get_messages(entry.chat_id, ids=entry.message_id)
            self._health("preparing_webview", source_checked_at=time.time(), source_verified=False)
            if message is None or not getattr(message, "id", None):
                raise ProtocolError("source_unavailable", phase="source")
            current = extract_world_boss_entry(message, sender=getattr(message, "sender", None))
            if (current is None
                    or current.fingerprint != entry.fingerprint or current.chat_id != entry.chat_id
                    or current.bot_username != entry.bot_username):
                raise ProtocolError("source_invalid", phase="source")
            if not message_recent_enough(message):
                raise ProtocolError("entry_expired", phase="source")
            self._health("preparing_webview", source_verified=True)
            me = await self.client.get_me()
            identity_ok = (getattr(me, "id", None) == int(self.account[2:])
                           and str(getattr(me, "username", "")).casefold() == ACCOUNTS[self.account]["username"])
            self._health("preparing_webview", identity_verified=identity_ok)
            if not identity_ok:
                raise ProtocolError("identity_mismatch", phase="identity")
            member = await verify_account_membership(self.client, self.account, entry.chat_id)
            self._health("preparing_webview", membership_verified=member)
            if not member:
                raise ProtocolError("membership_unverified", phase="membership")
            policy = await official_security_policy(self.account, fallback_age=POLICY_FALLBACK_MAX_AGE_SECONDS)
            required = policy.get("enabled") if isinstance(policy, dict) else None
            available = required is False or verification_status(self.account)["available"]
            self._health("preparing_webview", verification_required=required,
                         verification_available=available, verification_method="reference_native_browser",
                         policy_checked_at=time.time())
            if required is None or not available:
                raise ProtocolError("official_webview_verification_required", phase="preflight")
            if required and VERIFICATION_SERVICE is not None and message_recent_enough(message):
                prepare = getattr(VERIFICATION_SERVICE, "prepare_event", None)
                if callable(prepare):
                    prepare(current.fingerprint, account=self.account, valid_for_seconds=max(
                        0, message.date.timestamp() + ENTRY_RECOVERY_WINDOW_SECONDS + 30 - time.time()))
            url = await request_official_webview_url(self.client, current.bot_username, current.token)
            auth = parse_webview_auth(url, account=self.account, username=ACCOUNTS[self.account]["username"],
                                      entry_token=current.token, entry_fingerprint=current.fingerprint)
            # Identity, membership, policy and WebView awaits can exhaust
            # the one-minute admission window after the first check.
            if not message_recent_enough(message):
                raise ProtocolError("entry_expired", phase="source")
            result = await self._run_battle_isolated(current, auth)
            self._record(entry, str(result["status"]), result=light_protocol_result(result))
            deferred = {"status": str(result["status"]), "result": result}
            self.log.info("[%s] QYZ protocol: %s entry=%s", self.account, result["status"], entry.message_id)
            return result
        except asyncio.CancelledError:
            status = "unknown_mutation_outcome" if self._event(entry).get("mutation_started") else "cancelled_before_mutation"
            self._record(entry, status, error="cancelled")
            raise
        except Exception as exc:
            code = exc.code if isinstance(exc, ProtocolError) else "runtime_exception"
            status = {
                "source_unavailable": "blocked_source_unavailable", "source_invalid": "blocked_source_invalid",
                "entry_expired": "blocked_entry_expired",
                "identity_mismatch": "blocked_identity_mismatch", "membership_unverified": "blocked_membership",
                "official_webview_verification_required": "blocked_official_verification_required",
            }.get(code, "unknown_mutation_outcome" if isinstance(exc, ProtocolError) and exc.uncertain else "runtime_error")
            details = exc.details if isinstance(exc, ProtocolError) and isinstance(exc.details, dict) else {}
            result = {"account": self.account, "status": status, "error": code,
                      "phase": exc.phase if isinstance(exc, ProtocolError) else "host", "finish_confirmed": False,
                      # Server-confirmed counts of a failed battle stay visible to
                      # the report and the health view.
                      **{key: details[key] for key in ("expected_windows", "accepted_hits", "server_perfects",
                                                       "begin_confirmed", "local_missed_windows", "local_player_hp",
                                                       "local_dead") if key in details},
                      "diagnostics": details}
            record = self._event(entry)
            # Nothing was submitted: every failure (Telegram RPC or FloodWait,
            # membership, page policy, WebView) is retried; process_message
            # stops once the announcement is older than the admission window.
            uncertain = isinstance(exc, ProtocolError) and exc.uncertain
            retry_at_ts = (None if record.get("mutation_started") or uncertain else
                           time.time() + max(ENTRY_RETRY_SECONDS, float(getattr(exc, "seconds", 0) or 0)))
            if record.get("mutation_started"):
                # Never retried; the full evidence waits for the other battles.
                self._record(entry, status, error=code, result=light_protocol_result(result), retry_at_ts=retry_at_ts)
                deferred = {"status": status, "error": code, "result": result, "retry_at_ts": retry_at_ts}
            else:
                self._record(entry, status, error=code, result=result, retry_at_ts=retry_at_ts)
            self.log.error("[%s] QYZ protocol blocked: %s (%s) entry=%s retry=%s", self.account, code,
                           type(exc).__name__, entry.message_id, retry_at_ts is not None)
            return result
        finally:
            try:
                if deferred is not None:
                    await wait_for_battles_to_finish()
                    self._record(entry, deferred["status"], error=deferred.get("error", ""),
                                 result=deferred["result"], retry_at_ts=deferred.get("retry_at_ts"))
                await self._archive_event(entry)
            finally:
                self._inflight.discard(entry.fingerprint)

    async def _trace_async(self, entry: OfficialWorldBossEntry, record: dict[str, Any]) -> None:
        self._trace(entry, record)

    def _trace_safe(self, entry: OfficialWorldBossEntry, record: dict[str, Any]) -> None:
        try:
            self._trace(entry, record)
        except Exception as exc:  # noqa: BLE001 - journaling never aborts a running battle
            self.log.error("QYZ trace failed account=%s phase=%s error=%s",
                           self.account, record.get("phase"), type(exc).__name__)

    async def _run_battle_isolated(self, current: OfficialWorldBossEntry, auth: Any) -> dict[str, Any]:
        """Run the reference battle on a dedicated thread with its own event loop.

        The host loop keeps Telegram, health and journaling; every trace record
        is handed back to it in order. Only the first mutation phases block the
        battle thread until they are journaled, so a restart can still tell
        whether this entry was ever dispatched.
        """
        loop = asyncio.get_running_loop()
        account, username = self.account, ACCOUNTS[self.account]["username"]
        broker = VERIFICATION_SERVICE.broker if VERIFICATION_SERVICE else None
        actor = self.actor

        def trace(record: dict[str, Any]) -> None:
            try:
                if record.get("phase") in SYNCHRONOUS_TRACE_PHASES:
                    asyncio.run_coroutine_threadsafe(self._trace_async(current, record), loop).result(timeout=10)
                else:
                    loop.call_soon_threadsafe(self._trace_safe, current, record)
            except Exception as exc:  # noqa: BLE001 - a stopped host loop must not kill the battle
                logging.getLogger(account).error("QYZ trace handoff failed phase=%s error=%s",
                                                 record.get("phase"), type(exc).__name__)

        def battle() -> dict[str, Any]:
            async def main() -> dict[str, Any]:
                async with HttpTransport(account) as api:
                    runtime = ProtocolRuntime(account=account, username=username, transport=api, trace=trace,
                                              actor=actor, entry=current, state_root=STATE_ROOT, broker=broker)
                    return await runtime.run(auth)
            return asyncio.run(main())

        _battle_started()
        try:
            return await loop.run_in_executor(BATTLE_EXECUTOR, battle)
        finally:
            _battle_finished()

    async def process_message(self, message: Any, *, source: str = "new") -> bool:
        # Sender metadata is optional; resolving it must not consume the entry's
        # short admission window or block an otherwise valid deep link.
        entry = extract_world_boss_entry(message, sender=getattr(message, "sender", None))
        if entry is None:
            # Only a message without a qyz_ deep link can be the settlement
            # post. The entry announcement itself also says "…可查看贡献榜", so
            # a text test alone dropped every live entry (2026-09-21, event
            # 132: 0/6 admitted). Recording the board is read-only and lets the
            # battle report rank us against the top 3.
            if getattr(message, "chat_id", None) in SOURCE_CHAT_TOPICS and "贡献榜" in _message_text(message):
                board = parse_settlement_board(_message_text(message))
                if board and board["rows"]:
                    try:
                        _write_shared_board(message, board)
                    except OSError:
                        pass
            return False
        if not message_recent_enough(message):
            return False
        if entry.fingerprint in self._inflight:
            return False
        retryable_before_mutation = {
            "queued", "preparing_webview", "blocked_source_unavailable", "blocked_source_invalid",
            "blocked_official_verification_required", "blocked_identity_mismatch", "blocked_membership",
        }
        prior = self._event(entry)
        if prior:
            retry_due = (not prior.get("mutation_started") and prior.get("retry_at_ts") is not None
                         and time.time() >= float(prior["retry_at_ts"]))
            if not retry_due and (prior.get("mutation_started") or prior.get("status") not in retryable_before_mutation):
                return False
        self._inflight.add(entry.fingerprint)
        self._record(entry, "queued")
        callback = getattr(self.actor, "world_boss_entry_callback", None)
        if callable(callback):
            callback(message, entry)
        task = asyncio.create_task(self._run_exclusive(entry), name=f"qyz_protocol_{self.account}_{entry.message_id}")
        self._tasks.add(task)
        def done(completed_task: asyncio.Task[Any]) -> None:
            self._tasks.discard(completed_task)
            self._inflight.discard(entry.fingerprint)
            if completed_task.cancelled():
                return
            try:
                exc = completed_task.exception()
            except asyncio.CancelledError:
                return
            if exc:
                self._record(entry, "runtime_error", error="task_exception")
                self.log.error("protocol task failed account=%s error=%s", self.account, type(exc).__name__)
                return
            self._redeliver_later(message, entry)
        task.add_done_callback(done)
        return True

    def _event(self, entry: OfficialWorldBossEntry) -> dict[str, Any]:
        return next((item for item in self.actor.state.get("world_boss_events", [])
                     if item.get("fingerprint") == entry.fingerprint), {})

    def _redeliver_later(self, message: Any, entry: OfficialWorldBossEntry) -> None:
        retry_at = self._event(entry).get("retry_at_ts")
        if retry_at is None or not message_recent_enough(message):
            return

        async def redeliver() -> None:
            await asyncio.sleep(max(0.0, retry_at - time.time()))
            # A newer delivery may have run, failed or started the battle since.
            if self._event(entry).get("retry_at_ts") == retry_at:
                await self.process_message(message, source="entry_retry")

        task = asyncio.create_task(redeliver(), name=f"qyz_retry_{self.account}_{entry.message_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def scan_recent_entries(self, *, source: str = "poll") -> bool:
        handled = False
        for chat_id in self.target_chats:
            message = await latest_source_message(self.client, chat_id)
            if message:
                handled = await self.process_message(message, source=source) or handled
        return handled


@contextmanager
def account_process_lease(account: str):
    if account not in ACCOUNTS:
        raise ProtocolError("account_invalid")
    directory = STATE_ROOT / "accounts" / account
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink() or directory.parent.is_symlink():
        raise ProtocolError("state_path_invalid")
    fd = os.open(directory / "protocol.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ProtocolError("account_process_lease_busy") from None
        yield
    finally:
        os.close(fd)


async def run_account(account: str, stop: asyncio.Event) -> None:
    service = VERIFICATION_SERVICE
    if service is None:
        raise ProtocolError("verifier_not_running")
    with account_process_lease(account):
        service.start_account(account)
        try:
            await _run_account_unlocked(account, stop)
        finally:
            await service.close_account(account)


async def supervise_monitor_loop(account: str, name: str, run, stop: asyncio.Event) -> None:
    """Restart an exited background loop without cancelling this account's battle."""
    while not stop.is_set():
        try:
            await asyncio.create_task(run(), name=f"qyz_{name}_worker_{account}")
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling() or stop.is_set():
                raise
            error = "child_cancelled"
        except Exception as exc:
            error = type(exc).__name__
        else:
            if stop.is_set():
                return
            error = "child_exited"
        logging.warning("QYZ monitor loop restarting account=%s loop=%s error=%s", account, name, error)
        await asyncio.sleep(5)


async def deliver_shared_entry(client, monitor, account, payload, stop, *, now=time.time, sleep=asyncio.sleep):
    """Retry transient read failures within the original entry's admission window.

    Only the existing monitor admits the entry and owns mutation deduplication.
    Successful reads, including an already-in-flight battle, are never replayed.
    """
    try:
        published = datetime.fromisoformat(str(payload.get("published_at") or ""))
        if published.tzinfo is None:
            published = published.replace(tzinfo=BEIJING_TZ)
        deadline = published.timestamp() + ENTRY_RECOVERY_WINDOW_SECONDS
        chat_id, message_id = int(payload.get("chat_id") or 0), int(payload.get("message_id") or 0)
        fingerprint = str(payload.get("fingerprint") or "")
    except (TypeError, ValueError):
        return "invalid_signal"
    if (chat_id not in TARGET_CHAT_IDS or message_id <= 0 or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)
            or published.timestamp() > now() + 30):
        return "invalid_signal"
    delay = 1.0
    while not stop.is_set() and now() < deadline:
        message = None
        try:
            async with asyncio.timeout(min(5.0, max(.01, deadline - now()))):
                await ensure_connected(client)
                message = await client.get_messages(chat_id, ids=message_id)
        except Exception as exc:
            logging.warning("shared entry read retry account=%s error=%s", account, type(exc).__name__)
        if stop.is_set():
            return "stopped"
        if message is not None:
            entry = extract_world_boss_entry(message, sender=getattr(message, "sender", None))
            if (entry is None or entry.message_id != message_id or entry.chat_id != chat_id
                    or entry.fingerprint != fingerprint):
                return "message_mismatch"
            if now() >= deadline or not message_recent_enough(message):
                return "expired"
            await monitor.process_message(message, source="shared_entry")
            return "handled"
        remaining = deadline - now()
        if remaining > 0 and not stop.is_set():
            await sleep(min(delay, remaining))
            delay = min(8.0, delay * 2.0)
    return "stopped" if stop.is_set() else "expired"


async def _run_account_unlocked(account: str, stop: asyncio.Event) -> None:
    humanization = Humanization()
    source = session_source_for(account)
    env = _read_env(source)
    session = _host_session_path(account)
    if not session.exists():
        raise RuntimeError(f"dedicated session missing for {account}")
    client = _telegram_client(str(session), int(env["TG_API_ID"]), env["TG_API_HASH"], request_retries=1, connection_retries=3, retry_delay=2, auto_reconnect=True,
                              # A silent FloodWait sleep in get_messages, GetParticipant or
                              # RequestMainWebView consumed the admission window. Raise it
                              # (as transport.py does) so run_entry redelivers after the wait.
                              flood_sleep_threshold=0)
    try:
        # Through the fleet's connection gate like every reconnect; a failure
        # here is retried with backoff by supervise_account.
        await ensure_connected(client)
        if not await client.is_user_authorized():
            raise RuntimeError(f"session not authorized for {account}")
        actor = AccountActor(account, client)
        actor.my_info = await client.get_me()
        if (int(getattr(actor.my_info, "id", 0) or 0) != int(account.removeprefix("tg"))
                or str(getattr(actor.my_info, "username", "")).casefold() != ACCOUNTS[account]["username"]):
            raise RuntimeError(f"session identity mismatch for {account}")
        if account_scans_telegram_history(account):
            actor.world_boss_entry_callback = _write_shared_entry
        monitor = OfficialWorldBossMonitor(actor, account, logging.getLogger(account))
        await monitor.install()
        if not actor.state.get("world_boss_monitor_active"):
            raise RuntimeError(f"monitor install inactive for {account}")
        await monitor.refresh_health()
        logging.info("QYZ protocol monitor ready account=%s runtime=%s status=%s", account, RUNTIME_VERSION,
                     actor.state.get("world_boss_health", {}).get("status"))

        async def health_loop() -> None:
            while not stop.is_set():
                await asyncio.sleep(humanization.interval(HEALTH_INTERVAL_SECONDS))
                async with asyncio.timeout(55):
                    await monitor.refresh_health()

        async def admission_scan_loop() -> None:
            while not stop.is_set():
                if monitor._inflight:
                    # The entry is being fought; decoding 130 history messages
                    # every 15 s only competes with the battle for the loop.
                    await asyncio.sleep(humanization.interval(5))
                    continue
                try:
                    await monitor.scan_recent_entries(source="admission_poll")
                except Exception as exc:
                    logging.warning("admission scan failed account=%s error=%s", account, type(exc).__name__)
                await asyncio.sleep(humanization.interval(admission_scan_interval_seconds(datetime.now(BEIJING_TZ))))

        async def shared_entry_loop() -> None:
            last_signal: tuple[int, int, str] | None = None
            while not stop.is_set():
                payload = read_json(SHARED_ENTRY_STATE)
                signal_key = (int(payload.get("chat_id") or 0), int(payload.get("message_id") or 0), str(payload.get("fingerprint") or ""))
                if (signal_key != last_signal and signal_key[0] in TARGET_CHAT_IDS and signal_key[1] > 0
                        and re.fullmatch(r"[0-9a-f]{64}", signal_key[2])):
                    await deliver_shared_entry(client, monitor, account, payload, stop)
                    last_signal = signal_key
                await asyncio.sleep(humanization.interval(SHARED_ENTRY_STATE_POLL_SECONDS))

        tasks = [asyncio.create_task(supervise_monitor_loop(account, "shared", shared_entry_loop, stop), name=f"qyz_shared_{account}"),
                 asyncio.create_task(supervise_monitor_loop(account, "health", health_loop, stop), name=f"qyz_health_{account}")]
        if account_scans_telegram_history(account):
            tasks.append(asyncio.create_task(supervise_monitor_loop(account, "scan", admission_scan_loop, stop), name=f"qyz_scan_{account}"))
        try:
            await stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await monitor.stop()
    finally:
        await client.disconnect()


async def supervise_account(account: str, stop: asyncio.Event) -> None:
    """Keep one account's transient startup failure from collapsing the fleet."""
    backoff = 1.0
    humanization = Humanization()
    while not stop.is_set():
        try:
            await run_account(account, stop)
            backoff = 1.0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.error("QYZ protocol account supervisor failed account=%s error=%s", account, type(exc).__name__)
            await asyncio.sleep(humanization.retry_delay(0, base=backoff, cap=30))
            backoff = min(30.0, backoff * 2.0)


async def close_verification_service(service: SerialVerificationService) -> None:
    global VERIFICATION_SERVICE
    try:
        await service.close()
    finally:
        if VERIFICATION_SERVICE is service:
            VERIFICATION_SERVICE = None


async def main_async() -> None:
    global VERIFICATION_SERVICE
    _load_transport_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # otherwise one line per window poll, ~900 per account per battle
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    sys.setswitchinterval(BATTLE_SWITCH_INTERVAL_SECONDS)
    service = build_verification_service()
    VERIFICATION_SERVICE = service
    try:
        await asyncio.gather(*(supervise_account(account, stop) for account in ACCOUNTS))
    finally:
        await close_verification_service(service)


if __name__ == "__main__":
    asyncio.run(main_async())
