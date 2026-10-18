"""Official MiniApp first, canonical group only for unsupported commands."""
from __future__ import annotations

import asyncio
import inspect
import os
import time
from pathlib import Path

from command_confirmation import CANONICAL_CHAT_ID, CANONICAL_TARGET, CANONICAL_TOPIC_ID, official_message
from miniapp_shared.accounts import GAME_SECOND_CHAT_ID
from miniapp_shared.dwelling import JsonTransport, MiniAppError, open_dwelling, verify_username
from telegram_send_gate import run_with_global_send_gate

from . import ACCOUNT, PLAYER_ID
from .protocol import (COMMANDS, ExplorationError, action_text, catalog_commands, cooldown,
                       day, group_result, plain)

PREFIX = "/api/miniapp/xianxia-dwelling/"
READ_COMMANDS = frozenset({".天机盘", ".天星宗帮助"})
PREPARATION = frozenset({".观命", ".推命 探索", ".改命 探索", ".定命 太阴", ".定命 贪狼"})
GROUP_COMMANDS = READ_COMMANDS | PREPARATION | frozenset(COMMANDS.values())
SECOND_CHAT = GAME_SECOND_CHAT_ID


class LiveClient:
    def __init__(self, root, bridge, counters, *, clock=time.time, reader=None, writer=None):
        self.root, self.bridge, self.clock = Path(root), bridge, clock
        self.counters = counters
        self.reader = reader or JsonTransport({PREFIX+p: False for p in ("start", "details", "command-center")})
        self.writer = writer or JsonTransport({PREFIX+p: True for p in ("journey", "command-center")})
        self.session = None
        self.commands = set()
        self.peer = None

    def count(self, key):
        self.counters[key] = self.counters.get(key, 0) + 1

    async def open(self):
        await verify_username(self.bridge, ACCOUNT)
        self.count("miniapp_reads")
        self.session = await open_dwelling(self.bridge, self.reader)
        if not any(i.player_id == PLAYER_ID for i in self.session.identities):
            raise ExplorationError("personal_identity_missing")
        self.writer.bind_reward_account(ACCOUNT, self.session.init_data, self.root)
        detail = await self.details()
        if (detail.get("account", {}).get("sect") or {}).get("name") != "天星宗":
            raise ExplorationError("not_tianxing_sect")
        self.commands = catalog_commands(detail)
        return detail

    async def request(self, route, extra, *, mutation):
        if self.session is None:
            raise ExplorationError("miniapp_session_missing")
        self.count("miniapp_writes" if mutation else "miniapp_reads")
        try:
            reply = await (self.writer if mutation else self.reader)(
                PREFIX+route, {**extra, **self.session.base_payload(PLAYER_ID)}, mutation=mutation)
        except MiniAppError as exc:
            raise ExplorationError(exc.code, uncertain=exc.uncertain) from exc
        result = reply.get("actionResult") or {}
        code = result.get("error") or result.get("code") or reply.get("error") or reply.get("code")
        if (reply.get("ok") is False or result.get("ok") is False) and code == "command_not_allowed":
            raise ExplorationError("command_not_allowed")
        if (reply.get("ok") is False or result.get("ok") is False) and code in {
                "wild_experience_daily_limit", "wild_experience_unavailable", "wild_experience_cooldown"}:
            raise ExplorationError(code)
        account = reply.get("account") or {}
        if type(account.get("playerId")) is not int or account["playerId"] != PLAYER_ID:
            raise ExplorationError("miniapp_identity_mismatch", uncertain=mutation)
        if account.get("authUserId") is not None and account["authUserId"] != PLAYER_ID:
            raise ExplorationError("miniapp_auth_identity_mismatch", uncertain=mutation)
        if "command" in extra and result.get("command") != extra["command"]:
            raise ExplorationError("miniapp_command_mismatch", uncertain=mutation)
        if reply.get("ok") is not True:
            raise ExplorationError(str(code or "miniapp_rejected"), uncertain=mutation and not code)
        if route == "command-center" and result.get("completed") is not True:
            raise ExplorationError("miniapp_command_incomplete", uncertain=mutation)
        reply["_receipt"] = {"transport": "miniapp", "at": self.clock(), "command": extra.get("command")}
        return reply

    async def details(self):
        return await self.request("details", {}, mutation=False)

    async def command(self, command, *, on_sent=None):
        if command not in GROUP_COMMANDS:
            raise ExplorationError("command_not_allowlisted")
        mutation = command not in READ_COMMANDS
        if command.split()[0] in self.commands:
            try:
                return await self.request("command-center", {"command": command}, mutation=mutation)
            except ExplorationError as exc:
                # Only an explicit non-execution rejection permits fallback.
                if exc.code != "command_not_allowed" or exc.uncertain:
                    raise
                self.commands.discard(command.split()[0])
        return await self.group_command(command, on_sent=on_sent)

    async def journey(self):
        return await self.request("journey", {"action": "wild_experience", "mode": "deep"}, mutation=True)

    async def _peer(self):
        if self.peer is None:
            from telethon.utils import get_peer_id
            self.peer = await self.bridge.client.get_input_entity(CANONICAL_TARGET.lstrip("@"))
            if get_peer_id(self.peer) != CANONICAL_CHAT_ID:
                raise ExplorationError("canonical_group_mismatch")
        return self.peer

    @staticmethod
    def _matched(message, parent, chat):
        return (official_message(message, chat_id=chat)
                and getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None) == parent)

    @staticmethod
    def _ready(command, text):
        if command in COMMANDS.values():
            kind = next(k for k, v in COMMANDS.items() if v == command)
            return group_result(kind, text)["status"] != "unconfirmed"
        if command == ".天机盘":
            return "命中 / 落空 / 改命:" in text
        if command == ".观命":
            return "【" in text and any(t in text for t in ("显化", "命星"))
        if command.startswith(".定命"):
            return "你将今日命轨定在" in text
        if command.startswith(".推命"):
            return "8 小时" in text or "逆命劫" in text
        if command.startswith(".改命"):
            return "24 小时" in text or "无法" in text or "不足" in text
        return bool(text)

    def _envelope(self, command, sent, message, chat):
        at = message.date.timestamp()
        return {"ok": True, "account": {"playerId": PLAYER_ID},
                "actionResult": {"ok": True, "completed": True, "command": command,
                                 "message": message.raw_text or ""},
                "_receipt": {"transport": "group", "at": at, "command": command,
                             "chat_id": chat, "command_id": sent, "reply_id": message.id,
                             "sender_id": message.sender_id}}

    async def _reply(self, peer, chat, sent, command):
        self.count("history_reads")
        messages = await self.bridge.client.get_messages(peer, min_id=sent, reverse=True, limit=150)
        matches = [m for m in messages if self._matched(m, sent, chat)]
        if not matches:
            return None
        message = max(matches, key=lambda m: m.id)
        return self._envelope(command, sent, message, chat)

    async def group_command(self, command, *, on_sent=None):
        from telethon import events
        peer = await self._peer()
        ready, received = asyncio.Event(), []

        async def receive(event):
            message = event.message
            if official_message(message, chat_id=CANONICAL_CHAT_ID):
                received.append(message)
                del received[:-100]
                ready.set()

        new, edited = events.NewMessage(chats=peer), events.MessageEdited(chats=peer)
        self.bridge.client.add_event_handler(receive, new)
        self.bridge.client.add_event_handler(receive, edited)
        try:
            async def send():
                self.count("group_messages")
                return await self.bridge.client.send_message(peer, command, reply_to=CANONICAL_TOPIC_ID)

            os.environ.setdefault("TG_GLOBAL_SEND_GATE_STATE_PATH", str(self.root / "state/global_telegram_send_gate.json"))
            sent = await run_with_global_send_gate(send, label=f"{ACCOUNT}:tianxing:{command}",
                min_interval_seconds=2, state_dir=self.root / "state", log=lambda *_: None)
            if getattr(sent, "sender_id", None) != PLAYER_ID or plain(getattr(sent, "raw_text", "")) != command:
                raise ExplorationError("group_send_identity_mismatch", uncertain=True)
            if on_sent:
                value = on_sent({"command_id": int(sent.id), "chat_id": CANONICAL_CHAT_ID, "at": sent.date.timestamp()})
                if inspect.isawaitable(value):
                    await value
            end = time.monotonic() + 25
            while time.monotonic() < end:
                matches = [m for m in received if self._matched(m, sent.id, CANONICAL_CHAT_ID)]
                if matches:
                    message = matches[-1]
                    if self._ready(command, message.raw_text or ""):
                        return self._envelope(command, sent.id, message, CANONICAL_CHAT_ID)
                ready.clear()
                try:
                    await asyncio.wait_for(ready.wait(), timeout=min(3, max(.1, end - time.monotonic())))
                except TimeoutError:
                    pass
            reply = await self._reply(peer, CANONICAL_CHAT_ID, sent.id, command)
            if reply:
                return reply
            raise ExplorationError("group_reply_pending", uncertain=True)
        except ExplorationError:
            raise
        except Exception as exc:
            raise ExplorationError("group_send_or_reply_uncertain", uncertain=True) from exc
        finally:
            self.bridge.client.remove_event_handler(receive, new)
            self.bridge.client.remove_event_handler(receive, edited)

    async def recover_group(self, pending):
        peer = await self._peer()
        sent = pending.get("command_id")
        command = pending["command"]
        if sent is None:
            self.count("history_reads")
            me = await self.bridge.client.get_me()
            messages = await self.bridge.client.get_messages(peer, search=command, from_user=me, limit=5)
            matches = [m for m in messages if plain(m.raw_text) == command
                       and pending["at"] - 2 <= m.date.timestamp() <= pending["at"] + 120]
            if len(matches) != 1:
                return None
            sent = matches[0].id
        else:
            self.count("history_reads")
            original = await self.bridge.client.get_messages(peer, ids=int(sent))
            if not original or original.sender_id != PLAYER_ID or plain(original.raw_text) != command:
                raise ExplorationError("pending_group_identity_mismatch", uncertain=True)
        return await self._reply(peer, CANONICAL_CHAT_ID, sent, command)

    async def existing_actions(self):
        """Read recent own actions, including manual actions in the second group."""
        me = await self.bridge.client.get_me()
        found, observation = [], None
        for chat in (CANONICAL_CHAT_ID, SECOND_CHAT):
            try:
                peer = await self._peer() if chat == CANONICAL_CHAT_ID else await self.bridge.client.get_input_entity(chat)
                self.count("history_reads")
                own = await self.bridge.client.get_messages(peer, from_user=me, limit=100)
            except Exception as exc:
                # An incomplete initial history must not duplicate daily node actions.
                raise ExplorationError("own_history_unavailable") from exc
            for command in (*COMMANDS.values(), ".观命"):
                selected = next((m for m in own if plain(m.raw_text) == command), None)
                if selected is None:
                    self.count("history_reads")
                    older = await self.bridge.client.get_messages(peer, search=command, from_user=me, limit=1)
                    selected = next((m for m in older if plain(m.raw_text) == command), None)
                    if selected is None:
                        continue
                reply = await self._reply(peer, chat, selected.id, command)
                if not reply:
                    found.append({"command": command, "status": "pending", "at": selected.date.timestamp(),
                                  "command_id": selected.id, "chat_id": chat})
                    continue
                if command == ".观命":
                    if self._ready(command, action_text(reply)):
                        observation = max(observation or 0, selected.date.timestamp())
                    continue
                kind = next(k for k, v in COMMANDS.items() if v == command)
                parsed = group_result(kind, action_text(reply))
                found.append({"kind": kind, "command": command, **parsed, "receipt": reply["_receipt"],
                              "at": reply["_receipt"]["at"]})
        return {"actions": sorted(found, key=lambda row: row["at"]), "observed_day": day(observation) if observation else None}
