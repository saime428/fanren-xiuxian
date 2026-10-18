#!/usr/bin/env python3
"""共历心劫: the one 藏娇阁 feature that must run in the group, not the MiniApp.

Protocol observed in the canonical topic (2026-09-20/21, re-verified 2026-09-21 evening):

1. send ``.我的侍妾`` → official reply A (the status card of the active concubine)
2. send ``.共历心劫`` **as a reply to A** → official reply B
   ``【坠魔心劫·第一轮】…请回复本消息 .稳/.狠/.骗 进行抉择（共3轮）``
   or a refusal: ``侍妾正在远航途中，暂不可共历心劫`` / ``心劫余波未散，请在 … 后再试`` /
   ``你已有一场心劫抉择正在进行，请先完成当前抉择。``
3. send ``.稳`` **as a reply to B**, three times. The bot *edits B in place*:

   * after 稳 #1: ``【坠魔心劫·第1轮已定】…\\n\\n【坠魔心劫·第2轮】幻境再变，请继续回复 .稳 / .狠 / .骗。``
   * after 稳 #2: ``【坠魔心劫·第2轮已定】…\\n\\n【坠魔心劫·第3轮】…``
   * after 稳 #3: ``【坠魔心劫·结算】 三轮抉择：稳 / 稳 / 稳 … 修为结算 +N 情缘结算 +7 心魔值 -5``

   The first round is labelled with the Chinese numeral (第一轮) while the edited
   rounds use Arabic numerals (第2轮 / 第3轮). Both spellings are recognised; the
   2026-09-21 regression that stopped after a single ``.稳`` came from matching
   only 第二轮/第三轮. Replying to anything else yields ``心劫锚点已散，需重新引动天劫``.

稳/稳/稳 is the documented best line ("你以韩立式谨慎贯穿全局，收益最大"). Because every
round is 稳, a ``.稳`` re-sent after a missed edit can never pick a wrong choice, so
the chain follows the anchor's *text state* rather than counting its own sends.

A trial left half-finished blocks every later ``.共历心劫`` of the account
(``已有一场心劫抉择正在进行``). In that case the account's newest unsettled anchor
is looked up (our own ``.共历心劫`` → official reply still in a round state) and
driven to settlement first, then the intended trial is started.

The cooldown is 600 minutes (``last_companion_heart_tribulation_time``); a sailing
concubine is refused, so the loop runs this before ``.侍妾远航``.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from command_confirmation import CANONICAL_CHAT_ID, CANONICAL_TOPIC_ID, official_message
from miniapp_shared.daily import poll_until
from miniapp_shared.dwelling import MiniAppError
from miniapp_shared.priority import yield_to_qyz
from transport import fetch_message_by_id, send_with_global_gate

from .protocol import parse_minutes, plain

HEART_TRIAL_COOLDOWN_MIN = 600
STATUS_COMMAND = ".我的侍妾"
TRIAL_COMMAND = ".共历心劫"
CHOICE = ".稳"
ROUNDS = 3
# Every round is 稳, so a re-sent choice after a missed edit is harmless; allow
# two extra sends on top of the three rounds before giving up.
MAX_CHOICE_SENDS = ROUNDS + 2
REPLY_TIMEOUT_SECONDS = 45
ROUND_TIMEOUT_SECONDS = 60
PRE_CHOICE_DELAY_SECONDS = 2.0
SCAN_LIMIT = 40
# Resume lookup: how many of our own messages to walk back and how far after a
# ``.共历心劫`` the official anchor reply may sit.
OWN_SCAN_LIMIT = 400
OWN_TRIALS_TO_INSPECT = 6
REPLY_ID_WINDOW = 40

_ROUND_PENDING = re.compile(r"【坠魔心劫·第\s*([0-9一二三])\s*轮】")
_NUM = {"一": 1, "二": 2, "三": 3, "1": 1, "2": 2, "3": 3}


def classify_trial(text: str) -> dict[str, Any]:
    body = plain(text)
    if "坠魔心劫·结算" in body or "三轮抉择" in body:
        xw = re.search(r"修为结算[：:]\s*\+?(-?\d+)", body)
        aff = re.search(r"情缘结算[：:]\s*\+?(-?\d+)", body)
        demon = re.search(r"心魔值结算[：:]\s*(-?\d+)", body)
        choices = re.search(r"三轮抉择[：:]\s*`?([^`\n]+)`?", body)
        return {"status": "settled", "cultivation": int(xw.group(1)) if xw else None,
                "affection": int(aff.group(1)) if aff else None, "demon": int(demon.group(1)) if demon else None,
                "choices": choices.group(1).strip() if choices else ""}
    pending = _ROUND_PENDING.findall(body)
    if pending:
        # The open round is the last 【…第N轮】 block; ``第N轮已定`` blocks never match.
        return {"status": "round", "round": _NUM[pending[-1]]}
    if "远航途中" in body:
        return {"status": "sailing_blocked"}
    if "已有一场心劫" in body or "请先完成当前抉择" in body:
        return {"status": "in_progress"}
    if "余波未散" in body or "请在" in body:
        return {"status": "cooldown", "retry_minutes": parse_minutes(body)}
    if "锚点已散" in body:
        return {"status": "anchor_lost"}
    return {"status": "unknown"}


def _reply_id(message: Any) -> int | None:
    reply = getattr(message, "reply_to", None)
    return getattr(message, "reply_to_msg_id", None) or getattr(reply, "reply_to_msg_id", None)


class HeartTrialCommander:
    """Group message helper bound to one account's Telethon client."""

    def __init__(self, client: Any, *, state_dir, root=None, sleep: Any = asyncio.sleep,
                 monotonic: Any = time.monotonic, clock: Any = time.time) -> None:
        self.client, self.state_dir, self.root = client, state_dir, root
        self.sleep, self.monotonic, self.clock = sleep, monotonic, clock

    async def send(self, text: str, *, reply_to: int) -> int:
        # The chain can run for minutes after the tick-level check: re-check the
        # 青元子 window before every group message.
        if yield_to_qyz("pavilion_loop", self.clock(), self.root) is not None:
            raise MiniAppError("yielding_to_qyz")

        async def action():
            return await self.client.send_message(CANONICAL_CHAT_ID, text, reply_to=reply_to)
        message = await send_with_global_gate(action, label=f"pavilion_loop {text}", state_dir=self.state_dir)
        return int(getattr(message, "id", 0) or 0)

    async def wait_reply(self, sent_id: int, *, timeout: float = REPLY_TIMEOUT_SECONDS) -> dict[str, Any] | None:
        return await poll_until(lambda: self.official_reply(sent_id), timeout=timeout, sleep=self.sleep,
                                monotonic=self.monotonic)

    async def wait_edit(self, message_id: int, *, previous: str, timeout: float = ROUND_TIMEOUT_SECONDS) -> str | None:
        """The bot advances the trial by editing its own round message."""
        async def read() -> str | None:
            message = await fetch_message_by_id(self.client, CANONICAL_CHAT_ID, message_id)
            text = str(getattr(message, "message", "") or "") if message is not None else ""
            return text if text and text != previous else None

        return await poll_until(read, timeout=timeout, sleep=self.sleep, monotonic=self.monotonic)

    async def official_reply(self, sent_id: int) -> dict[str, Any] | None:
        """One-shot lookup of an official reply to our message (no waiting)."""
        async for message in self.client.iter_messages(CANONICAL_CHAT_ID, limit=SCAN_LIMIT, min_id=sent_id):
            if _reply_id(message) == sent_id and official_message(message):
                return {"id": int(message.id), "text": str(getattr(message, "message", "") or "")}
        return None

    async def find_pending_anchor(self) -> dict[str, Any] | None:
        """This account's newest unsettled 心劫 anchor: an official reply to our own ``.共历心劫``.

        Walks our own recent messages newest-first. A trial whose anchor is still in
        a round state is returned; a trial that was itself refused with
        ``已有一场心劫抉择正在进行`` is skipped (the blocking anchor is older); any
        other outcome (settled / refusal / no reply) means nothing is pending.
        """
        me = await self.client.get_me()
        inspected = 0
        async for message in self.client.iter_messages(CANONICAL_CHAT_ID, from_user=me, limit=OWN_SCAN_LIMIT):
            if str(getattr(message, "message", "") or "").strip() != TRIAL_COMMAND:
                continue
            sent_id = int(message.id)
            ids = list(range(sent_id + 1, sent_id + REPLY_ID_WINDOW + 1))
            candidates = await self.client.get_messages(CANONICAL_CHAT_ID, ids=ids)
            anchor = None
            for reply in (candidates if isinstance(candidates, (list, tuple)) else [candidates]):
                if reply is not None and _reply_id(reply) == sent_id and official_message(reply):
                    anchor = reply
                    break
            inspected += 1
            if anchor is None:
                return None
            text = str(getattr(anchor, "message", "") or "")
            parsed = classify_trial(text)
            if parsed["status"] == "round":
                return {"id": int(anchor.id), "text": text, "round": parsed["round"], "trial_message_id": sent_id}
            if parsed["status"] != "in_progress":
                return None
            if inspected >= OWN_TRIALS_TO_INSPECT:
                return None
        return None


async def drive_rounds(commander: HeartTrialCommander, anchor_id: int, current: str, trace: list[dict[str, Any]],
                       *, resumed: bool = False) -> dict[str, Any]:
    """Reply ``.稳`` to the anchor until its text reaches 结算, following the text state."""
    parsed = classify_trial(current)
    sends = 0
    advanced = 0
    await commander.sleep(PRE_CHOICE_DELAY_SECONDS)
    while parsed["status"] == "round" and sends < MAX_CHOICE_SENDS:
        choice_id = await commander.send(CHOICE, reply_to=anchor_id)
        sends += 1
        trace.append({"command": CHOICE, "round": parsed["round"], "sent_message_id": choice_id})
        edited = await commander.wait_edit(anchor_id, previous=current) if choice_id else None
        if edited is None:
            if not choice_id:
                break
            reply = await commander.official_reply(choice_id)
            if reply is not None:
                # The bot answered with a new message instead of editing: a refusal such as 锚点已散.
                verdict = classify_trial(reply["text"])
                trace[-1]["reply_message_id"] = reply["id"]
                return {**verdict, "text": plain(reply["text"])[:200], "anchor_message_id": anchor_id,
                        "choice_sends": sends, "resumed": resumed, "trace": trace}
            if resumed:
                break  # a stale anchor that no longer reacts is not worth more sends
            continue  # the bot may have missed or raced the reply: re-send the same safe choice
        advanced += 1
        current = edited
        parsed = classify_trial(current)
    base = {"anchor_message_id": anchor_id, "choice_sends": sends, "resumed": resumed, "trace": trace}
    if parsed["status"] == "settled":
        return {**parsed, "rounds": advanced, **base}
    if parsed["status"] == "round":
        return {"status": "unconfirmed", "step": f"round_{parsed['round']}", "text": plain(current)[:200], **base}
    return {**parsed, "text": plain(current)[:200], **base}


async def run_heart_trial(commander: HeartTrialCommander, *, companion_name: str) -> dict[str, Any]:
    trace: list[dict[str, Any]] = []
    status_id = await commander.send(STATUS_COMMAND, reply_to=CANONICAL_TOPIC_ID)
    card = await commander.wait_reply(status_id) if status_id else None
    trace.append({"command": STATUS_COMMAND, "sent_message_id": status_id, "reply_message_id": card and card["id"]})
    if card is None:
        return {"status": "unconfirmed", "step": "status", "trace": trace}
    if companion_name and f"【{companion_name}】" not in card["text"].split("---")[0]:
        # The group card leads with the active concubine; refuse to fight for the wrong one.
        return {"status": "companion_mismatch", "trace": trace, "text": plain(card["text"])[:200]}

    async def start_trial() -> dict[str, Any] | None:
        trial_id = await commander.send(TRIAL_COMMAND, reply_to=card["id"])
        anchor = await commander.wait_reply(trial_id) if trial_id else None
        trace.append({"command": TRIAL_COMMAND, "sent_message_id": trial_id, "reply_message_id": anchor and anchor["id"]})
        return anchor

    anchor = await start_trial()
    if anchor is None:
        return {"status": "unconfirmed", "step": "trial", "trace": trace}
    parsed = classify_trial(anchor["text"])
    resumed_settlement: dict[str, Any] | None = None
    if parsed["status"] == "in_progress":
        pending = await commander.find_pending_anchor()
        trace.append({"command": "resume", "anchor_message_id": pending and pending["id"],
                      "round": pending and pending["round"]})
        if pending is None:
            return {**parsed, "trace": trace, "text": plain(anchor["text"])[:200]}
        resumed = await drive_rounds(commander, pending["id"], pending["text"], trace, resumed=True)
        if resumed.get("status") != "settled":
            return resumed
        resumed_settlement = {k: resumed.get(k) for k in ("cultivation", "affection", "demon", "anchor_message_id")}
        # The stale trial is closed and the account lock released: start the intended one.
        anchor = await start_trial()
        if anchor is None:
            return {"status": "unconfirmed", "step": "trial", "trace": trace, "resumed_settlement": resumed_settlement}
        parsed = classify_trial(anchor["text"])
    if parsed["status"] != "round":
        result = {**parsed, "trace": trace, "text": plain(anchor["text"])[:200]}
    else:
        result = await drive_rounds(commander, anchor["id"], anchor["text"], trace)
    if resumed_settlement is not None:
        result["resumed_settlement"] = resumed_settlement
    return result
