"""Business accounts and the features each one runs, importable without Telethon.

One config file is the only account source. ``config/accounts/accounts.json``
(gitignored; written by ``scripts/xiuxian_accounts.py``) wins; without it the
tracked six-account demo ``config/accounts.example.json`` is used. Tests always
run against the demo file (``conftest.py`` sets ``XIUXIAN_EXAMPLE_CONFIG``).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_PATH = ROOT / "config" / "accounts.example.json"
USER_PATH = ROOT / "config" / "accounts" / "accounts.json"
# The slug must be ``tg<Telegram user id>``: receipts check ``int(account[2:])``.
ACCOUNT_ID = re.compile(r"tg[1-9]\d{4,14}")
USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")

# Every feature an account may enable: key -> 中文名. Keys of per-account group
# flows equal their ``config/active_flows.json`` flow; the rest are shared
# coordinators that loop over the accounts enabling them.
FEATURES = {
    "shendubiguan": "深度闭关",
    "yuanyingchuqiao": "元婴出窍",
    "tanxunliefeng": "探寻裂缝",
    "wendao": "问道",
    "xuexishanlin": "血洗山林",
    "zhaohuanmoying": "召唤魔影",
    "shenji_budao": "神迹·布道",
    "fumofabao_xuantianzhanlingjian": "抚摸法宝·玄天斩灵剑",
    "fumofabao_qingzhufengyunjian_shenlei": "抚摸法宝·青竹蜂云剑（神雷版）",
    "jiaohuan_fabao": "南陇侯交换·选法宝",
    "jiaohuan_gongfa": "南陇侯交换·选功法",
    "jiyin_choice": "极阴祖师抉择",
    "xuangu_zuoda": "玄骨上人作答",
    "shuangxiu_wenyang": "双修温养",
    "treasure_hunt": "洞府寻宝",
    "tianxing_exploration": "天星宗深入游历",
    "yuanshen": "第二元神修炼与镇魔",
    "qingyuanzi": "青元子世界 Boss",
    "fishing": "灵溪垂钓",
    "star_observatory": "星宫灵圃",
    "tower": "琉璃问心塔",
    "journey": "游历·野外历练",
    "fate_cards": "天机命脉",
    "sect_daily": "宗门日常（点卯/军报/支援慕兰）",
    "pavilion": "藏娇阁侍妾",
    "account_lifecycle": "肉身碎裂监控",
}
# Features whose runners keep one account's state (one listener, one soul).
SINGLE_ACCOUNT_FEATURES = frozenset({
    "yuanshen", "tianxing_exploration", "shuangxiu_wenyang", "fumofabao_xuantianzhanlingjian",
})
EXCLUSIVE_FEATURES = (frozenset({"jiaohuan_fabao", "jiaohuan_gongfa"}),)


def config_path() -> Path:
    explicit = os.environ.get("XIUXIAN_ACCOUNTS_FILE")
    # A named file that does not exist yet (the setup tools are about to write
    # it) leaves the import-time accounts on the demo config.
    if explicit and Path(explicit).is_file():
        return Path(explicit)
    if os.environ.get("XIUXIAN_EXAMPLE_CONFIG") or not USER_PATH.is_file():
        return EXAMPLE_PATH
    return USER_PATH


def _fail(message: str) -> None:
    raise ValueError(f"accounts config: {message}")


def validate(payload: Any) -> list[dict[str, Any]]:
    """Return the account rows of a config payload; raise ValueError on anything malformed."""
    if not isinstance(payload, dict) or not isinstance(payload.get("accounts"), list):
        _fail("expected an object with an accounts list")
    rows, seen = [], set()
    for row in payload["accounts"]:
        if not isinstance(row, dict):
            _fail("each account must be an object")
        account = row.get("id")
        if not isinstance(account, str) or not ACCOUNT_ID.fullmatch(account):
            _fail(f"invalid account id {account!r}; use tg<Telegram user id>")
        if account in seen:
            _fail(f"duplicate account {account}")
        seen.add(account)
        if not isinstance(row.get("username"), str) or not USERNAME.fullmatch(row["username"]):
            _fail(f"{account}: username must be the Telegram @username without @")
        source = row.get("credential_source", account)
        if not isinstance(source, str) or not ACCOUNT_ID.fullmatch(source):
            _fail(f"{account}: invalid credential_source")
        session = row.get("session")
        if session is not None and (not isinstance(session, str) or PurePosixPath(session).is_absolute()
                                    or ".." in PurePosixPath(session).parts
                                    or PurePosixPath(session).parts[:1] != ("state",)):
            _fail(f"{account}: session must be a relative path under state/")
        bound = row.get("bound_identity")
        if bound is not None and (not isinstance(bound, dict) or type(bound.get("player_id")) is not int
                                  or not isinstance(bound.get("label", ""), str)):
            _fail(f"{account}: bound_identity needs an integer player_id")
        features = row.get("features", {})
        if not isinstance(features, dict):
            _fail(f"{account}: features must be an object")
        for key, value in features.items():
            if key not in FEATURES:
                _fail(f"{account}: unknown feature {key!r}")
            if value is not True and not isinstance(value, dict):
                _fail(f"{account}: feature {key} must be true or an options object")
        for group in EXCLUSIVE_FEATURES:
            if len(group & set(features)) > 1:
                _fail(f"{account}: choose only one of {sorted(group)}")
        rows.append({**row, "credential_source": source, "features": dict(features)})
    for feature in SINGLE_ACCOUNT_FEATURES:
        users = [row["id"] for row in rows if feature in row["features"]]
        if len(users) > 1:
            _fail(f"feature {feature} supports one account, enabled on {users}")
    for row in rows:
        partner = row["features"].get("shuangxiu_wenyang", {})
        if isinstance(partner, dict) and partner.get("partner") not in seen | {None}:
            _fail(f"{row['id']}: shuangxiu_wenyang partner must be a configured account")
    return rows


def game_settings(payload: dict[str, Any]) -> dict[str, Any]:
    """The game group every account plays in; the tracked demo values are placeholders."""
    game = {**GAME_DEFAULTS, **(payload.get("game") or {})}
    if set(game) != set(GAME_DEFAULTS):
        _fail(f"game keys must be {sorted(GAME_DEFAULTS)}")
    if not isinstance(game["group"], str) or not USERNAME.fullmatch(game["group"]):
        _fail("game.group must be the group @username without @")
    for key in ("chat_id", "second_chat_id"):
        if type(game[key]) is not int or not str(game[key]).startswith("-100"):
            _fail(f"game.{key} must be a -100… supergroup id")
    if type(game["topic_id"]) is not int or game["topic_id"] <= 0:
        _fail("game.topic_id must be a positive forum topic id")
    return game


def notify_settings(payload: dict[str, Any]) -> dict[str, str]:
    """Optional Hermes delivery: {"target": "telegram:<your user id>", "hermes_bin": path}."""
    notify = payload.get("notify") or {}
    if (not isinstance(notify, dict) or set(notify) - {"target", "hermes_bin"}
            or not all(isinstance(value, str) for value in notify.values())):
        _fail("notify must be an object with string target/hermes_bin")
    if notify.get("target") and not re.fullmatch(r"telegram:-?\d+", notify["target"]):
        _fail("notify.target must look like telegram:<user id>")
    return dict(notify)


def load_accounts(path: Path | None = None) -> list[dict[str, Any]]:
    return validate(json.loads(Path(path or config_path()).read_text(encoding="utf-8")))


GAME_DEFAULTS = {"group": "example_game_group", "chat_id": -1009400000004, "topic_id": 7310786,
                 "second_chat_id": -1009400000005}
_PAYLOAD = json.loads(config_path().read_text(encoding="utf-8"))
ACCOUNTS = validate(_PAYLOAD)
GAME = game_settings(_PAYLOAD)
NOTIFY = notify_settings(_PAYLOAD)
# Commands go only to GAME_GROUP's forum topic GAME_TOPIC_ID (chat GAME_CHAT_ID);
# listeners also read the plain announcement chat GAME_SECOND_CHAT_ID.
GAME_GROUP, GAME_CHAT_ID = GAME["group"], GAME["chat_id"]
GAME_TOPIC_ID, GAME_SECOND_CHAT_ID = GAME["topic_id"], GAME["second_chat_id"]
_BY_ID = {row["id"]: row for row in ACCOUNTS}
ACCOUNT_USERNAMES = {row["id"]: row["username"] for row in ACCOUNTS}
ALL_ACCOUNTS = tuple(ACCOUNT_USERNAMES)
# The account whose config/accounts/<source>.env (API id/hash) and session an
# account signs in with; normally the account itself.
CREDENTIAL_SOURCE = {row["id"]: row["credential_source"] for row in ACCOUNTS}
# Project-relative Telethon session each dwelling MiniApp flow copies before
# signing in (fishing keeps its own copies under fishing/state/accounts).
DWELLING_SESSIONS = {row["id"]: row.get("session") or f"state/{row['credential_source']}.session"
                     for row in ACCOUNTS}
# On an account with a bound_identity only that dwelling identity runs the
# identity-scoped flows (星宫灵圃, 游历); other accounts are unrestricted.
BOUND_IDENTITY = {row["id"]: {"label": "", **row["bound_identity"]}
                  for row in ACCOUNTS if row.get("bound_identity")}


def accounts_for(feature: str) -> tuple[str, ...]:
    """Accounts enabling ``feature``, in config order."""
    if feature not in FEATURES:
        raise KeyError(feature)
    return tuple(row["id"] for row in ACCOUNTS if feature in row["features"])


def feature_options(account: str, feature: str) -> dict[str, Any]:
    """The options object of an enabled feature (``{}`` for ``true`` or a disabled feature)."""
    value = _BY_ID.get(account, {}).get("features", {}).get(feature)
    return dict(value) if isinstance(value, dict) else {}


def stagger_offsets(feature: str, step_minutes: int) -> dict[str, int]:
    """Admission offset in minutes per account: config order times ``step_minutes``."""
    return {account: index * step_minutes for index, account in enumerate(accounts_for(feature))}
