"""MiniApp error type and log-safe text helpers; standard library only.

Parsers and state modules import these without pulling in the dwelling chain
(Telethon, the Tianji solver, the fishing integration).
"""
from __future__ import annotations

import re
from typing import Any

# Official dwelling entries live on fanrenxiuxian_bot or a rotating
# hantianzunNN_bot; the bot is taken from a validated pinned URL, never guessed.
DWELLING_BOT_PATTERN = re.compile(r"(?:fanrenxiuxian_bot|hantianzun\d{2}_bot)$", re.IGNORECASE)

_ERROR_CODE = re.compile(r"^[a-z0-9_]{3,80}$")
_URL = re.compile(r"(?:https?|tg)://\S+", re.IGNORECASE)
_SECRET_PAIR = re.compile(
    r"(?i)(tgWebAppData|initData|token|hash|signature|query_id|startapp|start_param)=([^&#\s]+)"
)


class MiniAppError(RuntimeError):
    """A flow-level failure with a short machine code.

    ``uncertain`` means the server may already have applied a mutation (transport
    failure after sending); callers must reconcile through a read before replay.
    """

    def __init__(self, code: str, *, uncertain: bool = False) -> None:
        self.code = str(code)
        self.uncertain = bool(uncertain)
        super().__init__(self.code)


def safe_text(value: Any, limit: int = 200) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = _URL.sub("[url]", text)
    text = _SECRET_PAIR.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    return text[: max(0, int(limit))]


def error_code(exc: BaseException) -> str:
    """Short, log-safe code for any exception raised along the dwelling chain."""
    # ponytail: isinstance, not getattr(exc, "code"): Telethon's RPCError also
    # has an int .code (420 for every FloodWait) that would replace its name.
    if isinstance(exc, MiniAppError):
        return str(exc.code)
    return safe_text(type(exc).__name__, 60)


def is_uncertain(exc: BaseException) -> bool:
    return bool(getattr(exc, "uncertain", False))


def server_error_code(payload: Any, default: str) -> str:
    candidates: list[Any] = []
    if isinstance(payload, dict):
        candidates.extend((payload.get("code"), payload.get("error")))
        for key in ("data", "actionResult"):
            nested = payload.get(key)
            if isinstance(nested, dict):
                candidates.extend((nested.get("code"), nested.get("error")))
    for candidate in candidates:
        code = str(candidate or "").strip().lower()
        if _ERROR_CODE.fullmatch(code):
            return code
    return default
