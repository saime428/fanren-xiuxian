#!/usr/bin/env python3
"""Identity-scoped dwelling journey client (details + wild_experience action)."""
from __future__ import annotations

from typing import Any

from miniapp_shared.dwelling import (
    DWELLING_DETAILS,
    DWELLING_EXTERNAL,
    DWELLING_JOURNEY,
    DWELLING_START,
    DwellingSession,
    JsonTransport,
)

from .protocol import MODE, action_payload

ALLOWED_PATHS = {
    DWELLING_START: False,
    DWELLING_EXTERNAL: False,
    DWELLING_DETAILS: False,
    DWELLING_JOURNEY: True,
}


def official_transport() -> JsonTransport:
    return JsonTransport(ALLOWED_PATHS, timeout=25.0)


class JourneyIdentityClient:
    def __init__(self, transport: Any, dwelling: DwellingSession, player_id: int) -> None:
        self.transport = transport
        self.dwelling = dwelling
        self.player_id = int(player_id)

    async def details(self) -> dict[str, Any]:
        return await self.transport(DWELLING_DETAILS, self.dwelling.base_payload(self.player_id), mutation=False)

    async def act(self) -> dict[str, Any]:
        payload = action_payload(self.dwelling.base_payload(), self.player_id, mode=MODE)
        return await self.transport(DWELLING_JOURNEY, payload, mutation=True)
