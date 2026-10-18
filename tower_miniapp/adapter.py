#!/usr/bin/env python3
"""Identity-scoped session for the 琉璃问心塔 child MiniApp."""
from __future__ import annotations

from typing import Any

from miniapp_shared.dwelling import (
    DWELLING_EXTERNAL,
    DWELLING_START,
    DwellingSession,
    JsonTransport,
    MiniAppError,
    mint_child_token,
)

PAGODA_WEB_PATH = "/miniapp/xianxia-pagoda"
PAGODA_START = "/api/miniapp/xianxia-pagoda/start"
PAGODA_CHALLENGE = "/api/miniapp/xianxia-pagoda/challenge"
PAGODA_EXTERNAL_ACTION = "pagoda"
CHALLENGE_TIMEOUT_SECONDS = 90.0
ALLOWED_PATHS = {
    DWELLING_START: False,
    DWELLING_EXTERNAL: False,
    PAGODA_START: False,
    PAGODA_CHALLENGE: True,
}
AUTH_ERROR_CODES = frozenset({
    "token_expired", "invalid_token", "entry_token_missing", "hash_mismatch",
    "init_data_expired", "auth_expired", "pagoda_token_expired", "dwelling_token_expired",
})


def official_transport() -> JsonTransport:
    return JsonTransport(ALLOWED_PATHS, timeout=20.0)


class TowerIdentityClient:
    """``start``/``challenge`` for one ``playerId`` of an opened dwelling session."""

    def __init__(self, transport: Any, dwelling: DwellingSession, player_id: int) -> None:
        self.transport = transport
        self.dwelling = dwelling
        self.player_id = int(player_id)
        self.child_token = ""

    async def _mint(self) -> None:
        self.child_token = await mint_child_token(
            self.transport, self.dwelling, player_id=self.player_id, action=PAGODA_EXTERNAL_ACTION,
            web_path=PAGODA_WEB_PATH, prefixes=("pagoda_", "pagoda-"),
        )

    def _payload(self) -> dict[str, Any]:
        if not self.child_token:
            raise MiniAppError("session_not_prepared")
        return {"token": self.child_token, "initData": self.dwelling.init_data}

    async def start(self) -> dict[str, Any]:
        if not self.child_token:
            await self._mint()
        try:
            return await self.transport(PAGODA_START, self._payload(), mutation=False)
        except MiniAppError as exc:
            if exc.code not in AUTH_ERROR_CODES:
                raise
            # Only the read path may refresh a token; the challenge never replays.
            await self._mint()
            return await self.transport(PAGODA_START, self._payload(), mutation=False)

    async def challenge(self) -> dict[str, Any]:
        return await self.transport(PAGODA_CHALLENGE, self._payload(), mutation=True,
                                    timeout=CHALLENGE_TIMEOUT_SECONDS)
