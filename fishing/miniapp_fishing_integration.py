#!/usr/bin/env python3
"""Independent, account-isolated XIUXIAN MiniApp fishing automation.

Fishing is a standalone flow: it owns no World Boss behavior, scheduler, or
battle state. Telegram WebView initData remains in memory. An unfinished cast's
server token is kept in a separate private recovery file, never in coordinator
state or CLI output.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import math
import os
import re
import shutil
import sys
import tempfile
import time
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

if __package__:  # imported as fishing.miniapp_fishing_integration
    from . import fishing_accounts
    from .fishing_catch import extract_catch
else:  # launchd runs the fishing scripts from fishing/
    import fishing_accounts
    from fishing_catch import extract_catch

ROOT = Path(__file__).resolve().parent
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))
from miniapp_shared.accounts import (CREDENTIAL_SOURCE, GAME_GROUP, GAME_SECOND_CHAT_ID,  # noqa: E402
                                     accounts_for, feature_options)
# The pinned dwelling entry may use a rotating hantianzunNN_bot.  The bot is
# derived from the validated pinned URL, never guessed from bot history.
from miniapp_shared.errors import DWELLING_BOT_PATTERN, MiniAppError, safe_text, server_error_code  # noqa: E402
from miniapp_shared.dwelling import ENTRY_REJECTED_CODES, first_working_entry  # noqa: E402
from miniapp_shared.priority import yield_to_qyz  # noqa: E402
from miniapp_shared.state import LockBusy, day_key, exclusive_lock, shanghai_now, write_json  # noqa: E402
from transport import disconnect_telegram_client, ensure_connected  # noqa: E402
DEFAULT_CONFIG_PATH = ROOT / "state" / "miniapp_fishing.json"
DEFAULT_STATE_ROOT = ROOT / "state" / "miniapp_fishing"
DEFAULT_SESSION_ROOT = ROOT / "state" / "accounts"
FISHING_API_HOST = "asc.aiopenai.app"
FISHING_API_PREFIX = "/api/miniapp/xianxia-fishing/"
CAVE_API_PREFIX = "/api/miniapp/xianxia-dwelling/"
# WebView redirects commonly use the API base root rather than a UI path;
# validation of the dwelling deep-link itself is done by bot + startapp token.
CAVE_PATH = "/api/miniapp/xianxia-dwelling"
FISHING_PATH = "/api/miniapp/xianxia-fishing"
# This bot is contacted only after a pinned cave entry or cave response is
# validated.  It must never be used as an independent fishing-discovery route.
FISHING_BOT = "fanrenxiuxian_bot"
# Current production game group.  The source's historical `fanrenxxz` default
# is not a resolvable group here; fishing discovery must inspect this group's
# pinned dwelling button, never bot history.
FISHING_PINNED_ENTRY_GROUP = f"@{GAME_GROUP}"
# Canonical public dwelling entry supplied from this group's pinned message.
# Keep only the Telegram message id—not its short-lived startapp credential.
FISHING_PINNED_ENTRY_MESSAGE_ID = 955290
# Telethon's resolved Channel entity may use a different representation than
# GetMessages; this canonical peer preserves access to the forum-root message.
FISHING_PINNED_ENTRY_PEER_ID = GAME_SECOND_CHAT_ID
# Known groups that carry the official 【洞府公共入口】 button. The command
# group and the game group are both inspected; discovery follows whichever
# currently pins/announces the newest entry, so a ~30-day entry rotation
# (bot username, startapp token, or host group) needs no code change.
DWELLING_ENTRY_GROUPS = (FISHING_PINNED_ENTRY_GROUP, FISHING_PINNED_ENTRY_PEER_ID)
DWELLING_ENTRY_NOTICE_MARKERS = ("洞府公共入口", "进入自己的洞府", "公共入口")
DWELLING_ENTRY_SCAN_LIMIT = 300
# Last discovered official entry *message reference* (never its token); used only
# when live discovery finds nothing, e.g. a transient Telegram pinned-filter outage.
DWELLING_ENTRY_CACHE_PATH = ROOT.parent / "state" / "dwelling_entry_cache.json"
FISHING_ENDPOINTS = {
    "start": "start", "shop": "shop", "next": "next", "finish": "finish",
    "result": "result", "buy_bait": "buy-bait", "chum": "chum",
}
_SENSITIVE = ("token", "initdata", "init_data", "session", "hash", "query_id", "signature", "url", "authorization", "cookie", "api_hash", "api_id", "password", "secret")
_ALLOWED_PONDS = {"qingxi", "hantan", "luanxing", "highest_unlocked"}
# ``highest_unlocked``: the pond with the highest proficiency requirement the
# account meets (shop ``ponds[]``: unlocked and currentExp >= requiredExp).
HIGHEST_UNLOCKED_POND = "highest_unlocked"
FALLBACK_POND = "qingxi"
_ALLOWED_BAITS = {"plain", "spirit_rice", "spirit_worm", "demon_blood", "moon"}
_ALLOWED_CHUMS = {"none", "rice", "grass", "demon"}
_OVERRIDE_KEYS = {"pond", "bait", "chum_plan", "daily_cast_limit"}
# Only accounts whose fishing feature sets "special_bait": true may fish with a
# special bait; every other account keeps 凡饵 at every pond, including 灵眼寒潭
# and 乱星海礁 once its proficiency moves it up.
PLAIN_BAIT = "plain"
SPECIAL_BAIT_ACCOUNTS = frozenset(account for account in accounts_for("fishing")
                                  if feature_options(account, "fishing").get("special_bait") is True)


DEFAULT_DAILY_CAST_LIMIT = 5
RESULT_SAMPLE_MAX_CHARS = 3000
# An account whose quota is "server" fishes until /next answers
# fishing_daily_limit_reached; this cap only bounds a runaway local count.
SERVER_LIMIT_SAFETY_CAP = 40


def daily_quota_done(result: dict[str, Any]) -> bool:
    """Whether today's quota is used: a local count, or the server's own refusal."""
    if result.get("server_daily_limit_reached") is True:
        return True
    limit = result.get("daily_cast_limit", DEFAULT_DAILY_CAST_LIMIT)
    count = int(result.get("daily_cast_count", 0) or 0)
    return count >= (SERVER_LIMIT_SAFETY_CAP if limit is None else int(limit))


def merge_confirmed_cast_count(previous, result, *, limit: int | None = DEFAULT_DAILY_CAST_LIMIT):
    """Count each account's official settlement once, including recovered replies.

    ``limit=None`` means the server decides the account's daily quota.
    """
    day = day_key()
    previous = previous if previous.get("settlement_date", day) == day else {}
    known = set(previous.get("counted_settlement_ids", []))
    incoming = set(result.get("confirmed_settlement_ids", [])) if result.get("settlement_date", day) == day else set()
    if result.get("server_result_ready") is True and result.get("settlement_id"):
        incoming.add(result["settlement_id"])
    cap = SERVER_LIMIT_SAFETY_CAP if limit is None else limit
    count = min(cap, max(0, int(previous.get("daily_cast_count", 0))) + len(incoming - known))
    reached = previous.get("server_daily_limit_reached") is True
    if result.get("error_code") == "fishing_daily_limit_reached":
        reached = True
        if limit is not None:
            count = limit
    # Every settled cast's catch, in order, for the day's report.
    catches = [row for row in previous.get("catches") or [] if isinstance(row, dict)]
    catch, settlement = result.get("catch"), result.get("settlement_id")
    if (isinstance(catch, dict) and settlement in incoming
            and not any(row.get("settlement_id") == settlement for row in catches)):
        catches.append({"settlement_id": settlement, **catch})
    merged = {**result, "daily_cast_count": count, "counted_settlement_ids": sorted(known | incoming),
              "settlement_date": day, "daily_cast_limit": limit, "server_daily_limit_reached": reached,
              "catches": catches[-SERVER_LIMIT_SAFETY_CAP:]}
    if "chum" not in merged and isinstance(previous.get("chum"), dict):
        # A reel-in run never reads the shop; keep today's last nest evidence.
        merged["chum"] = previous["chum"]
    return merged


DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    # Enabling the fishing feature for an account is the explicit opt-in.
    "enabled": bool(accounts_for("fishing")),
    "accounts": list(accounts_for("fishing")),
    "auto": {"pond": "qingxi", "bait": PLAIN_BAIT, "chum": "none"},
    "buy_bait": {"enabled": False, "quantity": 10, "when_empty": True},
    "apply_chum": {"enabled": False, "when_selected": True},
    # Per-account pond/bait, a daily chum plan applied in order, and the daily
    # quota (an int, or "server" to fish until the server refuses).
    "account_overrides": {},
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _host_accounts() -> tuple[str, ...]:
    return accounts_for("fishing")


def _json_object(path: Path) -> dict[str, Any]:
    # ponytail: not miniapp_shared.state.read_json, which maps a corrupt file to a
    # malformed_state dict; a corrupt config, pending cast or scope must raise.
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return deepcopy(DEFAULT_CONFIG)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON config: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValueError("fishing config must be a JSON object")
    return value


def _require_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be boolean")
    return value


def load_fishing_config(path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    raw = _json_object(Path(path))
    forbidden = [str(key) for key in raw if any(marker in str(key).lower() for marker in _SENSITIVE)]
    if forbidden:
        raise ValueError("credentials are forbidden in fishing config")
    config = deepcopy(DEFAULT_CONFIG)
    config["enabled"] = _require_bool(raw.get("enabled", config["enabled"]), "enabled")
    accounts = raw.get("accounts", config["accounts"])
    host_accounts = list(_host_accounts())
    if (not isinstance(accounts, list) or not accounts
            or accounts != [account for account in host_accounts if account in accounts]):
        raise ValueError("accounts must be fishing-enabled accounts, in config order")
    config["accounts"] = list(accounts)
    # A legacy top-level "rod_transfer" section (rod trading is retired) is ignored.
    for section in ("auto", "buy_bait", "apply_chum"):
        extra = raw.get(section, {})
        if not isinstance(extra, dict):
            raise ValueError(f"{section} must be an object")
        config[section].update(extra)
    auto = config["auto"]
    if auto["pond"] not in _ALLOWED_PONDS or auto["bait"] not in _ALLOWED_BAITS or auto["chum"] not in _ALLOWED_CHUMS:
        raise ValueError("auto pond, bait, or chum is invalid")
    if auto["bait"] != PLAIN_BAIT:
        raise ValueError("auto.bait must be plain; only special_bait accounts may override their bait")
    for name in ("buy_bait.enabled", "buy_bait.when_empty", "apply_chum.enabled", "apply_chum.when_selected"):
        section, key = name.split(".")
        _require_bool(config[section][key], name)
    if not isinstance(config["buy_bait"]["quantity"], int) or config["buy_bait"]["quantity"] < 1:
        raise ValueError("buy_bait.quantity must be a positive integer")
    overrides = raw.get("account_overrides", {})
    if not isinstance(overrides, dict):
        raise ValueError("account_overrides must be an object")
    for account, override in overrides.items():
        if account not in config["accounts"] or not isinstance(override, dict):
            raise ValueError("account_overrides must name configured accounts")
        unknown = set(override) - _OVERRIDE_KEYS
        if unknown:
            raise ValueError(f"account_overrides.{account} has unknown keys")
        if "pond" in override and override["pond"] not in _ALLOWED_PONDS:
            raise ValueError(f"account_overrides.{account}.pond is invalid")
        if "bait" in override and override["bait"] not in _ALLOWED_BAITS:
            raise ValueError(f"account_overrides.{account}.bait is invalid")
        if override.get("bait", PLAIN_BAIT) != PLAIN_BAIT and account not in SPECIAL_BAIT_ACCOUNTS:
            raise ValueError(f"account_overrides.{account}.bait must be plain; set fishing.special_bait to override it")
        plan = override.get("chum_plan", [])
        if (not isinstance(plan, list) or len(plan) > 5
                or any(key not in _ALLOWED_CHUMS or key == "none" for key in plan)):
            raise ValueError(f"account_overrides.{account}.chum_plan is invalid")
        limit = override.get("daily_cast_limit", DEFAULT_DAILY_CAST_LIMIT)
        if limit != "server" and (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= SERVER_LIMIT_SAFETY_CAP):
            raise ValueError(f"account_overrides.{account}.daily_cast_limit is invalid")
    config["account_overrides"] = deepcopy(overrides)
    return config


def account_options(config: dict[str, Any], account: str) -> dict[str, Any]:
    """The options one account runs with: shared config plus its override."""
    options = deepcopy(config)
    override = dict(config.get("account_overrides", {}).get(account) or {})
    options["auto"] = {**options.get("auto", {}), **{key: override[key] for key in ("pond", "bait") if key in override}}
    options["chum_plan"] = list(override.get("chum_plan", []))
    limit = override.get("daily_cast_limit", DEFAULT_DAILY_CAST_LIMIT)
    options["daily_cast_limit"] = None if limit == "server" else int(limit)
    options.pop("account_overrides", None)
    return options


def _safe(value: Any) -> Any:
    """Remove secrets before values enter JSON state or CLI results."""
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items() if not any(mark in str(k).lower() for mark in _SENSITIVE)}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    if isinstance(value, str):
        # The shared redactor also hides URLs and startapp/signature values.
        return safe_text(value, 240)
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    write_json(path, _safe(value))


@contextmanager
def _account_lock(lock_root: Path, account: str):
    with ExitStack() as stack:
        try:
            stack.enter_context(exclusive_lock(Path(lock_root) / f"{account}.lock"))
        except LockBusy as exc:
            raise RuntimeError("account_operation_locked") from exc
        yield


def _extract_init_data(url: str) -> str:
    parsed = urlparse(str(url or ""))
    for part in (parsed.fragment, parsed.query):
        values = parse_qs(part, keep_blank_values=True).get("tgWebAppData", [])
        if values:
            return values[0]
    return ""


def build_fishing_proof(challenge: Any) -> dict[str, Any]:
    """Reproduce the source's bounded V2 reel control, not a fabricated score."""
    data = dict(challenge or {})
    challenge_id = str(data.get("challengeId") or "").strip()
    if not challenge_id:
        raise RuntimeError("fishing_challenge_missing")
    try:
        low, high = float(data.get("targetLow", 41)), float(data.get("targetHigh", 68))
        fish_power = max(0.1, float(data.get("fishPower", 1.7)))
        minimum = max(0, int(data.get("minDurationMs", 5200)))
        maximum = max(minimum + 1000, int(data.get("maxDurationMs", 70000)))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("fishing_challenge_invalid") from exc
    if high <= low + 2:
        raise RuntimeError("fishing_challenge_band_invalid")
    # Match JavaScript charCodeAt (UTF-16 code units), including non-BMP seeds.
    raw_seed = str(data.get("fishSeed") or "seed").encode("utf-16-le", errors="surrogatepass")
    seed = sum(int.from_bytes(raw_seed[i:i + 2], "little") for i in range(0, len(raw_seed), 2)) / 19
    tension, progress, elapsed, holding = (low + high) / 2 - 8, 0.0, 0, False
    width = high - low; hold_below, release_above = low + width * .54, low + width * .72
    events: list[dict[str, Any]] = []
    while elapsed < maximum and (progress < 100 or elapsed < minimum):
        desired = tension < low or (holding and tension < release_above) or (not holding and tension <= hold_below)
        if tension > high:
            desired = False
        if desired != holding:
            holding = desired; events.append({"t": elapsed + 20, "holding": holding})
        elapsed += 20
        pulse = math.sin(elapsed * .0027 * fish_power + seed)
        surge = max(0.0, math.sin(elapsed * .0041 + seed * 1.7))
        pull = fish_power * (.72 + pulse * .24 + surge * .42)
        tension += ((24 + pull * 3.1) if holding else (pull * 4.8 - 24)) * .02
        tension += math.sin(elapsed * .012 + seed) * .24
        tension = max(0.0, min(100.0, tension))
        if low <= tension <= high:
            progress += (8.2 + fish_power * .7 + (2.2 if holding else .5)) * .02
        elif tension > high:
            progress -= (1.5 + fish_power * .25) * .02
        else:
            progress -= .9 * .02
        if holding and tension < low:
            progress += 1.1 * .02
        progress = max(0.0, min(100.0, progress))
    if progress < 100:
        raise RuntimeError("fishing_proof_not_landed")
    return {"mode": "xianxiaFishingV2", "challengeId": challenge_id, "durationMs": elapsed, "events": events}


def _extract_start_param(url: str) -> str:
    parsed = urlparse(str(url or ""))
    for part in (parsed.fragment, parsed.query):
        query = parse_qs(part, keep_blank_values=True)
        for key in ("tgWebAppStartParam", "startapp", "start_param"):
            if query.get(key):
                return str(query[key][0])
    return ""


def extract_pinned_dwelling_url(message: Any, *, buttons_only: bool = False) -> str:
    """Find the canonical dwelling deep link in a pinned Telegram message.

    Source deployments place the link either in a URL button or in visible
    pinned text; both forms must be accepted, while all other links are ignored.
    ``buttons_only`` restricts the search to inline URL buttons, which only bots
    can attach: it is the authenticity check for *unpinned* entry notices.
    """
    candidates: list[str] = []
    if not buttons_only:
        candidates.append(str(getattr(message, "raw_text", "") or getattr(message, "message", "") or ""))
        for entity in getattr(message, "entities", None) or ():
            if getattr(entity, "url", None):
                candidates.append(str(entity.url))
    markup = getattr(message, "reply_markup", None)
    for row in getattr(markup, "rows", None) or ():
        for button in getattr(row, "buttons", None) or ():
            if getattr(button, "url", None):
                candidates.append(str(button.url))
    for raw in candidates:
        for url in re.findall(r"https?://[^\s<>\"'\]\)}]+", raw):
            parsed = urlparse(url)
            token = _extract_start_param(url)
            if (
                parsed.scheme == "https"
                and (parsed.hostname or "").lower() in {"t.me", "telegram.me"}
                and DWELLING_BOT_PATTERN.fullmatch(parsed.path.strip("/")) is not None
                and re.fullmatch(r"[A-Za-z0-9_-]{4,160}", token or "")
            ):
                return f"https://t.me/{parsed.path.strip('/')}?startapp={token}"
    return ""


def _read_entry_cache(path: Path | None = None) -> dict[str, Any]:
    try:
        data = json.loads(Path(path or DWELLING_ENTRY_CACHE_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_entry_cache(data: dict[str, Any], path: Path | None = None) -> None:
    """Best effort; the cache only ever holds a message reference, never a token."""
    try:
        path = Path(path or DWELLING_ENTRY_CACHE_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".entry-cache-", delete=False) as stream:
            tmp = Path(stream.name)
            try:
                json.dump(data, stream, ensure_ascii=False, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
                os.replace(tmp, path)
            finally:
                tmp.unlink(missing_ok=True)
    except OSError:
        return


class UpstreamFishingProtocol:
    """Narrow real bridge using this host's dedicated Telethon account sessions.

    All endpoint dispatch is allowlisted. An unconfirmed cast is reconciled
    before retrying its finish at the allowed interval. Credentials stay in
    memory; only challenge IDs and business evidence are durable.
    """
    def __init__(self, *, session_root: Path = DEFAULT_SESSION_ROOT, lock_root: Path | None = None,
                 session_copy_root: Path | None = None, client_factory: Callable[..., Any] | None = None,
                 http_transport: Callable[[dict[str, Any]], Any] | None = None,
                 pinned_cave_entry: Callable[[Any], Any] | None = None,
                 cave_webview_request: Callable[[Any, dict[str, str]], Any] | None = None,
                 session_exists: Callable[[Path], bool] | None = None,
                 env_loader: Callable[[str], dict[str, str]] | None = None,
                 yield_check: Callable[[float], dict[str, Any] | None] | None = None) -> None:
        self.session_root = Path(session_root)
        self._chum_reports: dict[str, dict[str, Any]] = {}
        self._ponds: dict[str, str] = {}
        self.lock_root = Path(lock_root) if lock_root else self.session_root.parent / "miniapp_fishing_locks"
        # Never hand the QYZ monitor's live SQLite session to a second Telethon
        # client. Each bridge action uses a short-lived private file copy.
        self.session_copy_root = Path(session_copy_root) if session_copy_root else self.lock_root / "session_copies"
        self.client_factory = client_factory
        self.http_transport = http_transport
        self._http_session = None
        # Dependencies are intentionally narrow. The caller must provide a
        # canonical *pinned group* cave entry; arbitrary bot WebViews are not a
        # discovery fallback.
        self.pinned_cave_entry = pinned_cave_entry
        self.cave_webview_request = cave_webview_request
        self.session_exists = session_exists or Path.exists
        self.env_loader = env_loader
        # 青元子 priority, checked again before every new cast: the tick-start
        # check alone let a staggered or slow login start a cast inside the window.
        self.yield_check = yield_check or (lambda now_ts: yield_to_qyz("miniapp_fishing", now_ts, ROOT.parent))

    def _source_env(self, account: str) -> dict[str, str]:
        if account not in _host_accounts():
            raise ValueError("unknown account")
        if self.env_loader:
            return self.env_loader(account)
        return fishing_accounts.read_account_env(CREDENTIAL_SOURCE[account])

    def _client(self, account: str) -> tuple[Any, Path]:
        session = self.session_root / account / "session.session"
        if not self.session_exists(session):
            raise RuntimeError("dedicated_session_missing")
        # Telethon expects a basename and appends '.session'. Use a random
        # temporary directory so each action gets an isolated, disposable copy.
        self.session_copy_root.mkdir(parents=True, exist_ok=True)
        fishing_accounts.sweep_stale_session_copies(self.session_copy_root)
        copy_dir = Path(tempfile.mkdtemp(prefix=f"{account}-", dir=self.session_copy_root))
        try:  # the copy holds the auth key: a bad env or client option must not strand it
            copy_base = copy_dir / "session"
            shutil.copy2(session, copy_base.with_suffix(".session"))
            env = self._source_env(account)
            if self.client_factory:
                return self.client_factory(session=str(copy_base), api_id=int(env["TG_API_ID"]), api_hash=env["TG_API_HASH"]), copy_dir
            return fishing_accounts.telegram_client(str(copy_base), int(env["TG_API_ID"]), env["TG_API_HASH"], request_retries=1, connection_retries=1, retry_delay=1), copy_dir
        except BaseException:
            shutil.rmtree(copy_dir, ignore_errors=True)
            raise

    @staticmethod
    def _canonical_launch(value: Any, *, path: str, token_prefix: str, error: str) -> dict[str, str]:
        item = value if isinstance(value, dict) else {"url": value}
        raw_url = str(item.get("url") or item.get("webview_url") or "").strip()
        parsed = urlparse(raw_url)
        host = (parsed.hostname or "").lower()
        token = _extract_start_param(raw_url)
        entry_bot = parsed.path.strip("/").lower()
        is_dwelling_deep_link = (
            path == CAVE_PATH
            and host in {"t.me", "telegram.me"}
            and DWELLING_BOT_PATTERN.fullmatch(entry_bot) is not None
        )
        is_app_url = host == FISHING_API_HOST and parsed.path.rstrip("/") == path
        if (parsed.scheme != "https" or not (is_dwelling_deep_link or is_app_url)
                or not re.fullmatch(rf"{token_prefix}[A-Za-z0-9_-]{{4,160}}", token)):
            raise RuntimeError(error)
        return {"url": raw_url, "token": token, "bot_username": entry_bot if is_dwelling_deep_link else FISHING_BOT}

    async def _call(self, callback: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        value = callback(*args, **kwargs)
        return await value if inspect.isawaitable(value) else value

    async def _dwelling_entry_candidates(self, client: Any, *, refresh: bool = False) -> list[dict[str, Any]]:
        """Official dwelling entries, newest first; cache-first unless ``refresh``.

        The official entry is a `【洞府公共入口】` button on a rotating
        `hantianzunNN_bot`; it is re-pinned / re-announced (roughly every 30 days)
        in one of the known game groups. Discovery never guesses from bot history.
        Without ``refresh`` the last working entry's message is re-read by id
        (one RPC per login instead of ~14); callers come back with
        ``refresh=True`` when that entry is missing or its token is rejected:

        1. pinned messages of every configured group (curated by admins);
        2. recent public-entry notices in those groups, accepted only when the
           link sits in an inline URL button (only bots can attach one);
        3. the last discovered entry message reference and the legacy fixed id,
           as fallbacks for a transient Telegram pinned-filter outage.

        Each item carries the canonical launch (``url``/``token``/``bot_username``)
        plus its message reference. Callers try the newest first and fall through
        to the next one only when the server rejects the token, so a rotated bot
        username, startapp token or host group is followed automatically. A
        FloodWait is raised, not skipped: scanning on would only extend it.
        """
        from telethon import functions, types
        from telethon.errors import FloodError

        groups: list[Any] = []
        env_group = str(os.environ.get("FISHING_CAVE_GROUP") or "").strip()
        if env_group:
            groups.append(env_group)
        for group in DWELLING_ENTRY_GROUPS:
            if group not in groups:
                groups.append(group)

        candidates: list[dict[str, Any]] = []
        seen: set[tuple[Any, int]] = set()

        def _remember(message: Any, *, peer: Any, source: str, buttons_only: bool = False) -> None:
            if message is None:
                return
            url = extract_pinned_dwelling_url(message, buttons_only=buttons_only)
            if not url:
                return
            message_id = int(getattr(message, "id", 0) or 0)
            if (peer, message_id) in seen:
                return
            seen.add((peer, message_id))
            date = getattr(message, "date", None)
            try:
                ts = float(date.timestamp()) if date is not None else 0.0
            except (AttributeError, OSError, OverflowError, ValueError):
                ts = 0.0
            try:
                launch = self._canonical_launch({"url": url}, path=CAVE_PATH, token_prefix="",
                                                error="pinned_cave_entry_invalid")
            except RuntimeError:
                return
            candidates.append({**launch, "ts": ts, "peer": peer, "message_id": message_id, "source": source})

        async def _read_by_id(peer: Any, message_id: Any, source: str) -> None:
            if not peer or not message_id:
                return
            try:
                _remember(await client.get_messages(int(peer), ids=int(message_id)), peer=int(peer), source=source)
            except FloodError:
                raise
            except Exception:
                return

        cached = _read_entry_cache()
        if not refresh:
            await _read_by_id(cached.get("peer_id"), cached.get("message_id"), "cache")
            if candidates:
                return candidates

        for group in groups:
            try:
                entity = await client.get_entity(group)
            except FloodError:
                raise
            except Exception:
                continue
            peer_id = getattr(entity, "id", None)
            peer_ref: Any = int(f"-100{peer_id}") if isinstance(peer_id, int) and peer_id > 0 else group
            # 1. curated pins: the filter (plain groups) and the channel-level pin (forum groups).
            try:
                pinned = await client.get_messages(entity, limit=50, filter=types.InputMessagesFilterPinned())
                for message in (pinned if isinstance(pinned, (list, tuple)) else [pinned]):
                    _remember(message, peer=peer_ref, source="pinned")
            except FloodError:
                raise
            except Exception:
                pass
            try:
                full = await client(functions.channels.GetFullChannelRequest(entity))
                pinned_id = int(getattr(getattr(full, "full_chat", None), "pinned_msg_id", 0) or 0)
                if pinned_id:
                    _remember(await client.get_messages(entity, ids=pinned_id), peer=peer_ref, source="pinned")
            except FloodError:
                raise
            except Exception:
                pass
            # 2. recent bot-issued notices back up a stale/absent pin. Space the
            # GetHistory pages as transport.collect_recent_messages does.
            try:
                recent = await client.get_messages(entity, limit=DWELLING_ENTRY_SCAN_LIMIT,
                                                   wait_time=1 if DWELLING_ENTRY_SCAN_LIMIT > 100 else 0)
                for message in recent or []:
                    text = str(getattr(message, "raw_text", "") or getattr(message, "message", "") or "")
                    if any(marker in text for marker in DWELLING_ENTRY_NOTICE_MARKERS):
                        _remember(message, peer=peer_ref, source="notice", buttons_only=True)
            except FloodError:
                raise
            except Exception:
                pass

        if not candidates:
            # 3. fallbacks: the last discovered official message, then the legacy fixed id.
            await _read_by_id(cached.get("peer_id"), cached.get("message_id"), "cache")
            if not candidates:
                await _read_by_id(FISHING_PINNED_ENTRY_PEER_ID, FISHING_PINNED_ENTRY_MESSAGE_ID, "legacy")

        candidates.sort(key=lambda item: item["ts"], reverse=True)
        return candidates

    @staticmethod
    def _remember_entry(candidate: dict[str, Any]) -> None:
        """Persist the official entry *message reference* (never its token) as a fallback."""
        if candidate.get("source") in {"pinned", "notice"} and isinstance(candidate.get("peer"), int):
            _write_entry_cache({"peer_id": candidate["peer"], "message_id": candidate.get("message_id"),
                                "source": candidate["source"], "bot_username": candidate.get("bot_username"),
                                "message_ts": candidate.get("ts"), "discovered_at": time.time()})

    async def _pinned_cave_entry(self, client: Any) -> Any:
        """The newest visible official dwelling entry (see ``_dwelling_entry_candidates``)."""
        if self.pinned_cave_entry is not None:
            return self._canonical_launch(await self._call(self.pinned_cave_entry, client),
                                          path=CAVE_PATH, token_prefix="", error="pinned_cave_entry_invalid")
        candidates = await self._dwelling_entry_candidates(client)
        if not candidates:
            raise RuntimeError("pinned_cave_entry_missing")
        best = candidates[0]
        self._remember_entry(best)
        return {"url": best["url"], "token": best["token"], "bot_username": best["bot_username"]}

    async def _cave_webview(self, client: Any, entry: dict[str, str]) -> str:
        if self.cave_webview_request is not None:
            raw_url = await self._call(self.cave_webview_request, client, entry)
        else:
            from telethon import functions
            entry_bot = str(entry.get("bot_username") or "").strip()
            if DWELLING_BOT_PATTERN.fullmatch(entry_bot) is None:
                raise RuntimeError("cave_entry_bot_not_allowed")
            bot = await client.get_entity(entry_bot)
            bot_input = await client.get_input_entity(bot)
            raw_url = (await client(functions.messages.RequestMainWebViewRequest(
                peer=bot_input, bot=bot_input, platform="android", start_param=entry["token"]
            ))).url
        parsed = urlparse(str(raw_url or ""))
        if parsed.scheme != "https" or (parsed.hostname or "").lower() != FISHING_API_HOST:
            raise RuntimeError("cave_webview_host_not_allowed")
        init_data = _extract_init_data(str(raw_url))
        if not init_data:
            raise RuntimeError("cave_webview_init_data_missing")
        return init_data

    async def _cave_snapshot(self, *, init_data: str, token: str) -> dict[str, Any]:
        """Call the authenticated dwelling /start endpoint before /external."""
        request = {
            "method": "POST",
            "url": f"https://{FISHING_API_HOST}{CAVE_API_PREFIX}start",
            "payload": {"token": token, "initData": init_data},
        }
        if self.http_transport is not None:
            response = self.http_transport(request)
            response = await response if inspect.isawaitable(response) else response
            if not isinstance(response, dict) or not response.get("ok"):
                raise RuntimeError(server_error_code(response.get("data") if isinstance(response, dict) else None,
                                                     "cave_start_failed"))
            payload = response.get("data")
        else:
            import requests
            response = requests.post(request["url"], json=request["payload"], timeout=(5, 20), headers={"User-Agent": "Mozilla/5.0"})
            try:
                payload = response.json()
            except ValueError as exc:
                if not response.ok:
                    raise RuntimeError(f"cave_start_http_{response.status_code}") from exc
                raise RuntimeError("cave_snapshot_invalid") from exc
            if not response.ok:
                raise RuntimeError(server_error_code(payload, f"cave_start_http_{response.status_code}"))
        if not isinstance(payload, dict):
            raise RuntimeError("cave_snapshot_invalid")
        if payload.get("ok") is False:  # a 200 rejection (e.g. a rotated entry token)
            raise RuntimeError(server_error_code(payload, "cave_start_failed"))
        return payload

    async def _fishing_token_from_dwelling(self, *, init_data: str, token: str, player_id: Any = None) -> str:
        """Mint the fish_ token from the initialized dwelling external action."""
        payload = {"token": token, "initData": init_data, "action": "fishing"}
        if player_id is not None:
            payload["playerId"] = int(player_id)
        request = {
            "method": "POST",
            "url": f"https://{FISHING_API_HOST}{CAVE_API_PREFIX}external",
            "payload": payload,
        }
        if self.http_transport is not None:
            response = self.http_transport(request)
            response = await response if inspect.isawaitable(response) else response
            if not isinstance(response, dict):
                raise RuntimeError("dwelling_external_failed")
            payload = response.get("data")
            if not response.get("ok"):
                code = _safe(str((payload or {}).get("error") or (payload or {}).get("code") or response.get("error") or "dwelling_external_failed")) if isinstance(payload, dict) else _safe(str(response.get("error") or "dwelling_external_failed"))
                raise RuntimeError(code)
        else:
            import requests
            response = requests.post(request["url"], json=request["payload"], timeout=(5, 20), headers={"User-Agent": "Mozilla/5.0"})
            try:
                payload = response.json()
            except ValueError as exc:
                raise RuntimeError("dwelling_external_invalid") from exc
            if not response.ok:
                code = _safe(str(payload.get("error") or payload.get("code") or "dwelling_external_failed")) if isinstance(payload, dict) else "dwelling_external_failed"
                raise RuntimeError(code)
        raw_url = str(payload.get("url") or "") if isinstance(payload, dict) else ""
        # Source deployment returns either a full Telegram deep-link/WebView URL
        # or the relative `/miniapp/xianxia-fishing?startapp=fish_*` route.
        if raw_url.startswith("/"):
            raw_url = f"https://{FISHING_API_HOST}{raw_url}"
        parsed = urlparse(raw_url)
        fish_token = _extract_start_param(raw_url)
        # The live `/external` response may be a direct fishing WebView under
        # an allowed game bot, or the canonical bot deep link.  Accept only a
        # valid fish_ token and record no URL/token in the failure surface.
        host = (parsed.hostname or "").lower()
        bot = parsed.path.strip("/").lower()
        acceptable_host = host in {"t.me", "telegram.me", FISHING_API_HOST}
        acceptable_bot = host == FISHING_API_HOST or DWELLING_BOT_PATTERN.fullmatch(bot) is not None
        if (
            parsed.scheme != "https"
            or not acceptable_host
            or not acceptable_bot
            or not re.fullmatch(r"fish_[A-Za-z0-9_-]{4,160}", fish_token)
        ):
            raise RuntimeError("dwelling_fishing_launch_invalid")
        return fish_token

    @staticmethod
    def _selected_player_id(cave_payload: Any) -> int:
        """Extract only the current account's selected dwelling identity."""
        roots = [cave_payload]
        if isinstance(cave_payload, dict) and isinstance(cave_payload.get("data"), dict):
            roots.append(cave_payload["data"])
        for root in roots:
            if not isinstance(root, dict):
                continue
            identity = root.get("identity") if isinstance(root.get("identity"), dict) else {}
            value = identity.get("selectedPlayerId") or root.get("selectedPlayerId")
            try:
                selected = int(value)
            except (TypeError, ValueError):
                continue
            if selected > 0:
                return selected
        raise RuntimeError("dwelling_selected_player_missing")

    async def _authenticated(self, account: str, *, prefer_pending: bool = True) -> tuple[Any, str, str, Path]:
        client, copy_dir = self._client(account)
        try:
            await ensure_connected(client)  # the fleet's bounded handshake gate
            if not await client.is_user_authorized():
                raise RuntimeError("session_not_authorized")
            me = await client.get_me()
            if int(getattr(me, "id", 0) or 0) != int(account.removeprefix("tg")):
                raise RuntimeError("session_identity_mismatch")
            pending_token = self._pending_scope_token(account) if prefer_pending else None

            async def fish_scope(_entry: dict[str, Any], token: str, init_data: str) -> tuple[str, str]:
                if pending_token:
                    # Keep the original cast scope across process restarts. Minting
                    # a new dwelling/external token cannot recover its old result.
                    return init_data, pending_token
                # The source route initializes the dwelling, then mints the fish_
                # token through dwelling/external. No fishing-specific WebView exists.
                try:
                    cave_payload = await self._cave_snapshot(init_data=init_data, token=token)
                except RuntimeError as exc:
                    code = str(exc).removeprefix("cave_start_")
                    if code in ENTRY_REJECTED_CODES:  # a rotated entry: try the next one
                        raise MiniAppError(code) from exc
                    raise
                player_id = self._selected_player_id(cave_payload)
                return init_data, await self._fishing_token_from_dwelling(
                    init_data=init_data, token=token, player_id=player_id)

            init_data, fish_token = await first_working_entry(self, client, fish_scope)
            return client, init_data, fish_token, copy_dir
        except BaseException:
            try:
                await self._close_client(client, copy_dir)
            except Exception:
                pass
            raise

    @staticmethod
    async def _close_client(client: Any, copy_dir: Path) -> None:
        try:
            await disconnect_telegram_client(client)
        finally:
            shutil.rmtree(copy_dir, ignore_errors=True)

    def _request(self, endpoint: str, *, token: str, init_data: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if endpoint not in FISHING_ENDPOINTS:
            raise ValueError("fishing endpoint not allowed")
        request = {"method": "POST", "url": f"https://{FISHING_API_HOST}{FISHING_API_PREFIX}{FISHING_ENDPOINTS[endpoint]}",
                   "payload": {"token": token, "initData": init_data, **dict(payload or {})}}
        try:
            if self.http_transport:
                envelope = self.http_transport(request)
                if not isinstance(envelope, dict):
                    raise ValueError("invalid_http_transport_response")
            else:
                import requests
                sender = self._http_session or requests
                response = sender.post(request["url"], json=request["payload"], timeout=(5, 20), headers={"User-Agent": "Mozilla/5.0"})
                data = response.json()
                envelope = {"ok": bool(response.ok), "status_code": response.status_code, "data": data,
                            "error": "" if response.ok else f"http_{response.status_code}"}
        except Exception as exc:
            return {"ok": False, "status_code": 0, "error": "response_unconfirmed", "error_type": type(exc).__name__}
        data = envelope.get("data")
        if not isinstance(data, dict):
            return {**envelope, "ok": False, "error": "response_invalid"}
        if data.get("ok") is False or data.get("success") is False or data.get("error"):
            return {**envelope, "ok": False, "explicit_rejection": True,
                    "error": str(data.get("error") or data.get("code") or "business_rejected")}
        return envelope

    def _pending_path(self, account):
        return self.lock_root / "commands" / account / "pending.json"

    def _scope_path(self, account):
        return self._pending_path(account).with_name("pending_scope.json")

    def _pending_scope_token(self, account):
        pending_path = self._pending_path(account)
        pending = _json_object(pending_path) if pending_path.exists() else {}
        path = self._scope_path(account)
        if not pending.get("pending") or not path.exists():
            return None
        scope = _json_object(path)
        if pending.get("account") != account or scope.get("account") != account:
            raise ValueError("fishing recovery scope account mismatch")
        if scope.get("challenge_id") != pending.get("challenge_id"):
            return None
        token = scope.get("token")
        if scope.get("version") != 1 or not isinstance(token, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,256}", token) is None:
            raise ValueError("invalid fishing recovery scope")
        return token

    def _save_scope(self, account, challenge_id, token):
        path = self._scope_path(account)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # NamedTemporaryFile creates mode 0600 before any credential is written.
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".pending-scope-", delete=False) as handle:
            temporary = Path(handle.name)
            try:
                json.dump({"version": 1, "account": account, "challenge_id": challenge_id,
                           "token": token}, handle)
                handle.flush()
                os.fsync(handle.fileno())
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)

    def _archive_unavailable_result(self, account, pending, phase):
        evidence = list(pending.get("unconfirmed_results") or [])
        evidence.append({"challenge_id": pending["challenge_id"], "status": "unconfirmed",
                         "reason": "fishing_token_scope", "official_phase": phase,
                         "observed_at_ts": time.time(), "previous_status": pending.get("status"),
                         "previous_updated_at_ts": pending.get("updated_at_ts")})
        self._save_pending(account, pending=False, status="result_scope_unavailable",
                           unconfirmed_results=evidence[-40:])
        self._scope_path(account).unlink(missing_ok=True)

    def _save_pending(self, account, **updates):
        path = self._pending_path(account)
        state = _json_object(path) if path.exists() else {}
        if state and state.get("account") != account:
            raise ValueError("fishing pending account mismatch")
        state.update(account=account, updated_at_ts=time.time(), **updates)
        _write_json(path, state)
        return state

    @staticmethod
    def _ready_result(response, challenge_id, *, scope_challenge_id=None):
        data = response.get("data") if isinstance(response.get("data"), dict) else {}
        result = data.get("result") if isinstance(data.get("result"), dict) else data
        ident = result.get("challengeId") or data.get("challengeId")
        if ident and str(ident) != str(challenge_id):
            return None
        # Some same-session results omit the challenge ID. A fresh WebView
        # may expose a different cast, so its ready flag alone cannot settle
        # a previous process's pending cast.
        if not ident and str(scope_challenge_id or "") != str(challenge_id):
            return None
        if response.get("ok") and (data.get("ready") is True or result.get("ready") is True):
            return result
        return None

    def _record_settlement(self, account, challenge_id, result, steps, finish_attempts, *, response=None, diagnostics=None):
        path = self._pending_path(account)
        state = _json_object(path) if path.exists() else {}
        evidence = list(state.get("settlements", []))
        day = day_key()
        key = f"{account}:{challenge_id}"
        if not any(row.get("settlement_id") == key for row in evidence):
            evidence.append({"settlement_id": key, "day": day, "confirmed_at_ts": time.time()})
        payload = {"status": "settled_reconciled", "settlement_id": key, "server_result_ready": True,
                   "steps": steps, "finish_attempts": finish_attempts, "caught": result.get("caught")}
        diagnostics = diagnostics if diagnostics is not None else state.get("cast_diagnostics")
        if isinstance(diagnostics, dict):
            payload["cast_diagnostics"] = dict(diagnostics)
        # What this cast caught, for the day's report. The reply shape is not
        # documented, so a redacted copy goes to runs.jsonl as evidence.
        reply = response.get("data") if isinstance(response, dict) and isinstance(response.get("data"), dict) else result
        payload["catch"] = {**extract_catch(reply),
                            "at": shanghai_now().strftime("%H:%M")}
        sample = json.dumps(_safe(reply), ensure_ascii=False)
        payload["result_sample"] = sample if len(sample) <= RESULT_SAMPLE_MAX_CHARS else sample[:RESULT_SAMPLE_MAX_CHARS] + "…"
        self._save_pending(account, pending=False, status="settled", settlements=evidence[-40:], last_result=payload)
        self._scope_path(account).unlink(missing_ok=True)
        return payload

    @staticmethod
    def _confirmed(data: dict[str, Any]) -> bool:
        values = [data, data.get("result") if isinstance(data.get("result"), dict) else {}]
        return any(item.get("confirmed") is True or item.get("success") is True or item.get("ok") is True for item in values)

    @staticmethod
    def _bait(shop_data: dict[str, Any], bait_key: str) -> dict[str, Any]:
        shop = shop_data.get("shop") if isinstance(shop_data.get("shop"), dict) else shop_data
        for bait in shop.get("baits", []) if isinstance(shop, dict) else []:
            if not isinstance(bait, dict) or str(bait.get("key") or bait.get("itemId") or "") != bait_key:
                continue
            # Catalog presence is not inventory.  A zero-count bait must take
            # the guarded buy path, never be passed to /next as if usable.
            try:
                count = int(bait.get("count", bait.get("quantity", 0)) or 0)
            except (TypeError, ValueError):
                count = 0
            if count > 0:
                return bait
        return {}

    @staticmethod
    def _shop_body(shop_data: dict[str, Any]) -> dict[str, Any]:
        shop = shop_data.get("shop") if isinstance(shop_data.get("shop"), dict) else shop_data
        return shop if isinstance(shop, dict) else {}

    @classmethod
    def select_pond(cls, shop_data: dict[str, Any], configured: str) -> str:
        """The configured pond, or the highest one this account's proficiency meets."""
        if configured != HIGHEST_UNLOCKED_POND:
            return configured
        best, best_required = FALLBACK_POND, -1
        for pond in cls._shop_body(shop_data).get("ponds", []):
            if not isinstance(pond, dict) or str(pond.get("key") or "") not in _ALLOWED_PONDS:
                continue
            try:
                required, current = int(pond.get("requiredExp", 0) or 0), int(pond.get("currentExp", 0) or 0)
            except (TypeError, ValueError):
                continue
            if pond.get("unlocked") is True and current >= required and required > best_required:
                best, best_required = str(pond["key"]), required
        return best

    @classmethod
    def next_planned_chum(cls, shop_data: dict[str, Any], plan: list[str]) -> tuple[str, str]:
        """``(chum_key, reason)`` for the plan step due now, from server counters only.

        Plan step *n* of a key is done once the server's ``usedToday`` for that
        key reaches *n*; a nest is laid only when none is active. Restarts,
        lost replies and manual play therefore never lay an extra nest.
        """
        shop = cls._shop_body(shop_data)
        active = shop.get("activeChum") if isinstance(shop.get("activeChum"), dict) else {}
        if active and int(active.get("remaining", 0) or 0) > 0:
            return "", "active"
        chums = {str(item.get("key")): item for item in shop.get("chums", []) if isinstance(item, dict)}
        seen: dict[str, int] = {}
        for key in plan:
            seen[key] = seen.get(key, 0) + 1
            chum = chums.get(key)
            if chum is None:
                return key, "chum_missing"
            if int(chum.get("usedToday", 0) or 0) >= seen[key]:
                continue
            if int(chum.get("remainingToday", 0) or 0) <= 0:
                continue
            return key, "due"
        return "", "plan_complete"

    def _chum_usage(self, shop_data: dict[str, Any]) -> dict[str, Any]:
        shop = self._shop_body(shop_data)
        active = shop.get("activeChum") if isinstance(shop.get("activeChum"), dict) else {}
        return {"used_today": {str(item.get("key")): int(item.get("usedToday", 0) or 0)
                               for item in shop.get("chums", []) if isinstance(item, dict)},
                "active": {"key": str(active.get("key") or ""), "remaining": int(active.get("remaining", 0) or 0)}
                if active else {}}

    def _apply_chum_plan(self, *, token: str, init_data: str, shop_data: dict[str, Any],
                         options: dict[str, Any], steps: list[str]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Lay the due nest of ``options["chum_plan"]`` before a cast.

        Missing bait that the nest consumes (灵草窝 takes 3 灵米饵) is bought
        first. Returns the refreshed shop and a report; report ``status`` is
        ``chum_unconfirmed`` when the server's counters cannot confirm a sent
        nest, and the caller must not cast in that run.
        """
        plan = list(options.get("chum_plan") or [])
        key, reason = self.next_planned_chum(shop_data, plan)
        if not key or reason != "due":
            return shop_data, {"status": reason, **self._chum_usage(shop_data)}
        shop = self._shop_body(shop_data)
        chum = next(item for item in shop.get("chums", []) if isinstance(item, dict) and str(item.get("key")) == key)
        before = int(chum.get("usedToday", 0) or 0)
        bought: list[dict[str, Any]] = []
        for cost in chum.get("cost", []) if isinstance(chum.get("cost"), list) else []:
            item_id = str(cost.get("itemId") or "")
            shortage = int(cost.get("qty", 0) or 0) - int(cost.get("owned", 0) or 0)
            if shortage <= 0 or not item_id.startswith("item_fishing_bait_"):
                continue
            bait = next((item for item in shop.get("baits", []) if isinstance(item, dict)
                         and str(item.get("itemId")) == item_id), None)
            if bait is None or not options.get("buy_bait", {}).get("enabled"):
                continue
            reply = self._request("buy_bait", token=token, init_data=init_data,
                                  payload={"baitKey": bait.get("key"), "quantity": shortage})
            steps.append("chum_buy_bait")
            bought.append({"bait": str(bait.get("key")), "quantity": shortage,
                           "status": "confirmed" if reply.get("ok") else str(reply.get("error") or "unconfirmed")})
        if bought:
            refreshed = self._request("shop", token=token, init_data=init_data); steps.append("chum_shop_refresh")
            if not refreshed.get("ok"):
                return shop_data, {"status": "shop_failed", "chum": key, "bought": bought}
            shop_data = dict(refreshed.get("data") or {})
            shop = self._shop_body(shop_data)
            chum = next((item for item in shop.get("chums", []) if isinstance(item, dict)
                         and str(item.get("key")) == key), chum)
        if chum.get("affordable") is False:
            shortages = [{"name": _safe(str(cost.get("name") or "")), "owned": cost.get("owned"), "qty": cost.get("qty")}
                         for cost in chum.get("cost", []) if int(cost.get("owned", 0) or 0) < int(cost.get("qty", 0) or 0)]
            return shop_data, {"status": "unaffordable", "chum": key, "shortages": shortages, "bought": bought,
                               **self._chum_usage(shop_data)}
        reply = self._request("chum", token=token, init_data=init_data, payload={"chumKey": key}); steps.append("chum")
        if reply.get("explicit_rejection"):
            return shop_data, {"status": "rejected", "chum": key, "error_code": str(reply.get("error") or ""),
                               "bought": bought, **self._chum_usage(shop_data)}
        # Success or a lost reply: only the server's counters decide.
        refreshed = self._request("shop", token=token, init_data=init_data); steps.append("chum_verify")
        if refreshed.get("ok"):
            shop_data = dict(refreshed.get("data") or {})
            usage = self._chum_usage(shop_data)
            if usage["used_today"].get(key, 0) > before or usage["active"].get("key") == key:
                return shop_data, {"status": "applied", "chum": key, "bought": bought, **usage}
        return shop_data, {"status": "chum_unconfirmed", "chum": key, "bought": bought}

    async def prepare_account(self, account: str, options: dict[str, Any], *, authenticated: tuple[Any, str, str] | None = None) -> dict[str, str]:
        """Perform optional bait/chum mutations under the account transaction lock."""
        own_client = authenticated is None
        lock = _account_lock(self.lock_root, account) if own_client else None
        if lock is not None:
            lock.__enter__()
        try:
            created = authenticated is None
            if created:
                client, init_data, token, copy_dir = await self._authenticated(account)
            else:
                client, init_data, token = authenticated
                copy_dir = None
            try:
                auto, buy, chum = options.get("auto", {}), options.get("buy_bait", {}), options.get("apply_chum", {})
                shop_reply = self._request("shop", token=token, init_data=init_data)
                if not shop_reply.get("ok"):
                    return {"buy_bait": "shop_failed", "chum": "not_attempted"}
                bait = self._bait(dict(shop_reply.get("data") or {}), str(auto.get("bait") or ""))
                buy_status = "not_needed"
                if not bait and buy.get("enabled") and buy.get("when_empty"):
                    reply = self._request("buy_bait", token=token, init_data=init_data, payload={"baitKey": auto.get("bait"), "quantity": int(buy["quantity"])})
                    buy_status = "confirmed" if reply.get("ok") and self._confirmed(dict(reply.get("data") or {})) else "unconfirmed"
                    if buy_status != "confirmed":
                        return {"buy_bait": buy_status, "chum": "not_attempted"}
                chum_status = "not_selected"
                if auto.get("chum") != "none" and chum.get("enabled") and chum.get("when_selected"):
                    reply = self._request("chum", token=token, init_data=init_data, payload={"chumKey": auto.get("chum")})
                    chum_status = "confirmed" if reply.get("ok") and self._confirmed(dict(reply.get("data") or {})) else "unconfirmed"
                return {"buy_bait": buy_status, "chum": chum_status}
            finally:
                if own_client:
                    await self._close_client(client, copy_dir)
        finally:
            if lock is not None:
                lock.__exit__(None, None, None)

    # A cast whose bite is pending is reeled in the same run. Returning
    # ``waiting`` and coming back 120 s later (the old rule) let the bite window
    # close in between: on 2026-09-23 @demo_player_04's server count reached 5
    # casts with only 4 settlements, the fifth having been lost this way.
    BITE_WAIT_MAX_SECONDS = 180
    UNCONFIRMED_CAST_PROBES = 3
    UNCONFIRMED_CAST_PROBE_DELAY_SECONDS = 2.0

    async def _bite_challenge(self, *, token: str, init_data: str, start_data: dict[str, Any],
                              steps: list[str]) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
        """Wait for ``biteAt`` inside this token scope and re-read ``start``.

        Returns ``(token, challenge, failure)``; ``challenge`` is empty when the
        server still shows no bite, ``failure`` is a terminal result to return.
        """
        challenge = start_data.get("challenge") if isinstance(start_data.get("challenge"), dict) else {}
        if challenge:
            return token, challenge, None
        session = dict(start_data.get("session") or {})
        try:
            bite_at = int(session.get("biteAt") or 0)
            server_now = int(session.get("serverNow") or 0)
        except (TypeError, ValueError):
            bite_at = server_now = 0
        if bite_at and server_now and bite_at > server_now:
            wait_seconds = max(1, (bite_at - server_now + 999) // 1000 + 1)
            if wait_seconds > self.BITE_WAIT_MAX_SECONDS:
                return token, {}, None
            await asyncio.sleep(wait_seconds)
            start = self._request("start", token=token, init_data=init_data); steps.append("start_bite")
            if not start.get("ok"):
                return token, {}, {"status": "start_bite_failed", "steps": steps}
            start_data = dict(start.get("data") or {})
            token = str(start_data.get("token") or token)
            challenge = start_data.get("challenge") if isinstance(start_data.get("challenge"), dict) else {}
        return token, challenge, None

    async def _recover_unconfirmed_cast(self, *, account: str, token: str, init_data: str,
                                        steps: list[str]) -> dict[str, Any] | None:
        """After a ``next`` reply was lost, read the live session at once.

        The server may already have consumed bait and started the cast. Only a
        ``lobby`` snapshot proves it did not; a waiting or challenge session is
        reeled right here so the bite is not lost to the retry delay.
        """
        for attempt in range(self.UNCONFIRMED_CAST_PROBES):
            if attempt:
                await asyncio.sleep(self.UNCONFIRMED_CAST_PROBE_DELAY_SECONDS)
            start = self._request("start", token=token, init_data=init_data); steps.append("start_after_unconfirmed_next")
            if not start.get("ok"):
                continue
            start_data = dict(start.get("data") or {})
            token = str(start_data.get("token") or token)
            phase = str(dict(start_data.get("session") or {}).get("phase") or "").strip().lower()
            token, challenge, failure = await self._bite_challenge(token=token, init_data=init_data,
                                                                   start_data=start_data, steps=steps)
            if failure is not None:
                return failure
            if challenge:
                return await self._settle_challenge(account=account, token=token, init_data=init_data,
                                                    challenge=challenge, steps=steps)
            if phase == "lobby":
                # The cast never reached the server: bait and quota untouched.
                self._save_pending(account, pending=False, status="cast_not_applied")
                return {"status": "next_failed", "steps": steps, "error_code": "response_unconfirmed",
                        "cast_applied": False}
            if phase == "waiting":
                return {"status": "waiting", "steps": steps, "cast_applied": True}
            if phase in {"missed", "expired", "settled"}:
                # Consumed without a settlement we can attribute; keep the
                # evidence, the server's daily count includes it.
                self._note_unconfirmed_cast(account, phase)
                return {"status": "cast_unconfirmed", "steps": steps, "error_code": "cast_consumed_unsettled",
                        "official_phase": phase, "cast_applied": True}
        return None

    def _note_unconfirmed_cast(self, account: str, phase: str) -> None:
        path = self._pending_path(account)
        state = _json_object(path) if path.exists() else {}
        rows = list(state.get("unconfirmed_casts") or [])
        rows.append({"day": day_key(), "official_phase": phase,
                     "observed_at_ts": time.time()})
        self._save_pending(account, pending=False, status="cast_unconfirmed", unconfirmed_casts=rows[-40:])

    def _log_run(self, account: str, result: dict[str, Any], *, sample: str | None = None) -> None:
        """Append one redacted line per run so a lost cast can be traced later."""
        try:
            path = self._pending_path(account).with_name("runs.jsonl")
            row = {"ts": time.time(), "at": shanghai_now().isoformat(timespec="seconds"),
                   "status": result.get("status"), "steps": list(result.get("steps") or [])[:16],
                   "error_code": result.get("error_code"), "settlement_id": result.get("settlement_id"),
                   "error_type": result.get("error_type"),
                   "caught": result.get("caught"), "cast_applied": result.get("cast_applied"),
                   "official_phase": result.get("official_phase"), "catch": result.get("catch"),
                   "cast_diagnostics": result.get("cast_diagnostics"),
                   **({"result_sample": sample} if sample else {})}
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
            lines.append(json.dumps(row, ensure_ascii=False))
            path.write_text("\n".join(lines[-400:]) + "\n", encoding="utf-8")
        except Exception:  # noqa: BLE001 - evidence must never break the cast
            pass

    async def _settle_challenge(self, *, account: str, token: str, init_data: str, challenge: dict[str, Any], steps: list[str]) -> dict[str, Any]:
        """Only result.ready ends the durable cast. HTTP/finish acceptance does not."""
        challenge_id = str(challenge.get("challengeId") or "")
        if not challenge_id:
            return {"status": "challenge_unclassified", "steps": steps}
        proof = build_fishing_proof(challenge)
        diagnostics = {"proof_duration_ms": proof["durationMs"], "event_count": len(proof["events"])}
        self._save_pending(account, pending=True, status="finish_pending", challenge_id=challenge_id,
                           retry_not_before_ts=time.time() + 120, cast_diagnostics=diagnostics)
        self._save_scope(account, challenge_id, token)
        planned_finish = time.monotonic() + proof["durationMs"] / 1000 + 0.2
        await asyncio.sleep(proof["durationMs"] / 1000 + 0.2)
        diagnostics["reel_wake_lateness_ms"] = round(max(0, time.monotonic() - planned_finish) * 1000, 3)
        diagnostics["finish_submitted_at_ts"] = time.time()
        finish_started = time.monotonic()
        finish = self._request("finish", token=token, init_data=init_data, payload={"fishingProof": proof})
        diagnostics.update(finish_duration_ms=round((time.monotonic() - finish_started) * 1000, 3),
                           finish_ok=bool(finish.get("ok")), finish_http_status=finish.get("status_code", 0),
                           finish_explicit_rejection=bool(finish.get("explicit_rejection")),
                           finish_error_type=finish.get("error_type"))
        data = finish.get("data") or {}
        score_data = data.get("result") if isinstance(data, dict) else None
        score = score_data.get("score") if isinstance(score_data, dict) else None
        if type(score) in (int, float) and math.isfinite(score):
            diagnostics["finish_score"] = score
        self._save_pending(account, cast_diagnostics=diagnostics)
        steps.append("finish")
        for attempt in range(9):
            await asyncio.sleep(0.65 if attempt < 4 else 1.0)
            response = self._request("result", token=token, init_data=init_data)
            steps.append("result")
            result = self._ready_result(response, challenge_id, scope_challenge_id=challenge_id)
            diagnostics["result_probes"] = attempt + 1
            if result is not None:
                return self._record_settlement(account, challenge_id, result, steps, 1,
                                               response=response, diagnostics=diagnostics)
        if finish.get("explicit_rejection"):
            self._save_pending(account, pending=False, status="finish_rejected", error_code=finish.get("error"))
            self._scope_path(account).unlink(missing_ok=True)
            return {"status": "finish_rejected", "steps": steps, "error_code": finish.get("error"),
                    "cast_diagnostics": diagnostics}
        self._save_pending(account, cast_diagnostics=diagnostics)
        return {"status": "finish_pending_settlement", "steps": steps, "finish_attempts": 1,
                "finish_ok": bool(finish.get("ok")), "retry_not_before_ts": time.time() + 120,
                "cast_diagnostics": diagnostics}

    async def run_account(self, account: str, options: dict[str, Any]) -> dict[str, Any]:
        # Pool only within this account/run; never share cookies or connection
        # state across accounts, and close the pool on failures as well.
        if self._http_session is not None:
            raise RuntimeError("fishing_protocol_already_running")
        if self.http_transport is None:
            import requests
            self._http_session = requests.Session()
        try:
            return await self._run_account_with_evidence(account, options)
        except Exception as exc:
            self._log_run(account, {"status": "failed", "error_type": type(exc).__name__})
            raise
        finally:
            if self._http_session is not None:
                self._http_session.close()
                self._http_session = None

    async def _run_account_with_evidence(self, account: str, options: dict[str, Any]) -> dict[str, Any]:
        self._chum_reports.pop(account, None)
        self._ponds.pop(account, None)
        result = await self._run_account(account, options)
        if account in self._chum_reports:
            result["chum"] = _safe(self._chum_reports.pop(account))
        if account in self._ponds:
            result["pond"] = self._ponds.pop(account)
        path = self._pending_path(account)
        state = _json_object(path) if path.exists() else {}
        day = day_key()
        result["confirmed_settlement_ids"] = [row["settlement_id"] for row in state.get("settlements", []) if row.get("day") == day]
        result["settlement_date"] = day
        result["unconfirmed_casts_today"] = len([row for row in state.get("unconfirmed_casts", []) if row.get("day") == day])
        self._log_run(account, result, sample=result.pop("result_sample", None))
        return result

    async def _run_account(self, account: str, options: dict[str, Any]) -> dict[str, Any]:
        """Run one complete source-compatible cast under one WebView scope."""
        with _account_lock(self.lock_root, account):
            path = self._pending_path(account)
            pending = _json_object(path) if path.exists() else {}
            if pending and pending.get("account") != account:
                raise ValueError("fishing pending account mismatch")
            day = day_key()
            limit = options.get("daily_cast_limit", DEFAULT_DAILY_CAST_LIMIT)
            if limit is not None and len([row for row in pending.get("settlements", []) if row.get("day") == day]) >= limit:
                return dict(pending.get("last_result") or {"status": "daily_complete"})
            resumed_scope = bool(self._pending_scope_token(account))
            client, init_data, token, copy_dir = await self._authenticated(account)
            steps: list[str] = []
            try:
                # `start` is also the authoritative session snapshot.  If the
                # prior cast is still waiting, do not consume bait or advance
                # another account; the same account must later resume/reel it.
                initial = self._request("start", token=token, init_data=init_data); steps.append("start_snapshot")
                if (resumed_scope and initial.get("explicit_rejection")
                        and initial.get("error") in {"fishing_token_used", "fishing_session_closed"}):
                    # The official client also reads /result for a closed cast;
                    # the saved token still binds an ID-less result to this cast.
                    recovered = self._request("result", token=token, init_data=init_data)
                    steps.append("closed_result_recovery")
                    result = self._ready_result(recovered, pending["challenge_id"],
                                                scope_challenge_id=pending["challenge_id"])
                    if result is not None:
                        return self._record_settlement(account, pending["challenge_id"], result, steps, 0,
                                                       response=recovered)
                    return {"status": "finish_pending_settlement", "steps": steps,
                            "retry_not_before_ts": time.time() + 120,
                            "recovery_error_code": recovered.get("error")}
                if (resumed_scope and initial.get("explicit_rejection")
                        and initial.get("error") in {"invalid_token", "fishing_token_expired", "fishing_token_scope"}):
                    # An explicit invalid-scope reply allows fresh discovery;
                    # transport failures never discard the pending credential.
                    await self._close_client(client, copy_dir)
                    self._scope_path(account).unlink(missing_ok=True)
                    client, init_data, token, copy_dir = await self._authenticated(account, prefer_pending=False)
                    resumed_scope = False
                    initial = self._request("start", token=token, init_data=init_data)
                    steps.append("start_scope_refresh")
                if not initial.get("ok"):
                    return {"status": "start_snapshot_failed", "steps": steps,
                            "error_code": initial.get("error"), "retry_not_before_ts": time.time() + 120}
                initial_data = dict(initial.get("data") or {})
                token = str(initial_data.get("token") or token)
                initial_session = dict(initial_data.get("session") or {})
                initial_phase = str(initial_session.get("phase") or "").strip().lower()
                initial_challenge = initial_data.get("challenge") if isinstance(initial_data.get("challenge"), dict) else {}
                if pending.get("pending") and pending.get("challenge_id"):
                    recovered = self._request("result", token=token, init_data=init_data)
                    steps.append("result_recovery")
                    scope_id = initial_challenge.get("challengeId")
                    if resumed_scope and not scope_id and initial_phase in {"expired", "settled", "missed"}:
                        scope_id = pending["challenge_id"]
                    result = self._ready_result(recovered, pending["challenge_id"], scope_challenge_id=scope_id)
                    if result is not None:
                        return self._record_settlement(account, pending["challenge_id"], result, steps, 0,
                                                       response=recovered)
                    if (recovered.get("explicit_rejection") and recovered.get("error") == "fishing_token_scope"
                            and initial_phase in {"lobby", "waiting"} and not initial_challenge
                            and time.time() >= float(pending.get("retry_not_before_ts") or 0)):
                        # The server explicitly rejected recovery and confirmed
                        # a new lobby/waiting session. Retain the unknown result
                        # as evidence; it contributes zero to the daily count.
                        self._archive_unavailable_result(account, pending, initial_phase)
                        pending = {}
                        steps.append("unconfirmed_result_archived")
                    elif (time.time() < float(pending.get("retry_not_before_ts") or 0)
                            or str(initial_challenge.get("challengeId") or "") != pending["challenge_id"]):
                        if resumed_scope and scope_id == pending["challenge_id"]:
                            self._save_scope(account, pending["challenge_id"], token)
                        return {"status": "finish_pending_settlement", "steps": steps,
                                "retry_not_before_ts": pending.get("retry_not_before_ts"),
                                "recovery_error_code": recovered.get("error"),
                                "completion_reason": "awaiting matching challenge/result; no new cast"}
                if initial_challenge:
                    return await self._settle_challenge(
                        account=account, token=token,
                        init_data=init_data,
                        challenge=initial_challenge,
                        steps=steps,
                    )
                if initial_phase == "waiting":
                    token, challenge, failure = await self._bite_challenge(token=token, init_data=init_data,
                                                                           start_data=initial_data, steps=steps)
                    if failure is not None:
                        return failure
                    if challenge:
                        return await self._settle_challenge(account=account, token=token, init_data=init_data,
                                                            challenge=challenge, steps=steps)
                    return {"status": "waiting", "steps": steps}
                if initial_phase != "lobby":
                    return {"status": "waiting", "steps": steps,
                            "completion_reason": "no official lobby or recoverable challenge; no new cast"}
                # A cast already on the line was reeled in above; a new one
                # (shop, nest, bait, next) is not started while 青元子 fights.
                yielded = self.yield_check(time.time())
                if yielded is not None:
                    return {"status": yielded["status"], "reason": yielded["reason"], "steps": steps}
                shop = self._request("shop", token=token, init_data=init_data); steps.append("shop")
                if not shop.get("ok"):
                    return {"status": "shop_failed", "steps": steps}
                if options.get("chum_plan"):
                    # The nest goes down before the bait check: 妖腥窝 itself
                    # consumes two 妖血饵.
                    shop_data, report = self._apply_chum_plan(token=token, init_data=init_data,
                                                              shop_data=dict(shop.get("data") or {}),
                                                              options=options, steps=steps)
                    self._chum_reports[account] = report
                    if report["status"] in {"chum_unconfirmed", "shop_failed"}:
                        return {"status": report["status"], "steps": steps,
                                "retry_not_before_ts": time.time() + 60}
                    shop = {**shop, "data": shop_data}
                bait = self._bait(dict(shop.get("data") or {}), str(options.get("auto", {}).get("bait") or ""))
                if not bait:
                    prepared = await self.prepare_account(
                        account,
                        options,
                        authenticated=(client, init_data, token),
                    )
                    if prepared.get("buy_bait") != "confirmed":
                        return {"status": "bait_missing", "steps": steps, "buy_bait": prepared.get("buy_bait", "not_attempted")}
                    # Refresh inventory only after a confirmed server purchase.
                    shop = self._request("shop", token=token, init_data=init_data); steps.append("shop_refresh")
                    bait = self._bait(dict(shop.get("data") or {}), str(options.get("auto", {}).get("bait") or ""))
                    if not bait:
                        return {"status": "bait_missing_after_confirmed_purchase", "steps": steps}
                if pending.get("pending") and time.time() < float(pending.get("retry_not_before_ts") or 0):
                    return {"status": "cast_reply_pending", "steps": steps, "retry_not_before_ts": pending["retry_not_before_ts"]}
                self._save_pending(account, pending=True, status="cast_submitted", challenge_id=None,
                                   retry_not_before_ts=time.time() + 120, cast_diagnostics={})
                pond = self.select_pond(dict(shop.get("data") or {}), str(options.get("auto", {}).get("pond") or FALLBACK_POND))
                self._ponds[account] = pond
                next_reply = self._request("next", token=token, init_data=init_data, payload={"pondKey": pond, "baitItemId": bait.get("itemId")}); steps.append("next")
                if not next_reply.get("ok"):
                    if next_reply.get("explicit_rejection"):
                        self._save_pending(account, pending=False, status="cast_rejected", error_code=next_reply.get("error"))
                    else:
                        recovered = await self._recover_unconfirmed_cast(account=account, token=token,
                                                                         init_data=init_data, steps=steps)
                        if recovered is not None:
                            return recovered
                    error_data = dict(next_reply.get("data") or {})
                    code = _safe(str(error_data.get("error") or error_data.get("code") or next_reply.get("error") or "next_failed"))
                    return {
                        "status": "no_rod" if code == "fishing_rod_missing" else "next_failed",
                        "steps": steps,
                        "error_code": code,
                    }
                data = dict(next_reply.get("data") or {})
                token = str(data.get("token") or data.get("nextToken") or token)
                start = self._request("start", token=token, init_data=init_data); steps.append("start")
                if not start.get("ok"):
                    return {"status": "start_failed", "steps": steps}
                start_data = dict(start.get("data") or {})
                token = str(start_data.get("token") or token)
                # Source worker retains the same transport until biteAt, then
                # re-reads start in the same token scope to obtain challenge.
                token, challenge, failure = await self._bite_challenge(token=token, init_data=init_data,
                                                                       start_data=start_data, steps=steps)
                if failure is not None:
                    return failure
                if not challenge:
                    return {"status": "waiting", "steps": steps}
                # A fresh cast can immediately return its bite challenge. Keep
                # this WebView scope alive through duration, finish and result.
                return await self._settle_challenge(
                    account=account, token=token,
                    init_data=init_data,
                    challenge=challenge,
                    steps=steps,
                )
            finally:
                await self._close_client(client, copy_dir)
