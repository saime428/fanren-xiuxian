"""Strict MiniApp requests. Authentication and full responses remain in memory."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from miniapp_shared.dwelling import JsonTransport, MiniAppError, open_dwelling, verify_username
from .catalog import ACCOUNTS, FLOW_COMMANDS, PREFIX, READ_COMMANDS, WRITE_COMMANDS


class CommandClient:
    def __init__(self, root: Path, account: str, bridge: Any, *, reader=None, writer=None):
        self.root, self.account, self.bridge = Path(root), account, bridge
        self.player_id = int(account.removeprefix("tg"))
        self.reader = reader or JsonTransport({PREFIX+p: False for p in
            ("start", "overview", "details", "command-center")})
        self.writer = writer or JsonTransport({PREFIX+p: True for p in
            ("deep-seclusion", "command-center", "small-world")})
        self.session = None

    async def open(self):
        await verify_username(self.bridge, self.account)
        self.session = await open_dwelling(self.bridge, self.reader)
        # Group commands operated the account's own character. Never silently
        # switch to a bound character or to the first identity in the menu.
        if not any(i.player_id == self.player_id for i in self.session.identities):
            raise MiniAppError("miniapp_personal_identity_missing")
        return self

    async def request(self, route: str, extra: dict, *, mutation: bool) -> dict:
        if self.session is None:
            raise MiniAppError("miniapp_session_missing")
        reply = await (self.writer if mutation else self.reader)(
            PREFIX+route, {**extra, **self.session.base_payload(self.player_id)}, mutation=mutation)
        # JsonTransport returns an ``ok: false`` body only when it carries an
        # ``actionResult``: a definite business answer that ``outcome`` classifies.
        if not isinstance(reply, dict) or (reply.get("ok") is not True
                                           and not isinstance(reply.get("actionResult"), dict)):
            raise MiniAppError("miniapp_response_invalid", uncertain=mutation)
        actor = (reply.get("account") or {}).get("playerId")
        if isinstance(actor, bool) or str(actor) != str(self.player_id):
            raise MiniAppError("miniapp_response_identity_mismatch", uncertain=mutation)
        auth_actor = (reply.get("account") or {}).get("authUserId")
        if auth_actor is not None and (isinstance(auth_actor, bool) or str(auth_actor) != str(self.player_id)):
            raise MiniAppError("miniapp_auth_identity_mismatch", uncertain=mutation)
        reported = (reply.get("actionResult") or {}).get("command")
        if "command" in extra and reported is not None and reported != extra["command"]:
            raise MiniAppError("miniapp_response_command_mismatch", uncertain=mutation)
        return reply

    async def overview(self):
        return await self.request("overview", {}, mutation=False)

    async def details(self):
        return await self.request("details", {}, mutation=False)

    async def read_command(self, command: str):
        if command not in READ_COMMANDS:
            raise MiniAppError("miniapp_read_command_not_allowed")
        return await self.request("command-center", {"command": command}, mutation=False)

    async def mutate(self, flow: str, *, operation: str = ""):
        if self.account not in ACCOUNTS.get(flow, ()):
            raise MiniAppError("miniapp_account_flow_not_allowed")
        from account_lifecycle import assert_account_allowed
        assert_account_allowed(self.account, path=self.root / "state/account_lifecycle.json")
        if flow == "shendubiguan":
            if operation not in {"start", "settle"}:
                raise MiniAppError("miniapp_deep_action_not_allowed")
            return await self.request("deep-seclusion", {"action": operation}, mutation=True)
        if flow == "shenji_budao":
            return await self.request("small-world", {"action": "miracle_sermon"}, mutation=True)
        command = ".元婴归窍" if flow == "yuanyingchuqiao" and operation == "return" else FLOW_COMMANDS[flow]
        if command not in WRITE_COMMANDS:
            raise MiniAppError("miniapp_write_command_not_allowed")
        return await self.request("command-center", {"command": command}, mutation=True)


def result_text(reply: dict) -> str:
    result = reply.get("actionResult")
    if not isinstance(result, dict):
        raise MiniAppError("miniapp_action_result_missing")
    return str(result.get("rawMessage") or result.get("message") or "")
