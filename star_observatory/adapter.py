#!/usr/bin/env python3
"""Account session for the 星宫灵圃 child MiniApp (``xianxia-sect-farm``)."""
from __future__ import annotations

from typing import Any

from miniapp_shared.dwelling import (
    DWELLING_EXTERNAL,
    DWELLING_START,
    DwellingSession,
    JsonTransport,
    MiniAppError,
    mint_child_token,
    open_dwelling,
)

from .protocol import STAR_NAME
from .state import scoped_identity

FARM_WEB_PATH = "/miniapp/xianxia-sect-farm"
FARM_START = "/api/miniapp/xianxia-sect-farm/start"
FARM_ACTION = "/api/miniapp/xianxia-sect-farm/action"
FARM_EXTERNAL_ACTION = "sect_farm"
ALLOWED_PATHS = {
    DWELLING_START: False,
    DWELLING_EXTERNAL: False,
    FARM_START: False,
    FARM_ACTION: True,
}
AUTH_ERROR_CODES = frozenset({
    "token_expired", "invalid_token", "entry_token_missing", "hash_mismatch",
    "init_data_expired", "auth_expired", "farm_token_expired", "dwelling_token_expired",
})


def official_transport() -> JsonTransport:
    return JsonTransport(ALLOWED_PATHS, timeout=20.0)


class StarAccountSession:
    """One authenticated dwelling → sect-farm chain for the account's scoped identity."""

    def __init__(self, bridge: Any, transport: Any) -> None:
        self.bridge = bridge
        self.transport = transport
        self.dwelling: DwellingSession | None = None
        self.player_id = 0
        self.identity_label = ""
        self.child_token = ""

    async def prepare(self) -> dict[str, Any]:
        self.dwelling = await open_dwelling(self.bridge, self.transport)
        identity = scoped_identity(str(self.bridge.account), self.dwelling)
        self.player_id = int(identity.player_id)
        self.identity_label = str(getattr(identity, "label", "") or "")
        return await self._start(force=True)

    async def _start(self, *, force: bool = False) -> dict[str, Any]:
        assert self.dwelling is not None
        if force or not self.child_token:
            self.child_token = await mint_child_token(
                self.transport, self.dwelling, player_id=self.player_id,
                action=FARM_EXTERNAL_ACTION, web_path=FARM_WEB_PATH, prefixes=("farm_",),
            )
        return await self.transport(FARM_START, self._payload(), mutation=False)

    def _payload(self) -> dict[str, Any]:
        # Child MiniApp tokens are already player-scoped by dwelling/external;
        # the official client posts only token + initData to child endpoints.
        if not self.dwelling or not self.child_token or self.player_id <= 0:
            raise MiniAppError("session_not_prepared")
        return {"token": self.child_token, "initData": self.dwelling.init_data}

    async def snapshot(self) -> dict[str, Any]:
        """Read-only refresh; on explicit auth expiry mint a fresh child token."""
        try:
            return await self._start()
        except MiniAppError as exc:
            if exc.code not in AUTH_ERROR_CODES:
                raise
            return await self._start(force=True)

    async def action(self, decision: dict[str, Any]) -> dict[str, Any]:
        action = str(decision.get("action") or "").strip()
        if action not in {"soothe", "collect", "pull"}:
            raise MiniAppError("action_not_allowed")
        payload = {**self._payload(), "action": action, "plotKey": str(decision.get("plotKey") or "")}
        if action == "pull":
            star = str(decision.get("starName") or "").strip()
            if star != STAR_NAME:
                raise MiniAppError("star_not_allowed")
            payload["starName"] = star
        return await self.transport(FARM_ACTION, payload, mutation=True)
