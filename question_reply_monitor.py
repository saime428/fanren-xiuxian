"""Process account-bound game prompts without waiting for history searches."""
from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from telethon import errors, events, utils

from event_monitor_runtime import question_monitor_version
from nanlonghou_trade import (TRADE_COMMANDS, TRADE_FLOWS, TRADE_MAX_AGE_SECONDS, TRADE_PENDING,
                              offered_trade_commands, parse_trade_result, trade_companion, trade_message_kind)
import xuangu_quiz as xuangu
from message_fact_store import MessageFactStore
from runtime import env, log_print
from transport import send_with_global_gate, with_telegram_retry, ensure_connected, collect_direct_replies_to_message
from event_command_receipts import FIXED_EVENT_FLOWS, fixed_event_receipt
from account_lifecycle import AccountPausedError
from command_confirmation import CANONICAL_CHAT_ID, CANONICAL_TARGET, CANONICAL_TOPIC_ID
from state_store import flow_lock, is_paused

JIYIN_COMMANDS = {".献上魂魄", ".收敛气息"}
CHOICE_WINDOW_RE = re.compile(r"(?:你必[须須]\s*在|你有)\s*(\d+)\s*(秒|分[钟鐘]|小[时時])")
UNCERTAIN = {"send_submitted", "send_outcome_unknown"}
PAUSED_RETRY_SECONDS = 600  # a shattered account stays paused for hours; do not spin on it
RETRYABLE = {"pending", "retryable_error", "answer_unavailable", "reply_retry_pending"}
COMMAND_TERMINAL = {"command_confirmed", "command_rejected"}
RECEIPT_FLOWS = FIXED_EVENT_FLOWS | TRADE_FLOWS | {xuangu.FLOW}
WAITING_RESULT = UNCERTAIN | TRADE_PENDING | xuangu.PENDING | {
    "awaiting_command_result", "command_reply_unclassified", "command_result_unconfirmed"}
BUSINESS_ERRORS = {"send_outcome_unknown", "retryable_error", "answer_unavailable", "expired_unanswered", "question_missing", "reply_rejected", "trade_result_unconfirmed", "command_result_unconfirmed", "command_rejected", "command_reply_unclassified"} | xuangu.BUSINESS_ERRORS


def reply_parent(message):
    return (getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None)
            or getattr(message, "reply_to_msg_id", None))


def message_owned_by(message, username: str, user_id: int) -> bool:
    text = str(getattr(message, "message", "") or "")
    if username and re.search(r"(?<![\w@])@" + re.escape(username.lstrip("@")) + r"(?![A-Za-z0-9_])", text, re.I):
        return True
    for entity in getattr(message, "entities", None) or []:
        if getattr(entity, "user_id", None) == user_id:
            return True
        url = str(getattr(entity, "url", "") or "")
        if not url.startswith("tg://user?"):
            continue
        parsed = urlsplit(url)
        if parsed.scheme == "tg" and parsed.netloc == "user" and parse_qs(parsed.query).get("id") == [str(user_id)]:
            return True
    return False


def question_kind(text: str, flow: str) -> str | None:
    if flow == "shenji_budao":
        return "question" if "小世界·天降浩劫" in text and any(word in text for word in ("安抚信徒", "小世界")) else None
    if flow == xuangu.FLOW and xuangu.parse_result(text):
        return "answer_result"
    if any(marker in text for marker in ("题面失效", "題面失效", "未能及时作答", "未能及時作答", "·超时", "·超時")):
        if flow != xuangu.FLOW and xuangu.event_variant(text):
            return None
        return "expired"
    if flow in TRADE_FLOWS:
        return trade_message_kind(text)
    if flow == "jiyin_choice":
        # The announcement and the actionable choice are separate messages,
        # sometimes from different official Bots. Only the latter is a reply
        # target; it need not repeat the NPC's name.
        if (all(command in text for command in JIYIN_COMMANDS)
                and re.search(r"回[复覆復]本(?:消息|訊息)", text)):
            return "question"
        if "魔君降临" in text or "魔君降臨" in text:
            return "notice"
        return None
    if flow == xuangu.FLOW:
        if xuangu.parse_question(text):
            return "question"
        if xuangu.notice_variant(text):
            return "notice"
    return None


def question_max_age(text: str, flow: str, configured_max_age: float) -> float:
    if flow == xuangu.FLOW:
        configured_max_age = min(configured_max_age, xuangu.MAX_AGE_SECONDS)
    if flow in TRADE_FLOWS:
        configured_max_age = min(configured_max_age, TRADE_MAX_AGE_SECONDS)
    if flow not in {"jiyin_choice", xuangu.FLOW} and flow not in TRADE_FLOWS:
        return configured_max_age
    windows = [int(amount) * (1 if unit == "秒" else 60 if unit.startswith("分") else 3600)
               for amount, unit in CHOICE_WINDOW_RE.findall(text)]
    return min([configured_max_age, *windows])


def message_topic(message):
    reply = getattr(message, "reply_to", None)
    return getattr(reply, "reply_to_top_id", None) or getattr(reply, "reply_to_msg_id", None)


def write_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.write("\n")
        os.replace(name, path)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(name)


class QuestionReplyMonitor:
    def __init__(self, client, health, sources, *, username, trusted_sender_ids,
                 flow, state_dir, state_path, max_age, scan_limit, reconcile_seconds,
                 reply_text, answer_bank=(), answer_bank_path=None, auto_update_answer_bank=False,
                 result_marker_groups=(), result_pattern=r"答案\s*([A-D])\s*完全正[确確]",
                 learned_max_age=86400, dry_run=False, clock=time.time):
        self.client, self.health, self.sources = client, health, sources
        self.username, self.trusted = username, set(trusted_sender_ids)
        self.flow, self.account = flow, health.data["account"]
        self.user_id = int(self.account[2:])
        self.state_dir, self.state_path = Path(state_dir), Path(state_path)
        expected_tail = ("state", "accounts", self.account, self.flow)
        self.command_root = (self.state_dir.parents[3] if self.state_dir.parts[-4:] == expected_tail
                             else self.state_dir.parent)
        if self.flow == "shenji_budao":
            from miniapp_commands.catalog import migrated, VERSION
            if migrated(self.command_root, self.account, self.flow):
                self.health.data.update(command_transport="miniapp", command_transport_version=VERSION)
        self.journal_path = self.state_dir / "question_state.json"
        self.max_age = min(max_age, TRADE_MAX_AGE_SECONDS) if flow in TRADE_FLOWS else max_age
        if flow == xuangu.FLOW:
            self.max_age = min(max_age, xuangu.MAX_AGE_SECONDS)
        self.scan_limit = scan_limit
        self.reconcile_seconds, self.reply_text = reconcile_seconds, reply_text
        self.reply_wait = max(15.0, float(env("TG_EVENT_REPLY_RETRY_SECONDS", "30" if flow == xuangu.FLOW else "60")))
        self.answer_bank, self.clock = list(answer_bank), clock
        self.answer_bank_path = Path(answer_bank_path) if answer_bank_path else None
        self.learned_path = self.state_dir / "question_answer_bank.json"
        self.auto_update_answer_bank = auto_update_answer_bank and flow == "xuangu_zuoda"
        self.result_marker_groups, self.result_pattern = result_marker_groups, re.compile(result_pattern)
        self.learned_max_age = learned_max_age
        self.dry_run = dry_run
        self.learned_results = set()
        self.learned_questions = {}
        self.parent_cache = {}
        self.outbound_replies = {}
        self.source_by_chat = {utils.get_peer_id(entity): (target, entity) for target, entity in sources}
        if CANONICAL_CHAT_ID not in self.source_by_chat:
            raise ValueError("canonical outbound chat missing")
        self.outbound = self.source_by_chat[CANONICAL_CHAT_ID][1]
        self.queue = asyncio.Queue()
        self.queued = set()
        self.load_state()
        self.reload_answer_bank()
        self.health.data["question_monitor_version"] = question_monitor_version(self.flow)
        self.health.data["receipt_policy"] = {"official_receipt_required": True, "retry_without_reply": True,
                                              "retry_interval_seconds": self.reply_wait}
        if self.flow in TRADE_FLOWS:
            self.health.data["trade_policy"] = {
                "reply_text": self.reply_text, "max_event_age_seconds": self.max_age,
                "official_receipt_required": True,
            }
        if self.flow == xuangu.FLOW:
            self.health.data["answer_policy"] = {
                "question_bound_answers": True, "max_event_age_seconds": self.max_age,
                "official_receipt_required": True, "reply_to_original_question": True,
            }

    def load_state(self):
        self.rows = {}
        self.trade_receipts = {}
        self.answer_receipts = {}
        self.command_receipts = {}
        self.last_state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        self.cursors = dict(self.last_state.get("handled_event_ids_by_chat") or {})
        if not self.cursors and self.last_state.get("event_message_id"):
            old_source = str(self.last_state.get("source_chat") or self.last_state.get("target") or CANONICAL_TARGET)
            for chat_id, (target, entity) in self.source_by_chat.items():
                if old_source in {str(target), str(chat_id), str(entity.id)}:
                    self.cursors[str(entity.id)] = int(self.last_state["event_message_id"])
        if self.journal_path.exists():
            journal = json.loads(self.journal_path.read_text())
            if journal.get("account") != self.account or journal.get("flow") != self.flow:
                raise ValueError("question journal identity mismatch")
            if self.flow in TRADE_FLOWS:
                self.trade_receipts = {self.key(row["chat_id"], row["message_id"]): row
                                       for row in journal.get("trade_receipts", [])}
            if self.flow == xuangu.FLOW:
                self.answer_receipts = {self.key(row["chat_id"], row["message_id"]): row
                                       for row in journal.get("answer_receipts", [])}
            self.command_receipts = {self.key(row["chat_id"], row["message_id"]): row
                                     for row in journal.get("command_receipts", [])}
            for row in journal["events"]:
                if row["status"] == "send_submitted":
                    row["status"] = "send_outcome_unknown"
                if self.flow == xuangu.FLOW and row["status"] in {"sent", "already_replied"}:
                    row["status"] = "awaiting_answer_result"
                if self.flow in FIXED_EVENT_FLOWS and row["status"] in {"sent", "already_replied"}:
                    row["status"] = "awaiting_command_result"
                if row.get("sent_message_id"):
                    row.setdefault("sent_message_ids", [row["sent_message_id"]])
                row.setdefault("retry_at_ts", float(row.get("sent_at_ts") or row.get("submitted_at_ts") or row["date_ts"]) + self.reply_wait
                               if row["status"] in WAITING_RESULT else 0)
                row["retry_at_ts"] = self.retry_not_before(row)
                self.rows[self.key(row["chat_id"], row["message_id"])] = row
        if self.flow in FIXED_EVENT_FLOWS and self.last_state.get("status") in {"sent", "already_replied"}:
            old = self.last_state
            for chat_id, (target, entity) in self.source_by_chat.items():
                if str(old.get("source_chat", old.get("target", ""))) not in {str(target), str(chat_id), str(entity.id)}:
                    continue
                key = self.key(chat_id, old.get("event_message_id"))
                if key not in self.rows and old.get("event_text") and old.get("event_message_id"):
                    ts = float(old.get("ts") or self.clock())
                    self.rows[key] = {"chat_id": chat_id, "message_id": old["event_message_id"], "kind": "question",
                        "date_ts": ts, "text": old["event_text"], "status": "awaiting_command_result",
                        "reply_text": old.get("reply_text", self.reply_text), "sent_message_id": old.get("sent_message_id"),
                        "sent_message_ids": [old["sent_message_id"]] if old.get("sent_message_id") else [],
                        "send_reply_to": CANONICAL_TOPIC_ID if self.flow == "shenji_budao" else old["event_message_id"],
                        "submitted_at_ts": ts, "sent_at_ts": ts, "retry_at_ts": ts + self.reply_wait, "legacy_unconfirmed": True}
        self.update_business_issue()

    @staticmethod
    def key(chat_id, message_id):
        return f"{chat_id}:{message_id}"

    def persist(self):
        ordered = sorted(self.rows.values(), key=lambda row: row["date_ts"])
        retained = [row for index, row in enumerate(ordered)
                    if index >= len(ordered) - 40 or row["status"] in WAITING_RESULT | RETRYABLE
                    or self.key(row["chat_id"], row["message_id"]) in self.queued]
        self.rows = {self.key(row["chat_id"], row["message_id"]): row for row in retained}
        payload = {"version": 1, "account": self.account, "flow": self.flow,
                   "updated_at_ts": self.clock(), "events": retained}
        if self.flow in FIXED_EVENT_FLOWS or self.command_receipts:
            payload["command_receipts"] = sorted(self.command_receipts.values(), key=lambda item: item["date_ts"])[-80:]
        if self.flow in TRADE_FLOWS:
            receipts = sorted(self.trade_receipts.values(), key=lambda item: item["date_ts"])[-80:]
            self.trade_receipts = {self.key(item["chat_id"], item["message_id"]): item for item in receipts}
            payload["trade_receipts"] = receipts
        if self.flow == xuangu.FLOW:
            receipts = sorted(self.answer_receipts.values(), key=lambda item: item["date_ts"])[-80:]
            self.answer_receipts = {self.key(item["chat_id"], item["message_id"]): item for item in receipts}
            payload["answer_receipts"] = receipts
        write_atomic(self.journal_path, payload)
        self.update_business_issue()
        self.health._write()

    def update_business_issue(self):
        ordered = sorted(self.rows.values(), key=lambda row: row["date_ts"])
        pending = [row for row in ordered if row["status"] in WAITING_RESULT | {"reply_retry_pending"}]
        self.health.data["pending_receipts"] = len(pending)
        self.health.data["pending_command"] = ({"message_id": pending[0]["message_id"],
            "status": pending[0]["status"], "retry_at_ts": self.retry_not_before(pending[0]),
            "server_retry_at_ts": pending[0].get("server_retry_at_ts"),
            "sent_message_ids": pending[0].get("sent_message_ids", [])} if pending else None)
        unresolved = next((row for row in ordered if row["status"] in {
            "answer_result_unconfirmed", "trade_result_unconfirmed", "command_result_unconfirmed"}), None)
        # A new notice or heartbeat is not evidence that a previous failure
        # recovered. Xuangu recovery must also be in the same event variant.
        tracked = (BUSINESS_ERRORS - WAITING_RESULT) | {"trade_confirmed", "trade_already_observed", "answer_confirmed", "answer_already_observed", "command_confirmed"}
        if self.flow == xuangu.FLOW:
            latest_by_variant = {}
            for row in ordered:
                if row["status"] in tracked:
                    latest_by_variant[self.row_variant(row)] = row
            latest = min((row for row in latest_by_variant.values() if row["status"] in BUSINESS_ERRORS),
                         key=lambda row: row["date_ts"], default=None)
        else:
            latest = next((row for row in reversed(ordered) if row["status"] in tracked), None)
        issue = unresolved or (latest if latest and latest["status"] in BUSINESS_ERRORS else None)
        self.health.data["business_issue"] = ({"code": issue["status"], "message_id": issue["message_id"],
                                               "chat_id": issue["chat_id"], "since_ts": issue["date_ts"]} if issue else None)
        if issue and self.flow == xuangu.FLOW:
            self.health.data["business_issue"]["variant"] = self.row_variant(issue)

    @staticmethod
    def row_variant(row):
        variant = row.get("variant")
        return variant if variant in xuangu.VARIANTS else xuangu.event_variant(row.get("text", ""))

    def owned(self, message, chat_id):
        if message_owned_by(message, self.username, self.user_id):
            return True
        # An explicit different account cannot inherit ownership merely by
        # replying to this account's notice.
        text = str(getattr(message, "message", "") or "")
        if re.search(r"@[A-Za-z0-9_]+", text) or any(
                getattr(entity, "user_id", None) or str(getattr(entity, "url", "")).startswith("tg://user?")
                for entity in getattr(message, "entities", None) or []):
            return False
        parent = reply_parent(message)
        row = self.rows.get(self.key(chat_id, parent))
        if not (row and row.get("kind") == "notice" and row["status"] in {"waiting_question", "question_missing", "question_received"}):
            return False
        if self.flow == xuangu.FLOW:
            question = xuangu.parse_question(text)
            return bool(question and self.row_variant(row) == question["variant"])
        return True

    def eligible(self, message, chat_id):
        if chat_id not in self.source_by_chat or int(getattr(message, "sender_id", 0) or 0) not in self.trusted:
            return False
        if getattr(message, "chat_id", chat_id) != chat_id:
            return False
        if getattr(message, "fwd_from", None) is not None:
            return False
        date = getattr(message, "date", None)
        max_age = question_max_age(str(getattr(message, "message", "") or ""), self.flow, self.max_age)
        if date is None or not -30 <= self.clock() - date.timestamp() < max_age:
            return False
        forwarded = getattr(getattr(message, "fwd_from", None), "date", None)
        if forwarded is not None and self.clock() - forwarded.timestamp() >= max_age:
            return False
        return self.owned(message, chat_id)

    def deadline(self, row):
        if self.flow == "shenji_budao" and (row.get("submitted_at_ts") or row.get("sent_at_ts")
                                             or row.get("miniapp_queued_at_ts")):
            # The 15-minute discovery window is not a server expiry for 布道.
            return float("inf")
        parsed = row["date_ts"] + question_max_age(row.get("text", ""), self.flow, self.max_age)
        return min(row.get("deadline_ts", parsed), parsed)

    @staticmethod
    def retry_not_before(row, default=0):
        return max(float(row.get("retry_at_ts", default) or 0),
                   float(row.get("server_retry_at_ts") or 0))

    def fixed_choice_readonly_source(self, message, chat_id):
        return (self.flow in {"jiyin_choice", xuangu.FLOW} or self.flow in TRADE_FLOWS) and (
            chat_id != CANONICAL_CHAT_ID or message_topic(message) != CANONICAL_TOPIC_ID)

    def fixed_choice_commands(self):
        if self.flow == xuangu.FLOW:
            return xuangu.COMMANDS
        if self.flow == "shenji_budao":
            return {".神迹 布道"}
        return TRADE_COMMANDS if self.flow in TRADE_FLOWS else JIYIN_COMMANDS if self.flow == "jiyin_choice" else set()

    def outbound_text(self, message):
        text = str(getattr(message, "message", "") or "").strip()
        return xuangu.command(text) if self.flow == xuangu.FLOW else text

    def observe(self, message, chat_id):
        if self.flow == xuangu.FLOW and (
                getattr(message, "chat_id", None) != chat_id or getattr(message, "fwd_from", None) is not None):
            return
        self.observe_outbound(message, chat_id)
        if self.flow in RECEIPT_FLOWS:
            self.observe_command_receipt(message, chat_id)
        text = str(getattr(message, "message", "") or "")
        kind = question_kind(text, self.flow)
        if kind is None:
            return
        if kind == "trade_result":
            self.observe_trade_receipt(message, chat_id)
            return
        if kind == "answer_result":
            self.observe_answer_receipt(message, chat_id)
            return
        key = self.key(chat_id, getattr(message, "id", 0))
        row = self.rows.get(key)
        if kind == "expired":
            if (row and self.flow == xuangu.FLOW and (variant := xuangu.event_variant(text)) is not None
                    and variant != self.row_variant(row)):
                return
            if (row and chat_id in self.source_by_chat and getattr(message, "sender_id", None) in self.trusted
                    and self.owned(message, chat_id) and row["status"] not in UNCERTAIN | xuangu.TERMINAL_RESULTS | {"readonly_source", "trade_confirmed", "trade_already_observed"}):
                row["status"] = "reply_rejected" if row["status"] in {"sent", "awaiting_trade_result", "awaiting_answer_result", "answer_result_unconfirmed"} else "expired_unanswered"
                self.persist()
            return
        if (row and kind == "question" and row["status"] in RETRYABLE and chat_id in self.source_by_chat
                and getattr(message, "sender_id", None) in self.trusted):
            # A pushed edit may shorten (never extend) a known question's
            # window, even after the shorter window has already closed; the
            # send-slot re-check then refuses it without another history read.
            shorter = row["date_ts"] + question_max_age(text, self.flow, self.max_age)
            if shorter < self.deadline(row):
                row["deadline_ts"] = shorter
                self.persist()
        if not self.eligible(message, chat_id):
            return
        chat_key = str(getattr(self.source_by_chat[chat_id][1], "id", chat_id))
        if int(message.id) <= int(self.cursors.get(chat_key) or 0):
            return
        if row and row["status"] in WAITING_RESULT | COMMAND_TERMINAL | xuangu.TERMINAL_RESULTS | {"sent", "already_replied", "trade_confirmed", "trade_already_observed", "expired_unanswered", "reply_rejected", "reply_retry_pending"}:
            return
        if row and row["status"] == "answer_unavailable" and row.get("text") == text and self.answer(text) is None:
            return
        if row is None:
            row = {"message_id": message.id, "chat_id": chat_id, "date_ts": message.date.timestamp(),
                   "sender_id": message.sender_id, "kind": kind, "status": "waiting_question" if kind == "notice" else "pending"}
            self.rows[key] = row
        old_deadline = self.deadline(row)
        row["text"] = text
        row["kind"] = kind
        if self.flow == xuangu.FLOW:
            row["variant"] = xuangu.event_variant(text)
        row["deadline_ts"] = min(old_deadline, self.deadline(row))
        if self.fixed_choice_readonly_source(message, chat_id):
            # A reply-bound choice cannot be relocated to a different group or
            # topic. Retain the observation without sending a root command.
            row["status"] = "readonly_source"
            self.persist()
            return
        if kind == "question":
            # Notices and actual questions can have unrelated messages between
            # them. Only explicit ownership (or an exact reply) binds a question.
            notices = [notice for notice in self.rows.values()
                       if notice is not row and notice["chat_id"] == chat_id and notice["status"] in {"waiting_question", "question_missing"}
                       and (self.flow != xuangu.FLOW or self.row_variant(notice) == row["variant"])
                       and 0 <= row["date_ts"] - notice["date_ts"] <= self.max_age]
            if notices:
                notice = max(notices, key=lambda item: (item["message_id"] == reply_parent(message), item["date_ts"]))
                notice.update(status="question_received", question_message_id=message.id)
            row["status"] = "pending"
            if key not in self.queued:
                self.queued.add(key)
                self.queue.put_nowait(key)
        self.persist()

    def answer(self, text):
        if self.flow == xuangu.FLOW:
            return xuangu.resolve_answer(text, self.answer_bank, self.learned_questions)
        if self.flow in TRADE_FLOWS:
            return self.reply_text if self.reply_text in offered_trade_commands(text) else None
        if self.flow == "jiyin_choice":
            return self.reply_text
        if self.flow == "shenji_budao":
            return ".神迹 布道"
        return None

    def observe_outbound(self, message, chat_id):
        if chat_id != CANONICAL_CHAT_ID or getattr(message, "sender_id", None) != self.user_id:
            return
        if getattr(message, "fwd_from", None) is not None:
            return
        commands = self.fixed_choice_commands()
        if commands and self.outbound_text(message) not in commands:
            return
        parent = reply_parent(message)
        if self.flow == "shenji_budao" and parent == CANONICAL_TOPIC_ID:
            date = getattr(message, "date", None)
            candidates = [row for row in self.rows.values() if date and row.get("submitted_at_ts")
                          and int(row.get("last_submitted_at_ts", row["submitted_at_ts"])) <= date.timestamp()
                          <= row.get("last_submitted_at_ts", row["submitted_at_ts"]) + 60
                          and row["status"] in WAITING_RESULT | RETRYABLE]
            if len(candidates) == 1:
                self.outbound_replies[candidates[0]["message_id"]] = message
            return
        if not parent or parent == CANONICAL_TOPIC_ID:
            return
        self.outbound_replies[parent] = message
        if len(self.outbound_replies) > self.scan_limit:
            self.outbound_replies.pop(next(iter(self.outbound_replies)))

    def matching_outbound(self, row, message):
        if (not message or getattr(message, "chat_id", None) != CANONICAL_CHAT_ID
                or getattr(message, "sender_id", None) != self.user_id
                or self.outbound_text(message) != row.get("reply_text")
                or reply_parent(message) != row.get("send_reply_to")):
            return False
        date = getattr(message, "date", None)
        return bool(date and row["date_ts"] <= date.timestamp() <= self.deadline(row) + 10)

    def record_sent(self, row, message, *, recovered=False):
        status = "already_replied" if recovered else "sent"
        if self.flow in TRADE_FLOWS:
            status = ("trade_confirmed" if row["status"] == "trade_confirmed" else
                      "already_replied" if row["reply_text"] == ".拒绝交易" else "awaiting_trade_result")
        if self.flow == xuangu.FLOW:
            status = row["status"] if row["status"] in xuangu.TERMINAL_RESULTS else "awaiting_answer_result"
        if self.flow in FIXED_EVENT_FLOWS:
            status = row["status"] if row["status"] in COMMAND_TERMINAL else "awaiting_command_result"
        sent_ts = getattr(message, "date", None).timestamp() if getattr(message, "date", None) else self.clock()
        ids = row.setdefault("sent_message_ids", [])
        if message.id not in ids:
            ids.append(message.id)
        row.setdefault("sent_at_ts", sent_ts)
        row.update(status=status, sent_message_id=message.id, last_sent_at_ts=sent_ts,
                   retry_at_ts=max(sent_ts + self.reply_wait, row.get("server_retry_at_ts", 0)))
        # Preserve the acknowledged Telegram ID before writing secondary facts.
        # If a subsequent write fails, recovery can verify this exact ID.
        self.persist()
        MessageFactStore(Path(env("TG_MESSAGE_LOG_PATH", str(self.state_dir / "messages.jsonl")))).append({
            "event_type": "message", "identity_id": self.user_id, "sender_id": self.user_id,
            "chat_id": CANONICAL_CHAT_ID, "message_id": message.id, "text": row["reply_text"],
            "reply_to_message_id": row["send_reply_to"], "topic_id": CANONICAL_TOPIC_ID,
            "flow_slug": self.flow, "reply_source": "auto_reply_topic_event",
            "source_chat_id": row["chat_id"], "event_message_id": row["message_id"],
        })
        source_target, source_entity = self.source_by_chat[row["chat_id"]]
        chat_key = str(getattr(source_entity, "id", row["chat_id"]))
        self.cursors[chat_key] = max(int(self.cursors.get(chat_key) or 0), row["message_id"])
        self.last_state = {"ts": self.clock(), "status": row["status"], "flow_slug": self.flow,
                           "event_message_id": row["message_id"], "reply_target_message_id": row["message_id"],
                           "sent_message_id": message.id, "reply_text": row["reply_text"],
                           "target": CANONICAL_TARGET, "topic_id": CANONICAL_TOPIC_ID,
                           "source_chat": str(source_target), "trigger_target_username": self.username,
                           "event_text": row["text"], "handled_event_ids_by_chat": self.cursors}
        if self.flow in TRADE_FLOWS or self.flow == xuangu.FLOW or self.flow in FIXED_EVENT_FLOWS:
            # A Telegram acknowledgement proves delivery, not an accepted
            # exchange. Only a matching official receipt writes last_success.
            write_atomic(self.state_dir / "last_attempt.json", self.last_state)
            self.reconcile_trade_receipts()
            if row["status"] == "trade_confirmed":
                self.write_trade_success(row)
            if self.flow == xuangu.FLOW:
                self.reconcile_answer_receipts()
                self.write_answer_success(row)
        else:
            write_atomic(self.state_dir / "last_attempt.json", self.last_state)
        log_print(f"Question reply {'reconciled' if recovered else 'sent'}: {self.account}/{self.flow} "
                  f"question={row['message_id']} sent={message.id}")

    @staticmethod
    def reply_ids(row):
        return {row.get("message_id"), row.get("sent_message_id"), *row.get("sent_message_ids", [])} - {None}

    def receipt_age_allowed(self, message, chat_id, timestamp):
        age = self.clock() - timestamp
        if age < -30:
            return False
        if age <= self.learned_max_age:
            return True
        # The history/learning horizon must not discard a late read of an
        # exact official reply to a journalled action after a long shutdown.
        # Broadcasts still require recent, uniquely attributable evidence.
        parent = reply_parent(message)
        return chat_id == CANONICAL_CHAT_ID and any(
            row.get("kind") == "question" and row.get("chat_id") == chat_id
            and row.get("submitted_at_ts", row.get("sent_at_ts")) is not None
            and timestamp >= int(row.get("submitted_at_ts", row.get("sent_at_ts")))
            and parent in self.reply_ids(row)
            for row in self.rows.values()
        )

    async def reconcile_command_copies(self, row, *, limit=2):
        """Rotate bounded exact lookups while retaining every submitted copy.

        Recent history/push still matches all copies. An unavailable old
        Telegram reply thread must not starve new retries or source health.
        """
        checked_at = row.setdefault("reply_scans_by_message", {})
        interval = 60 if self.clock() < self.deadline(row) else 300
        ids = [mid for mid in row.get("sent_message_ids", [row.get("sent_message_id")]) if mid]
        due = [mid for mid in ids if self.clock() - checked_at.get(str(mid), 0) >= interval]
        count = 0
        for mid in sorted(due, key=lambda item: checked_at.get(str(item), 0))[:limit]:
            checked_at[str(mid)] = self.clock()
            row["last_reply_scan_ts"] = self.clock()
            count += 1
            try:
                replies = await asyncio.wait_for(collect_direct_replies_to_message(
                    self.client, self.outbound, mid, self.scan_limit), 10)
            except (TimeoutError, OSError) as exc:
                row["receipt_lookup_error_type"] = type(exc).__name__
                row["receipt_lookup_error_ts"] = self.clock()
                continue
            for item in sorted(replies, key=lambda msg: getattr(msg, "id", 0)):
                self.observe(item, CANONICAL_CHAT_ID)
        return count

    async def reconcile_event_broadcasts(self):
        # Some Tianzun outcomes are account-named broadcasts in the secondary
        # group, not replies to a command. Recover one pending event's bounded
        # time range, then let the usual owner/command/uniqueness checks decide.
        for row in sorted(self.rows.values(), key=lambda item: item.get("last_broadcast_scan_ts", 0)):
            submitted = row.get("submitted_at_ts", row.get("sent_at_ts"))
            if (row["status"] not in WAITING_RESULT | RETRYABLE or submitted is None
                    or self.clock() - submitted < self.reply_wait
                    or self.clock() - row.get("last_broadcast_scan_ts", 0) < 300):
                continue
            row["last_broadcast_scan_ts"] = self.clock()
            before = datetime.fromtimestamp(min(self.clock(), self.deadline(row) + 60) + 1, timezone.utc)
            for _, entity in self.sources:
                try:
                    messages = await asyncio.wait_for(self.client.get_messages(
                        entity, search="@" + self.username.lstrip("@"), offset_date=before,
                        limit=min(self.scan_limit, 100)), 10)
                except (TimeoutError, OSError, ValueError, errors.RPCError) as exc:
                    row["receipt_lookup_error_type"] = type(exc).__name__
                    row["receipt_lookup_error_ts"] = self.clock()
                    continue
                chat_id = utils.get_peer_id(entity)
                for message in reversed(messages):
                    self.observe(message, chat_id)
            self.persist()
            break

    def observe_command_receipt(self, message, chat_id):
        if (chat_id not in self.source_by_chat or getattr(message, "chat_id", None) != chat_id
                or getattr(message, "sender_id", None) not in self.trusted
                or getattr(message, "fwd_from", None) is not None or not getattr(message, "date", None)):
            return
        text = str(getattr(message, "message", "") or "")
        date = getattr(message, "edit_date", None) or message.date
        ts = date.timestamp()
        parent = reply_parent(message)
        named = message_owned_by(message, self.username, self.user_id)
        if re.search(r"@[A-Za-z0-9_]+", text) and not named:
            return
        matches = []
        for key, row in self.rows.items():
            start = row.get("submitted_at_ts", row.get("sent_at_ts"))
            direct = chat_id == CANONICAL_CHAT_ID and parent in self.reply_ids(row) - {row["message_id"]}
            if (row.get("kind") != "question" or start is None or ts < int(start)
                    or (not direct and ts > self.deadline(row) + 60)):
                continue
            parsed = fixed_event_receipt(text, self.flow, row.get("reply_text", ""))
            if self.flow not in FIXED_EVENT_FLOWS and (
                    not direct or not parsed or parsed["status"] != "cooldown"):
                continue  # Quiz/trade successes keep their own strict parsers.
            if not direct and not (named and parsed and parsed.get("command") == row.get("reply_text")
                                   and (not parent or parent in {CANONICAL_TOPIC_ID, row["message_id"]})):
                continue
            if parsed and parsed.get("command") and parsed["command"] != row.get("reply_text"):
                continue
            if parsed and parsed["status"] == "expired":
                continue
            matches.append((key, row, parsed or {"status": "unclassified"}))
        if len(matches) != 1:
            return
        key, row, parsed = matches[0]
        if self.flow not in FIXED_EVENT_FLOWS and row["status"] in xuangu.TERMINAL_RESULTS | {
                "trade_confirmed", "trade_already_observed", "reply_rejected", "expired_unanswered"}:
            return
        if row["status"] == "command_confirmed" and not (
                row.get("receipt_chat_id") == chat_id and row.get("receipt_message_id") == message.id
                and parsed.get("status") == "success" and "receipt_reply_to_message_id" not in row):
            return
        receipt = {"message_id": message.id, "chat_id": chat_id, "sender_id": message.sender_id,
                   "date_ts": ts, "text": text, "event_key": key, "reply_parent": parent, **parsed}
        receipt_key = self.key(chat_id, message.id)
        if self.command_receipts.get(receipt_key) == receipt and "receipt_reply_to_message_id" in row:
            return  # An old cooldown/unclassified reply cannot restart the wait.
        self.command_receipts[receipt_key] = receipt
        cooldown_status = ("awaiting_answer_result" if self.flow == xuangu.FLOW else
                           "awaiting_trade_result" if self.flow in TRADE_FLOWS else "awaiting_command_result")
        status = {"success": "command_confirmed", "rejected": "command_rejected",
                  "cooldown": cooldown_status, "unclassified": "command_reply_unclassified"}[parsed["status"]]
        row.update(status=status, receipt_message_id=message.id, receipt_chat_id=chat_id,
                   receipt_sender_id=message.sender_id, receipt_text=text, confirmed_at_ts=ts,
                   receipt_reply_to_message_id=parent)
        if parsed["status"] == "cooldown":
            row["server_retry_at_ts"] = max(row.get("server_retry_at_ts", 0), ts + parsed["retry_seconds"] + 1)
            row["retry_at_ts"] = row["server_retry_at_ts"]
        payload = {"ts": ts, "status": status, "flow_slug": self.flow, "event_message_id": row["message_id"],
                   "sent_message_id": (parent if chat_id == CANONICAL_CHAT_ID and parent in
                                       self.reply_ids(row) - {row["message_id"]} else row.get("sent_message_id")),
                   "sent_message_ids": row.get("sent_message_ids", []), "reply_text": row.get("reply_text"),
                   "receipt_message_id": message.id, "receipt_chat_id": chat_id,
                   "receipt_reply_to_message_id": parent,
                   "receipt_sender_id": message.sender_id, "receipt_text": text,
                   "trigger_target_username": self.username, "handled_event_ids_by_chat": self.cursors,
                   "retry_not_before_ts": row.get("server_retry_at_ts")}
        write_atomic(self.state_dir / "last_attempt.json", payload)
        if status == "command_confirmed":
            previous = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
            if previous.get("status") != status or float(previous.get("ts") or 0) <= ts:
                write_atomic(self.state_path, payload)
                self.last_state = payload
        self.persist()

    def observe_trade_receipt(self, message, chat_id):
        if (self.flow not in TRADE_FLOWS or chat_id not in self.source_by_chat
                or getattr(message, "chat_id", None) != chat_id
                or getattr(message, "fwd_from", None) is not None
                or getattr(message, "sender_id", None) not in self.trusted):
            return
        parsed = parse_trade_result(str(getattr(message, "message", "") or ""))
        date = getattr(message, "date", None)
        if (not parsed or parsed["username"].casefold() != self.username.lstrip("@").casefold()
                or not date or not self.receipt_age_allowed(message, chat_id, date.timestamp())):
            return
        key = self.key(chat_id, message.id)
        if key not in self.trade_receipts:
            # A result observed before any associated submission is evidence
            # of another action, not permission to claim a later send worked.
            candidates = [event_key for event_key, row in self.rows.items()
                          if row.get("submitted_at_ts") is not None or row.get("sent_at_ts") is not None]
            self.trade_receipts[key] = {**parsed, "chat_id": chat_id, "message_id": message.id,
                                        "sender_id": message.sender_id, "date_ts": date.timestamp(),
                                        "reply_parent": reply_parent(message), "candidate_event_keys": candidates,
                                        "status": "unlinked"}
        self.reconcile_trade_receipts()
        self.persist()

    def preexisting_trade_receipt(self, row):
        companion = trade_companion(row.get("text", ""))
        if not companion:
            return None
        for receipt in self.trade_receipts.values():
            if (receipt["companion"] != companion or not receipt["command"]
                    or receipt.get("event_key")
                    or not row["date_ts"] <= receipt["date_ts"] <= self.deadline(row) + 60):
                continue
            candidates = [candidate for candidate in self.rows.values()
                          if candidate.get("kind") == "question" and candidate["chat_id"] == CANONICAL_CHAT_ID
                          and trade_companion(candidate.get("text", "")) == companion
                          and candidate["date_ts"] <= receipt["date_ts"] <= self.deadline(candidate) + 60]
            parent = receipt.get("reply_parent")
            if parent and not (receipt["chat_id"] == CANONICAL_CHAT_ID and parent == CANONICAL_TOPIC_ID):
                candidates = [candidate for candidate in candidates
                              if receipt["chat_id"] == candidate["chat_id"] and parent in self.reply_ids(candidate)]
            if len(candidates) == 1 and candidates[0] is row:
                return receipt
        return None

    def write_trade_success(self, row):
        if row["status"] != "trade_confirmed":
            return
        source_target, source_entity = self.source_by_chat[row["chat_id"]]
        chat_key = str(getattr(source_entity, "id", row["chat_id"]))
        self.cursors[chat_key] = max(int(self.cursors.get(chat_key) or 0), row["message_id"])
        state = {"ts": row["confirmed_at_ts"], "status": "trade_confirmed", "flow_slug": self.flow,
                 "event_message_id": row["message_id"], "reply_target_message_id": row["message_id"],
                 "sent_message_id": row.get("sent_message_id"), "reply_text": row["reply_text"],
                 "target": CANONICAL_TARGET, "topic_id": CANONICAL_TOPIC_ID,
                 "source_chat": str(source_target), "trigger_target_username": self.username,
                 "event_text": row["text"], "handled_event_ids_by_chat": dict(self.cursors),
                 "receipt_message_id": row["receipt_message_id"], "receipt_chat_id": row["receipt_chat_id"],
                 "trade_companion": row["trade_companion"], "trade_reward": row["trade_reward"]}
        # Preserve the newest confirmed transaction when replaying old receipts.
        if self.last_state.get("status") == "trade_confirmed" and self.last_state.get("ts", 0) > state["ts"]:
            return
        if state != self.last_state or not self.state_path.exists():
            write_atomic(self.state_path, state)
            self.last_state = state

    def reconcile_trade_receipts(self):
        if self.flow not in TRADE_FLOWS:
            return
        for receipt in sorted(self.trade_receipts.values(), key=lambda item: item["date_ts"]):
            if receipt.get("event_key"):
                row = self.rows.get(receipt["event_key"])
                if row:
                    self.write_trade_success(row)
                continue
            candidates = []
            for key in receipt["candidate_event_keys"]:
                row = self.rows.get(key)
                if (not row or row["chat_id"] != CANONICAL_CHAT_ID or row.get("kind") != "question"
                        or row["status"] not in UNCERTAIN | TRADE_PENDING | RETRYABLE | {"reply_rejected"}
                        or not receipt["command"] or row.get("reply_text") != receipt["command"]
                        or trade_companion(row.get("text", "")) != receipt["companion"]):
                    continue
                action_ts = row.get("submitted_at_ts", row.get("sent_at_ts"))
                direct = receipt["chat_id"] == row["chat_id"] and receipt.get("reply_parent") in self.reply_ids(row)
                if (action_ts is not None and int(action_ts) <= receipt["date_ts"]
                        and (direct or receipt["date_ts"] <= self.deadline(row) + 60)):
                    candidates.append((key, row))
            parent = receipt.get("reply_parent")
            if parent and not (receipt["chat_id"] == CANONICAL_CHAT_ID and parent == CANONICAL_TOPIC_ID):
                candidates = [(key, row) for key, row in candidates
                              if receipt["chat_id"] == row["chat_id"] and parent in self.reply_ids(row)]
            if len(candidates) != 1:
                receipt["status"] = "ambiguous" if len(candidates) > 1 else "unlinked"
                continue
            key, row = candidates[0]
            row.update(status="trade_confirmed", confirmed_at_ts=receipt["date_ts"],
                       receipt_chat_id=receipt["chat_id"], receipt_message_id=receipt["message_id"],
                       trade_companion=receipt["companion"], trade_reward=receipt["reward"])
            row.pop("error_type", None)
            receipt.update(status="confirmed", event_key=key)
            self.persist()
            self.write_trade_success(row)

    def observe_answer_receipt(self, message, chat_id):
        if (self.flow != xuangu.FLOW or chat_id not in self.source_by_chat
                or getattr(message, "chat_id", None) != chat_id
                or getattr(message, "sender_id", None) not in self.trusted
                or getattr(message, "fwd_from", None) is not None):
            return
        parsed = xuangu.parse_result(str(getattr(message, "message", "") or ""))
        date = getattr(message, "date", None)
        if (not parsed or parsed["username"].casefold() != self.username.lstrip("@").casefold()
                or not date or not self.receipt_age_allowed(message, chat_id, date.timestamp())):
            return
        key = self.key(chat_id, message.id)
        if key not in self.answer_receipts:
            self.answer_receipts[key] = {
                **parsed, "chat_id": chat_id, "message_id": message.id,
                "sender_id": message.sender_id, "date_ts": date.timestamp(),
                "reply_parent": reply_parent(message), "status": "unlinked",
                "candidate_event_keys": list(self.rows),
            }
        self.reconcile_answer_receipts()
        self.persist()

    def answer_receipt_candidates(self, receipt, *, include_unseen=False):
        candidates = []
        keys = self.rows if include_unseen else receipt["candidate_event_keys"]
        for key in keys:
            row = self.rows.get(key)
            if (not row or row.get("kind") != "question" or row["chat_id"] != CANONICAL_CHAT_ID
                    or row["status"] not in UNCERTAIN | RETRYABLE | xuangu.PENDING | xuangu.TERMINAL_RESULTS | {"reply_rejected", "expired_unanswered"}):
                continue
            question = xuangu.parse_question(row.get("text", ""))
            direct = receipt["chat_id"] == row["chat_id"] and receipt.get("reply_parent") in self.reply_ids(row)
            if (not question or question["variant"] != receipt["variant"]
                    or receipt["date_ts"] < row["date_ts"]
                    or (not direct and receipt["date_ts"] > self.deadline(row) + xuangu.RESULT_GRACE_SECONDS)
                    or (receipt["option"] and receipt["option"] not in question["options"])):
                continue
            action_ts = row.get("submitted_at_ts", row.get("sent_at_ts"))
            if action_ts is not None:
                chosen = xuangu.command(row.get("reply_text", ""))
                if (int(action_ts) > receipt["date_ts"] or not chosen
                        or (receipt["option"] and receipt["option"] != chosen[-1])):
                    continue
            candidates.append((key, row))
        parent = receipt.get("reply_parent")
        # A specific reply is stronger than a broadcast: it must identify this
        # exact question or answer. Never fall back to temporal proximity when
        # its explicit reply chain points elsewhere.
        if parent and not (receipt["chat_id"] == CANONICAL_CHAT_ID and parent == CANONICAL_TOPIC_ID):
            candidates = [(key, row) for key, row in candidates
                          if receipt["chat_id"] == row["chat_id"]
                          and parent in self.reply_ids(row)]
        return candidates

    def reconcile_preexisting_answer(self, row):
        for receipt in self.answer_receipts.values():
            if receipt.get("event_key"):
                continue
            candidates = self.answer_receipt_candidates(receipt, include_unseen=True)
            if len(candidates) == 1 and candidates[0][1] is row:
                key = candidates[0][0]
                if key not in receipt["candidate_event_keys"]:
                    receipt["candidate_event_keys"].append(key)
                self.reconcile_answer_receipts()
                return row["status"] in xuangu.TERMINAL_RESULTS
        return False

    def reconcile_answer_receipts(self):
        if self.flow != xuangu.FLOW:
            return
        for receipt in sorted(self.answer_receipts.values(), key=lambda item: item["date_ts"]):
            if receipt.get("event_key"):
                row = self.rows.get(receipt["event_key"])
                if row:
                    self.write_answer_success(row)
                continue
            candidates = self.answer_receipt_candidates(receipt)
            if len(candidates) != 1:
                receipt["status"] = "ambiguous" if len(candidates) > 1 else "unlinked"
                continue
            key, row = candidates[0]
            if row["status"] in xuangu.TERMINAL_RESULTS:
                receipt.update(status="duplicate", event_key=key)
                continue
            submitted = row.get("submitted_at_ts") is not None or row.get("sent_at_ts") is not None
            status = ("answer_confirmed" if submitted else "answer_already_observed") if receipt["outcome"] == "correct" else (
                "answer_incorrect" if receipt["outcome"] == "incorrect" else "answer_timed_out")
            row.update(status=status, answer_outcome=receipt["outcome"], confirmed_at_ts=receipt["date_ts"],
                       receipt_chat_id=receipt["chat_id"], receipt_message_id=receipt["message_id"],
                       result_option=receipt["option"])
            row.pop("error_type", None)
            receipt.update(status="linked", event_key=key)
            self.persist()
            write_atomic(self.state_dir / "last_attempt.json", {
                "ts": receipt["date_ts"], "status": status, "flow_slug": self.flow,
                "event_message_id": row["message_id"], "reply_text": row.get("reply_text"),
                "receipt_chat_id": receipt["chat_id"], "receipt_message_id": receipt["message_id"],
                "answer_outcome": receipt["outcome"],
            })
            self.write_answer_success(row)
            if receipt["outcome"] == "correct":
                self.record_learned_question(row["text"], receipt["option"], {
                    "chat_id": row["chat_id"], "question_message_id": row["message_id"],
                    "result_chat_id": receipt["chat_id"], "result_message_id": receipt["message_id"],
                    "result_sender_id": receipt["sender_id"],
                })

    def write_answer_success(self, row):
        if row["status"] != "answer_confirmed":
            return
        source_target, source_entity = self.source_by_chat[row["chat_id"]]
        chat_key = str(source_entity.id)
        self.cursors[chat_key] = max(int(self.cursors.get(chat_key) or 0), row["message_id"])
        state = {
            "ts": row["confirmed_at_ts"], "status": "answer_confirmed", "flow_slug": self.flow,
            "event_message_id": row["message_id"], "reply_target_message_id": row["message_id"],
            "sent_message_id": row.get("sent_message_id"), "reply_text": row["reply_text"],
            "target": CANONICAL_TARGET, "topic_id": CANONICAL_TOPIC_ID,
            "source_chat": str(source_target), "trigger_target_username": self.username,
            "event_text": row["text"], "handled_event_ids_by_chat": dict(self.cursors),
            "receipt_message_id": row["receipt_message_id"], "receipt_chat_id": row["receipt_chat_id"],
            "automated_submission": row.get("submitted_at_ts") is not None,
        }
        previous = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        if previous.get("status") == "answer_confirmed" and previous.get("ts", 0) > state["ts"]:
            return
        if previous != state:
            write_atomic(self.state_path, state)
        self.last_state = state

    def record_learned_question(self, text, option, evidence):
        if not self.auto_update_answer_bank:
            return
        question = xuangu.parse_question(text)
        if not question or option not in question["options"]:
            return
        answer = question["options"][option]
        existing = json.loads(self.learned_path.read_text()) if self.learned_path.exists() else {}
        if existing and (existing.get("account") != self.account or existing.get("flow") != self.flow):
            raise ValueError("question answer bank identity mismatch")
        questions = list(existing.get("questions", []))
        entry = next((entry for entry in questions if entry.get("variant") == question["variant"]
                      and xuangu.question_key(entry["variant"], entry["question"]) == question["key"]), None)
        if entry is None:
            entry = {"variant": question["variant"], "question": question["question"], "answers": []}
            questions.append(entry)
        if answer not in entry["answers"]:
            entry["answers"].append(answer)
        source = {**evidence, "answer": answer}
        entry["source"] = source
        write_atomic(self.learned_path, {"account": self.account, "flow": self.flow, "ts": self.clock(),
                                        "questions": questions, "last_source": source})
        self.learned_questions[question["key"]] = list(entry["answers"])

    async def reconcile_unknown(self):
        for row in list(self.rows.values()):
            if row["status"] not in UNCERTAIN | {"reply_retry_pending"}:
                continue
            # One row's failed lookup or write is recorded on that row; it
            # must not abort the rest of the pass or mark the sources failed.
            try:
                # A topic-root send from the secondary group is only attributable
                # when Telegram returned its exact ID. Time proximity is insufficient.
                if row.get("sent_message_id"):
                    message = await asyncio.wait_for(self.client.get_messages(self.outbound, ids=row["sent_message_id"]), 5)
                elif self.flow == "shenji_budao" or (row["chat_id"] == CANONICAL_CHAT_ID and row.get("send_reply_to") == row["message_id"]):
                    message = self.outbound_replies.get(row["message_id"])
                else:
                    continue
            except Exception as exc:
                row.update(receipt_lookup_error_type=type(exc).__name__, receipt_lookup_error_ts=self.clock())
                continue
            if self.matching_outbound(row, message):
                try:
                    self.record_sent(row, message, recovered=True)
                except Exception as exc:
                    if row["status"] != "trade_confirmed" and row["status"] not in xuangu.TERMINAL_RESULTS | COMMAND_TERMINAL:
                        row.update(status="send_outcome_unknown", error_type=type(exc).__name__)
                    self.persist()
                    log_print(f"Question recovery write failed: {self.account}/{self.flow} "
                              f"question={row['message_id']} error={type(exc).__name__}")

    def reload_answer_bank(self):
        learned = {}
        for path in (self.answer_bank_path, self.learned_path):
            if path:
                self.answer_bank = xuangu.merge_answer_bank(self.answer_bank, xuangu.load_answer_bank_file(path))
                if self.flow == xuangu.FLOW and path.exists():
                    payload = json.loads(path.read_text())
                    if path == self.learned_path and (
                            payload.get("account") != self.account or payload.get("flow") != self.flow):
                        raise ValueError("question answer bank identity mismatch")
                    for entry in payload.get("questions", []):
                        # Old untyped entries remain on disk, but cannot
                        # authorize an answer across different event variants.
                        if entry.get("variant") not in xuangu.VARIANTS:
                            continue
                        key = xuangu.question_key(entry["variant"], entry["question"])
                        learned[key] = xuangu.merge_answer_bank(learned.get(key, []), entry["answers"])
        self.learned_questions = learned

    async def learn_result(self, message, chat_id):
        if (not self.auto_update_answer_bank or chat_id not in self.source_by_chat
                or getattr(message, "chat_id", None) != chat_id
                or getattr(message, "fwd_from", None) is not None
                or getattr(message, "sender_id", None) not in self.trusted):
            return
        key = self.key(chat_id, message.id)
        text = str(getattr(message, "message", "") or "")
        match = self.result_pattern.search(text)
        date = getattr(message, "date", None)
        if (key in self.learned_results or not match or not date
                or not -30 <= self.clock() - date.timestamp() <= self.learned_max_age
                or not any(all(marker in text for marker in group) for group in self.result_marker_groups)):
            return
        parent = reply_parent(message)
        if not parent or (chat_id == CANONICAL_CHAT_ID and parent == CANONICAL_TOPIC_ID):
            return
        question = await self.cached_message(chat_id, parent)
        if question and re.fullmatch(r"\s*\.作答\s+" + match.group(1) + r"\s*", question.message or ""):
            self.observe_outbound(question, chat_id)
            parent = reply_parent(question)
            question = await self.cached_message(chat_id, parent) if parent else None
        if (not question or getattr(question, "sender_id", None) not in self.trusted
                or getattr(question, "chat_id", None) != chat_id
                or getattr(question, "fwd_from", None) is not None
                or question_kind(question.message or "", self.flow) != "question"
                or not 0 <= date.timestamp() - question.date.timestamp() <= question_max_age(question.message or "", self.flow, self.max_age) + 30):
            return
        parsed_result = xuangu.parse_result(text)
        parsed_question = xuangu.parse_question(question.message or "")
        if (not parsed_result or not parsed_question or parsed_result["outcome"] != "correct"
                or parsed_result["variant"] != parsed_question["variant"] or not message_owned_by(
                    question, parsed_result["username"], self.user_id if parsed_result["username"].casefold() == self.username.casefold() else 0)):
            return
        answers = [option.strip() for code, option in xuangu.OPTION_RE.findall(question.message or "") if code == match.group(1)]
        if len(answers) != 1:
            return
        evidence = {"chat_id": chat_id, "question_message_id": question.id, "result_message_id": message.id,
                    "result_sender_id": message.sender_id, "answer": answers[0]}
        self.record_learned_question(question.message, match.group(1), evidence)
        self.learned_results.add(key)
        if len(self.learned_results) > 1000:
            self.learned_results = {key}

    async def cached_message(self, chat_id, message_id):
        # An unlearnable result is seen again by every overlapping or full
        # history read; fetch each parent once. ponytail: FIFO of 500 entries.
        key = (chat_id, message_id)
        if key not in self.parent_cache:
            message = await asyncio.wait_for(self.client.get_messages(
                self.source_by_chat[chat_id][1], ids=message_id), 5)
            if len(self.parent_cache) >= 500:
                self.parent_cache.pop(next(iter(self.parent_cache)))
            self.parent_cache[key] = message
        return self.parent_cache[key]

    async def dispatch(self, key):
        row = self.rows.get(key)
        if row is None or row["status"] not in RETRYABLE or is_paused(self.state_dir, self.flow)[0]:
            return
        if self.clock() < self.retry_not_before(row):
            return
        if self.clock() >= self.deadline(row):
            row["status"] = "expired_unanswered"
            self.persist()
            return
        send_started = False
        try:
            with flow_lock(self.state_dir / "send.lock") as lock:
                lock.seek(0)
                lock.truncate()
                json.dump({"pid": os.getpid(), "flow_slug": self.flow,
                           "started_at_ts": self.clock(), "run_max_seconds": 120}, lock)
                lock.flush()
                # History reads, MiniApp calls and the answer decision happen
                # before claiming a global send slot; the slot itself only
                # covers a local re-check and the Telegram send.
                if not await self.prepare_send(row):
                    return

                async def action():
                    nonlocal send_started
                    if (row["status"] not in RETRYABLE or is_paused(self.state_dir, self.flow)[0]
                            or self.clock() < self.retry_not_before(row)):
                        return None
                    remaining = self.deadline(row) - self.clock()
                    if remaining <= 1:
                        row["status"] = "expired_unanswered"
                        return None
                    row.setdefault("submitted_at_ts", self.clock())
                    row.update(status="send_submitted", last_submitted_at_ts=self.clock(),
                               retry_at_ts=self.clock() + self.reply_wait,
                               attempt_count=int(row.get("attempt_count", 0)) + 1)
                    self.persist()
                    # Delivery is only an attempt. Retry after the confirmation
                    # window, including when the transport result is unknown.
                    send_started = True
                    return await asyncio.wait_for(
                        self.client.send_message(self.outbound, row["reply_text"], reply_to=row["send_reply_to"]),
                        min(10, remaining))

                sent = await send_with_global_gate(action, label=f"{self.flow}:question reply", state_dir=self.state_dir)
                if sent is not None:
                    self.record_sent(row, sent)
        except BaseException as exc:
            if isinstance(exc, AccountPausedError) and row["status"] not in xuangu.TERMINAL_RESULTS | COMMAND_TERMINAL | {"trade_confirmed"}:
                # A definite local refusal: nothing reached Telegram, so this is
                # not an unknown send outcome. Back off until the pause clears.
                row.update(status="retryable_error", error_type=type(exc).__name__,
                           retry_at_ts=max(self.retry_not_before(row), self.clock() + PAUSED_RETRY_SECONDS))
            elif row["status"] != "trade_confirmed" and row["status"] not in xuangu.TERMINAL_RESULTS | COMMAND_TERMINAL:
                row["status"] = "send_outcome_unknown" if send_started or row.get("sent_message_id") else "retryable_error"
                row["error_type"] = type(exc).__name__
                row["retry_at_ts"] = max(self.retry_not_before(row), self.clock() +
                    max(self.reply_wait if send_started else 2, float(getattr(exc, "seconds", 0) or 0) + 1))
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
        finally:
            self.persist()

    async def prepare_send(self, row):
        """Read and decide outside the send gate; True leaves reply_text/send_reply_to set."""
        if row["status"] not in RETRYABLE or is_paused(self.state_dir, self.flow)[0]:
            return False
        if self.clock() < self.retry_not_before(row):
            return False
        source_entity = self.source_by_chat[row["chat_id"]][1]
        if row.get("submitted_at_ts") or row.get("sent_at_ts"):
            # History and push check all copies; rotate exact old
            # threads so a growing journal cannot block retries.
            await self.reconcile_command_copies(row)
            if row["status"] not in RETRYABLE:
                return False
            recent = await asyncio.wait_for(self.client.get_messages(self.outbound, limit=self.scan_limit), 10)
            for item in reversed(recent):
                self.observe(item, CANONICAL_CHAT_ID)
            if row["status"] not in RETRYABLE:
                return False
        if self.flow in TRADE_FLOWS or self.flow == xuangu.FLOW or self.flow in FIXED_EVENT_FLOWS:
            own = self.outbound_replies.get(row["message_id"])
            chosen = self.outbound_text(own)
            if chosen in self.fixed_choice_commands():
                row.update(reply_text=chosen, send_reply_to=row["message_id"])
                if self.flow == "shenji_budao":
                    row["send_reply_to"] = CANONICAL_TOPIC_ID
                if self.matching_outbound(row, own):
                    if self.existing_copy_waits(row, own):
                        return False
            if self.flow == xuangu.FLOW and self.reconcile_preexisting_answer(row):
                return False
            receipt = self.preexisting_trade_receipt(row) if self.flow in TRADE_FLOWS else None
            if receipt:
                row.update(status="trade_already_observed", receipt_chat_id=receipt["chat_id"],
                           receipt_message_id=receipt["message_id"], trade_reward=receipt["reward"])
                # An external/manual result prevents repeating the
                # exchange, but is not success evidence for our send.
                write_atomic(self.state_dir / "last_attempt.json", {
                    "ts": self.clock(), "status": row["status"], "flow_slug": self.flow,
                    "event_message_id": row["message_id"], "receipt_chat_id": receipt["chat_id"],
                    "receipt_message_id": receipt["message_id"]})
                return False
        remaining = self.deadline(row) - self.clock()
        if remaining <= 1:
            row["status"] = "expired_unanswered"
            return False
        message = await asyncio.wait_for(self.client.get_messages(source_entity, ids=row["message_id"]), min(5, remaining))
        if row["status"] not in RETRYABLE:
            return False
        resume_root_command = (self.flow == "shenji_budao" and row.get("submitted_at_ts")
                               and row.get("reply_text") == ".神迹 布道")
        eligible = self.eligible(message, row["chat_id"]) if message else False
        if not resume_root_command and (not message or not eligible or question_kind(message.message or "", self.flow) != "question"):
            row["status"] = "expired_unanswered"
            return False
        if not resume_root_command and self.fixed_choice_readonly_source(message, row["chat_id"]):
            row["status"] = "readonly_source"
            return False
        text = row["text"] if resume_root_command else message.message or ""
        # Edits may shorten a window, but never restart its clock
        # or extend a deadline already recorded before a restart.
        row["deadline_ts"] = min(self.deadline(row), row["date_ts"] +
                                 question_max_age(text, self.flow, self.max_age))
        reply_text = self.answer(text)
        if (self.flow in TRADE_FLOWS | FIXED_EVENT_FLOWS and row.get("sent_at_ts")
                and row.get("reply_text") in self.fixed_choice_commands()):
            reply_text = row["reply_text"]
        if not reply_text:
            row["status"] = "answer_unavailable"
            return False
        if self.dry_run:
            row["status"] = "dry_run"
            log_print(f"DRY RUN question reply: {self.account}/{self.flow} question={row['message_id']}")
            return False
        if self.deadline(row) - self.clock() <= 1:
            row["status"] = "expired_unanswered"
            return False
        topic = CANONICAL_TOPIC_ID if resume_root_command else message_topic(message)
        send_reply_to = (row["message_id"] if row["chat_id"] == CANONICAL_CHAT_ID and topic == CANONICAL_TOPIC_ID
                         else CANONICAL_TOPIC_ID)
        if self.flow == "shenji_budao":
            send_reply_to = CANONICAL_TOPIC_ID
        row.update(reply_text=reply_text, send_reply_to=send_reply_to, text=text)
        own_reply = self.outbound_replies.get(row["message_id"]) if row["chat_id"] == CANONICAL_CHAT_ID else None
        if self.fixed_choice_commands() and own_reply:
            chosen = self.outbound_text(own_reply)
            if chosen in self.fixed_choice_commands():
                row["reply_text"] = chosen
                if self.matching_outbound(row, own_reply):
                    if self.existing_copy_waits(row, own_reply):
                        return False
                row["reply_text"] = reply_text
        if self.matching_outbound(row, own_reply):
            if self.existing_copy_waits(row, own_reply):
                return False
        if row["status"] not in RETRYABLE or self.clock() < self.retry_not_before(row):
            return False
        from miniapp_commands.catalog import migrated
        project_root = self.command_root
        if self.flow == "shenji_budao" and migrated(project_root, self.account, self.flow):
            # A MiniApp request is not a Telegram send; it runs under the flow
            # lock but never inside the global Telegram send slot.
            from miniapp_commands.runner import run_flow
            row.setdefault("miniapp_queued_at_ts", self.clock())
            self.persist()
            result = await run_flow(
                project_root, self.account, self.flow, clock=self.clock, lock_held=True,
                event={"key": self.key(row["chat_id"], row["message_id"]),
                       "deadline": self.deadline(row)},
            )
            row.update(transport="miniapp", miniapp_receipt=result.get("last_response"),
                       miniapp_status=result["status"])
            if result["status"] in {"success", "event_already_complete"}:
                row.update(status="command_confirmed", confirmed_at_ts=self.clock())
                write_atomic(self.state_path, {"ts": self.clock(), "status": row["status"],
                    "flow_slug": self.flow, "account": self.account, "transport": "miniapp",
                    "event_message_id": row["message_id"], "miniapp_receipt": result.get("last_response")})
            elif result["status"] == "unknown_outcome":
                row.update(status="command_result_unconfirmed", error_type="miniapp_unknown_outcome")
            elif result["status"] in {"rejected", "unavailable"}:
                row["status"] = "command_rejected"
            else:
                row.update(status="reply_retry_pending", retry_at_ts=result.get("next_due_ts") or self.clock()+120)
            self.persist()
            return False
        return True

    def existing_copy_waits(self, row, message):
        self.record_sent(row, message, recovered=True)
        if (row["status"] in xuangu.TERMINAL_RESULTS | COMMAND_TERMINAL | {"trade_confirmed"}
                or self.clock() < self.retry_not_before(row)):
            return True
        row["status"] = "reply_retry_pending"
        return False

    def tick(self):
        changed = False
        for key, row in list(self.rows.items()):
            if row["status"] in WAITING_RESULT and self.clock() < self.deadline(row):
                if self.clock() >= self.retry_not_before(row, row["date_ts"] + self.reply_wait):
                    row["status"] = "reply_retry_pending"
                    changed = True
            if (row["status"] in WAITING_RESULT | {"reply_retry_pending"}
                    and self.clock() >= self.deadline(row) + (xuangu.RESULT_GRACE_SECONDS if self.flow == xuangu.FLOW else 60)):
                final = ("answer_result_unconfirmed" if self.flow == xuangu.FLOW else
                         "trade_result_unconfirmed" if self.flow in TRADE_FLOWS else "command_result_unconfirmed")
                # Rows already in their final unconfirmed state stay as sticky
                # alerts; re-assigning them on every 0.5s tick rewrote the state
                # and health files continuously.
                if row["status"] != final:
                    row["status"] = final
                    changed = True
            if row["status"] == "awaiting_answer_result" and self.clock() >= self.deadline(row) + xuangu.RESULT_GRACE_SECONDS:
                row["status"] = "answer_result_unconfirmed"
                changed = True
            elif row["status"] == "awaiting_trade_result" and self.clock() >= self.deadline(row) + 60:
                row["status"] = "trade_result_unconfirmed"
                changed = True
            elif row["status"] == "waiting_question" and self.clock() - row["date_ts"] >= 30:
                row["status"] = "question_missing"
                changed = True
            elif row["status"] in RETRYABLE:
                if self.clock() >= self.deadline(row):
                    if row.get("submitted_at_ts") or row.get("sent_at_ts"):
                        continue  # Keep observing a final receipt during its grace window.
                    row["status"] = "expired_unanswered"
                    changed = True
                elif (self.clock() >= self.retry_not_before(row) and key not in self.queued
                      and (row["status"] != "answer_unavailable" or self.answer(row["text"]) is not None)):
                    self.queued.add(key)
                    self.queue.put_nowait(key)
        if changed:
            self.persist()

    async def consume(self):
        while True:
            self.tick()
            try:
                key = await asyncio.wait_for(self.queue.get(), 0.5)
            except TimeoutError:
                continue
            try:
                await self.dispatch(key)
            finally:
                self.queued.discard(key)
                self.queue.task_done()

    async def scan(self, *, on_scan_complete):
        self.reload_answer_bank()
        history = self.health.history
        history.begin()
        async def read(source):
            target, entity = source
            chat_id = utils.get_peer_id(entity)
            # Pushes deliver new messages and edits; routine reconciliation only
            # reads past the last seen ID (a few IDs of overlap), the full
            # TG_SCAN_LIMIT window only when HistoryWindow says it is due.
            min_id = history.min_id(chat_id)
            messages = await with_telegram_retry(lambda: self.client.get_messages(
                entity, limit=self.scan_limit, min_id=min_id,
                wait_time=1 if self.scan_limit > 100 else 0),
                                                 label=f"question reconciliation {target}", reconnect=lambda: ensure_connected(self.client))
            for message in messages:
                self.observe_outbound(message, chat_id)
            for message in reversed(messages):
                self.observe(message, chat_id)
            history.record(chat_id, messages)
            return chat_id, messages
        # Both sources share one account's history quota; do not issue two
        # simultaneous page bursts. Push handling and the sender stay live.
        recent = [await read(source) for source in self.sources]
        await self.reconcile_unknown()
        # Expired prompts are never replayed, but an old exact command reply
        # can still resolve their journal after restart. Bound this slow path
        # to two command IDs per scan, even when one event has many retries.
        checked = 0
        for row in sorted(self.rows.values(), key=lambda item: item.get("last_reply_scan_ts", 0)):
            interval = 60 if self.clock() < self.deadline(row) else 300
            if (row["status"] not in WAITING_RESULT | RETRYABLE
                    or self.clock() - row.get("last_reply_scan_ts", 0) < interval):
                continue
            checked += await self.reconcile_command_copies(row, limit=2 - checked)
            if checked >= 2:
                break
        if checked:
            self.persist()
        await self.reconcile_event_broadcasts()
        self.reconcile_trade_receipts()
        self.reconcile_answer_receipts()
        for chat_id, messages in recent:
            for message in messages:
                try:
                    await self.learn_result(message, chat_id)
                except Exception as exc:
                    # Learning is best effort; one bad parent lookup must not
                    # discard this completed source scan.
                    log_print(f"Answer learning skipped: {self.account}/{self.flow} "
                              f"message={getattr(message, 'id', None)} error={type(exc).__name__}")
        history.complete()
        on_scan_complete()

    async def run(self):
        """Serve pushes and reconciliation; the caller holds question_monitor.lock."""
        self.load_state()
        if self.last_state.get("status") in {"sent", "already_replied"}:
            self.last_state.update(legacy_status=self.last_state["status"], status="legacy_unconfirmed",
                                   confirmation_valid=False)
            write_atomic(self.state_path, self.last_state)
        self.persist()
        async def on_message(event):
            message = getattr(event, "message", event)
            try:
                self.observe(message, getattr(message, "chat_id", None))
            except Exception as exc:
                self.health.failed("question_push", exc)
                raise
        builders = [events.NewMessage(chats=[entity for _, entity in self.sources]),
                    events.MessageEdited(chats=[entity for _, entity in self.sources])]
        for builder in builders:
            self.client.add_event_handler(on_message, builder)
        log_print(f"Question push handler ready: {self.account}/{self.flow} version={question_monitor_version(self.flow)}")
        async def reconcile_loop():
            while True:
                try:
                    await self.health.run_cycle(self.client, self.scan)
                except Exception:
                    pass  # The primary health writer retains the exact failure type.
                await asyncio.sleep(self.reconcile_seconds)
        tasks = [asyncio.create_task(self.consume(), name=f"question_replies_{self.account}_{self.flow}"),
                 asyncio.create_task(reconcile_loop(), name=f"question_history_{self.account}_{self.flow}")]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for builder in builders:
                self.client.remove_event_handler(on_message, builder)
