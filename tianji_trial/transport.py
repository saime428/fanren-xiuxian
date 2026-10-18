#!/usr/bin/env python3
"""Thin local transport facade for the upstream Tianji Trial protocol."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, urlparse

from .protocol import TianjiTrialError


AUTH_ERROR_CODES = {
    "trial_token_expired",
    "token_expired",
    "invalid_token",
    "entry_token_missing",
    "hash_mismatch",
    "init_data_expired",
    "auth_expired",
}


@dataclass(frozen=True)
class AccountAuth:
    init_data: str = field(repr=False)
    dwelling_token: str = field(repr=False)
    identities: tuple[Any, ...]


AuthProvider = Callable[[bool], Awaitable[AccountAuth]]
PostJson = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def extract_trial_token(url: Any) -> str:
    raw = str(url or "").strip()
    if raw.startswith("/"):
        raw = f"https://asc.aiopenai.app{raw}"
    parsed = urlparse(raw)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != "asc.aiopenai.app":
        raise TianjiTrialError("trial_entry_invalid")
    token = ""
    for part in (parsed.fragment, parsed.query):
        query = parse_qs(part, keep_blank_values=True)
        for key in ("tgWebAppStartParam", "startapp", "start_param"):
            values = query.get(key) or []
            if values:
                token = str(values[0])
                break
        if token:
            break
    if not token.startswith("trial_") or not all(char.isalnum() or char in "_-" for char in token):
        raise TianjiTrialError("trial_entry_invalid")
    return token


class DwellingTrialTransport:
    def __init__(self, auth_provider: AuthProvider, post_json: PostJson) -> None:
        self.auth_provider = auth_provider
        self.post_json = post_json
        self.auth: AccountAuth | None = None
        self.tokens: dict[int, str] = {}

    async def initialize(self, force: bool = False) -> AccountAuth:
        if self.auth is None or force:
            self.auth = await self.auth_provider(force)
            self.tokens.clear()
        return self.auth

    async def _mint_token(self, player_id: int) -> str:
        auth = await self.initialize()
        payload = await self.post_json(
            "/api/miniapp/xianxia-dwelling/external",
            {
                "token": auth.dwelling_token,
                "initData": auth.init_data,
                "playerId": int(player_id),
                "action": "tianji_trial",
            },
        )
        token = extract_trial_token(payload.get("url") if isinstance(payload, dict) else None)
        self.tokens[int(player_id)] = token
        return token

    async def _start_once(self, player_id: int) -> dict[str, Any]:
        auth = await self.initialize()
        token = await self._mint_token(player_id)
        return await self.post_json(
            "/api/miniapp/xianxia-trial/start",
            {"token": token, "initData": auth.init_data},
        )

    async def start(self, player_id: int) -> dict[str, Any]:
        self.tokens.pop(int(player_id), None)
        try:
            return await self._start_once(player_id)
        except TianjiTrialError as exc:
            if exc.code not in AUTH_ERROR_CODES:
                raise
            await self.initialize(force=True)
            return await self._start_once(player_id)

    async def finish(self, player_id: int, proof: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(proof, dict) or not proof.get("challengeId"):
            raise TianjiTrialError("trial_proof_invalid")
        auth = await self.initialize()
        token = self.tokens.get(int(player_id))
        if not token:
            raise TianjiTrialError("trial_session_missing")
        return await self.post_json(
            "/api/miniapp/xianxia-trial/finish",
            {
                "token": token,
                "initData": auth.init_data,
                "trialProof": proof,
            },
        )
