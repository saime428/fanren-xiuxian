#!/usr/bin/env python3
"""Identity-scoped clients for 天机命脉 and 噬金虫进化 (child tokens via dwelling/external)."""
from __future__ import annotations

from typing import Any

from miniapp_shared.dwelling import (
    DWELLING_EXTERNAL,
    DWELLING_START,
    DwellingSession,
    JsonTransport,
    mint_child_token,
)

from .protocol import (
    BEAST_ACTION, BEAST_PATHS, BEAST_PREFIX, BEAST_TOKEN_PREFIXES, BEAST_WEB_PATH,
    FATE_ACTION, FATE_PATHS, FATE_PREFIX, FATE_TOKEN_PREFIXES, FATE_WEB_PATH,
)

ALLOWED_PATHS = {DWELLING_START: False, DWELLING_EXTERNAL: False, **FATE_PATHS, **BEAST_PATHS}


def official_transport() -> JsonTransport:
    return JsonTransport(ALLOWED_PATHS, timeout=25.0)


class FateIdentityClient:
    def __init__(self, transport: Any, dwelling: DwellingSession, player_id: int) -> None:
        self.transport = transport
        self.dwelling = dwelling
        self.player_id = int(player_id)
        self._fate_token = ""
        self._beast_token = ""

    async def _token(self, kind: str) -> str:
        if kind == "fate":
            if not self._fate_token:
                self._fate_token = await mint_child_token(
                    self.transport, self.dwelling, player_id=self.player_id, action=FATE_ACTION,
                    web_path=FATE_WEB_PATH, prefixes=FATE_TOKEN_PREFIXES)
            return self._fate_token
        if not self._beast_token:
            self._beast_token = await mint_child_token(
                self.transport, self.dwelling, player_id=self.player_id, action=BEAST_ACTION,
                web_path=BEAST_WEB_PATH, prefixes=BEAST_TOKEN_PREFIXES)
        return self._beast_token

    async def fate(self, endpoint: str, **fields: Any) -> dict[str, Any]:
        path = FATE_PREFIX + endpoint
        payload = {"token": await self._token("fate"), "initData": self.dwelling.init_data, **fields}
        return await self.transport(path, payload, mutation=FATE_PATHS[path])

    async def beast(self, endpoint: str, **fields: Any) -> dict[str, Any]:
        path = BEAST_PREFIX + endpoint
        payload = {"token": await self._token("beast"), "initData": self.dwelling.init_data, **fields}
        return await self.transport(path, payload, mutation=BEAST_PATHS[path])
