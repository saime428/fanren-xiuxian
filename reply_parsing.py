#!/usr/bin/env python3
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Sequence

from runtime import log_print
from command_confirmation import OFFICIAL_REPLY_SENDER_IDS, official_message

DEFAULT_SUCCESS_MARKER_TEXT = "闭关成功|閉關成功|成功炼化灵气|成功煉化靈氣|修为最终增加|修為最終增加"
DUEL_OUTCOME_MARKERS = (
    "面对境界压制",
    "凭借神通侥幸逃脱",
    "成功率:",
    "成功率：",
)
DUEL_ANTI_REPEAT_MARKERS = (
    "24小时内已交锋过多",
    "24 小时内已交锋过多",
    "暂不可再次斗法",
)

STATUS_MESSAGE = ".查看闭关"
ACTION_MESSAGE = ".深度闭关"
STATUS_COUNTDOWN_MARKERS = ("预计还需", "預計還需", "还需", "還需")
STATUS_SUMMARY_MARKERS = ("【深度闭关总结】", "【深度閉關總結】", "深度闭关总结", "深度閉關總結")
STATUS_NOT_IN_RETREAT_MARKERS = ("你并未处于深度闭关之中", "你並未處於深度閉關之中")
STATUS_ALREADY_IN_RETREAT_MARKERS = ("你已在深度闭关之中", "你已在深度閉關之中")
ACTION_STARTED_MARKERS = ("你已进入深度闭关状态", "你已進入深度閉關狀態")
ACTION_ALREADY_IN_RETREAT_MARKERS = ("你已在深度闭关之中", "你已在深度閉關之中")
DEFAULT_DURATION_SECONDS = 8 * 60 * 60


def parse_markers(marker_text: str) -> list[str]:
    return [part.strip() for part in marker_text.split("|") if part.strip()]


# Historic aliases kept for existing imports.
parse_reset_triggers = parse_markers
parse_cooldown_markers = parse_markers
parse_success_markers = parse_markers


def slugify_message(message: str) -> str:
    raw = (message or "").strip().lower()
    if not raw:
        return "send"
    raw = raw.lstrip("./")
    slug = re.sub(r"[^a-z0-9]+", "_", raw)
    slug = slug.strip("_")
    return slug or "send"


def get_message_top_id(message: object) -> int | None:
    reply = getattr(message, "reply_to", None)
    return (
        getattr(reply, "reply_to_top_id", None)
        or getattr(message, "reply_to_msg_id", None)
        or getattr(reply, "reply_to_msg_id", None)
    )


def normalize_topic_id(value: int | None) -> int | None:
    return None if value in (None, 0) else int(value)


_DURATION_UNIT_PATTERNS: tuple[tuple[str, int], ...] = (
    (r"(\d+)\s*(?:天|日|d|D)", 86400),
    (r"(\d+)\s*(?:小时|小時|h|H)", 3600),
    (r"(\d+)\s*(?:分钟|分鐘|分|m|M)", 60),
    (r"(\d+)\s*(?:秒钟|秒鐘|秒|s|S)", 1),
)
_COMPACT_CLOCK_RE = re.compile(r"(\d+)[:：](\d{1,2})(?:[:：](\d{1,2}))?")
# Span between 请在 and the first following 后, e.g. "请在 3分钟 后再来".
_COOLDOWN_ANCHOR_RE = re.compile(r"(?:请在|請在)([^后後]{1,40}?)(?:后|後)")
_REMAINING_TIME_ANCHOR_RE = re.compile(
    r"(?:剩[余餘]|[还還](?:剩|需)|尚需|仍需|(?:需要?(?:再)?|请|請)等待)"
    r"\s*(?:冷[却卻])?\s*(?:时间|時間)?\s*(?:[:：]|为|為)?\s*"
    r"((?:\d+\s*(?:天|日|小时|小時|分钟|分鐘|分|秒钟|秒鐘|秒|[dhmsDHMS])\s*)+"
    r"|\d+[:：]\d{1,2}(?:[:：]\d{1,2})?)")
_DAILY_MIND_RE = re.compile(r"今日神念\s*[:：]\s*(\d+)\s*/\s*(\d+)")


def _parse_duration_text(text: str) -> int | None:
    total_seconds = 0
    matched = False
    for pattern, multiplier in _DURATION_UNIT_PATTERNS:
        m = re.search(pattern, text)
        if not m:
            continue
        total_seconds += int(m.group(1)) * multiplier
        matched = True
    if matched:
        return total_seconds

    compact = _COMPACT_CLOCK_RE.search(text)
    if compact:
        parts = [int(part) for part in compact.groups() if part is not None]
        if len(parts) == 2:
            minutes, seconds = parts
            return minutes * 60 + seconds
        hours, minutes, seconds = parts
        return hours * 3600 + minutes * 60 + seconds
    return None


def parse_cooldown_retry_delay_seconds(bot_text: str, cooldown_markers: Sequence[str]) -> int | None:
    text = (bot_text or "").strip()
    if not text:
        return None
    normalized = text.replace("個", "")
    # An explicit remaining-time instruction is authoritative even when an
    # older per-flow marker list omitted its wording. Anchor to that duration
    # so previous activity lengths, rewards and answer windows are not mixed in.
    for pattern in (_COOLDOWN_ANCHOR_RE, _REMAINING_TIME_ANCHOR_RE):
        for anchor in pattern.finditer(normalized):
            parsed = _parse_duration_text(anchor.group(1))
            if parsed is not None:
                return parsed

    if cooldown_markers and not any(marker in normalized for marker in cooldown_markers):
        return None

    return _parse_duration_text(normalized)


def parse_duration_seconds(text: str) -> int | None:
    normalized = (text or "").strip().replace("個", "")
    if not normalized:
        return None
    return _parse_duration_text(normalized)


def parse_daily_mind(text: str) -> tuple[int, int] | None:
    """Return (remaining, limit) from a battle reply's 今日神念 field."""
    match = _DAILY_MIND_RE.search(text or "")
    if not match:
        return None
    remaining, limit = int(match.group(1)), int(match.group(2))
    if limit <= 0 or remaining < 0 or remaining > limit:
        return None
    return remaining, limit


def compute_inline_cooldown_wait_budget_seconds(
    *,
    run_started_monotonic: float,
    run_max_seconds: int,
    max_inline_wait_seconds: int,
    safety_margin_seconds: int = 15,
) -> float:
    elapsed = max(0.0, time.monotonic() - run_started_monotonic)
    remaining_runtime = max(0.0, float(run_max_seconds) - elapsed - float(safety_margin_seconds))
    return max(0.0, min(float(max_inline_wait_seconds), remaining_runtime))


def compute_repeat_guard_seconds(
    *,
    min_repeat_seconds: int,
    pre_repeat_probe_seconds: int,
    recent_success_ts: float,
) -> int:
    guard_seconds = max(0, int(min_repeat_seconds))
    probe_seconds = max(0, int(pre_repeat_probe_seconds))
    if guard_seconds <= 0:
        return 0
    if probe_seconds <= 0:
        return guard_seconds
    if recent_success_ts <= 0:
        return guard_seconds
    return max(0, guard_seconds - probe_seconds)


@dataclass
class AckPolicy:
    reply_bot_id: int = 0
    ack_markers: list[str] = field(default_factory=list)
    success_markers: list[str] = field(default_factory=list)
    failure_markers: list[str] = field(default_factory=list)
    ack_any_reply: bool = False
    ack_reply_bot_with_time_only: bool = False
    # Flows with a strict success oracle may accept only an explicit success
    # marker or a parsed cooldown reply; arbitrary bot replies stay unverified.
    require_success_or_cooldown: bool = False
    trusted_sender_ids: set[int] = field(default_factory=lambda: set(OFFICIAL_REPLY_SENDER_IDS))
    settlement_markers: list[str] = field(default_factory=list)


@dataclass
class ReplyOutcome:
    matched_ids: list[int] = field(default_factory=list)
    acked: bool = False
    reset_triggered: bool = False
    cooldown_retry_seconds: int | None = None
    cooldown_reply_text: str = ""

    acked_at_ts: float | None = None
    ack_reply_message_id: int | None = None
    ack_reply_text: str = ""
    daily_mind_remaining: int | None = None
    daily_mind_limit: int | None = None
    duel_anti_repeat: bool = False
    succeeded: bool = False
    failed: bool = False
    terminal_status: str = ""
    ack_reply_sender_id: int | None = None
    command_message_id: int | None = None
    command_sent_at_ts: float | None = None

    def as_tuple(self) -> tuple[list[int], bool, bool, int | None, str, float | None, int | None]:
        return (
            self.matched_ids,
            self.acked,
            self.reset_triggered,
            self.cooldown_retry_seconds,
            self.cooldown_reply_text,
            self.acked_at_ts,
            self.ack_reply_message_id,
        )


def decide_poll_action(outcome: ReplyOutcome) -> str:
    if outcome.reset_triggered:
        return "reset_resend"
    if outcome.cooldown_retry_seconds is not None:
        return "cooldown"
    if outcome.failed:
        return "failure_resend"
    if outcome.acked:
        return "acknowledged"
    return "keep_waiting"


def classify_reply_messages(
    messages: Sequence[object],
    *,
    topic_id: int | None,
    own_sender_id: int,
    target_message: str,
    sent_message_id: int | None,
    reset_triggers: Sequence[str],
    cooldown_markers: Sequence[str],
    policy: AckPolicy,
    log: Callable[..., None] = log_print,
) -> ReplyOutcome:
    normalized_topic_id = normalize_topic_id(topic_id)
    messages_by_id = {m.id: m for m in messages if getattr(m, "id", None) is not None}
    own_candidates: set[int] = set()
    reset_trigger_norms = [t.strip() for t in reset_triggers if t.strip()]


    for m in messages:
        top_msg_id = normalize_topic_id(get_message_top_id(m))
        if (
            m.sender_id == own_sender_id
            and (m.message or "").strip() == target_message
            and top_msg_id == normalized_topic_id
        ):
            own_candidates.add(m.id)
        if sent_message_id is not None and getattr(m, "id", None) == sent_message_id:
            own_candidates.add(m.id)

    outcome = ReplyOutcome()
    replied_to_own: set[int] = set()
    for m in messages:
        allowed = set(policy.trusted_sender_ids)
        if policy.reply_bot_id:
            allowed.add(policy.reply_bot_id)
        if not official_message(m, allowed=allowed):
            continue
        reply_to_msg_id = getattr(m, "reply_to_msg_id", None)
        if not reply_to_msg_id:
            reply = getattr(m, "reply_to", None)
            reply_to_msg_id = getattr(reply, "reply_to_msg_id", None)
        if not reply_to_msg_id:
            continue

        replied_msg = messages_by_id.get(reply_to_msg_id)
        if replied_msg is None:
            continue

        replied_top_msg_id = normalize_topic_id(get_message_top_id(replied_msg))
        replied_msg_id = getattr(replied_msg, "id", None)
        sent_message_match = sent_message_id is not None and replied_msg_id == sent_message_id
        if not (
            replied_msg.sender_id == own_sender_id
            and (replied_msg.message or "").strip() == target_message
            and (sent_message_match or replied_top_msg_id == normalized_topic_id)
        ):
            continue
        if sent_message_id is not None and not sent_message_match:
            continue
        sent_date = getattr(replied_msg, "date", None)
        reply_date = getattr(m, "edit_date", None) or getattr(m, "date", None)
        if (getattr(m, "id", 0) <= replied_msg_id or
                (sent_date and reply_date and reply_date.timestamp() < sent_date.timestamp() - 1)):
            continue

        own_candidates.add(replied_msg.id)

        bot_text = (m.message or "").strip()
        duel_anti_repeat_hit = str(target_message or "").strip().startswith(".斗法") and any(
            marker in bot_text for marker in DUEL_ANTI_REPEAT_MARKERS
        )
        duel_outcome_hit = str(target_message or "").strip().startswith(".斗法") and all(
            marker in bot_text for marker in DUEL_OUTCOME_MARKERS[:2]
        ) and any(marker in bot_text for marker in DUEL_OUTCOME_MARKERS[2:])
        trigger_hit = any(trigger in bot_text for trigger in reset_trigger_norms)
        success_hit = any(marker in bot_text for marker in policy.success_markers)
        failure_hit = any(marker in bot_text for marker in policy.failure_markers)
        ack_marker_hit = any(marker in bot_text for marker in policy.ack_markers)
        settlement_hit = any(marker in bot_text for marker in policy.settlement_markers)
        daily_mind = parse_daily_mind(bot_text)
        explicit_wait = bool(_COOLDOWN_ANCHOR_RE.search(bot_text) or _REMAINING_TIME_ANCHOR_RE.search(bot_text))
        parsed_cooldown_seconds = (parse_cooldown_retry_delay_seconds(bot_text, cooldown_markers)
                                   if not success_hit and (cooldown_markers or explicit_wait) else None)
        cooldown_retry_hit = parsed_cooldown_seconds is not None
        if cooldown_retry_hit:
            # "Cannot yet, N seconds remaining" is a timed refusal. An ordinary
            # failure retry must never bypass the server's remaining time.
            failure_hit = False
        sender_is_reply_bot = bool(policy.reply_bot_id) and getattr(m, "sender_id", None) == policy.reply_bot_id
        duel_count_hit = str(target_message or "").strip().startswith(".斗法 ") and daily_mind is not None
        marker_based_ack = duel_anti_repeat_hit or duel_outcome_hit or duel_count_hit or trigger_hit or success_hit or failure_hit or cooldown_retry_hit or ack_marker_hit or settlement_hit
        if policy.require_success_or_cooldown:
            qualifies_as_ack = success_hit or failure_hit or cooldown_retry_hit or settlement_hit or trigger_hit or duel_anti_repeat_hit or duel_outcome_hit or duel_count_hit
        else:
            qualifies_as_ack = marker_based_ack

        if not qualifies_as_ack:
            log(
                "Ignoring non-ack reply "
                f"message_id={getattr(m, 'id', None)} sender_id={getattr(m, 'sender_id', None)} "
                f"reply_to_own_message_id={getattr(replied_msg, 'id', None)} text={bot_text!r}"
            )
            continue

        ack_reasons = []
        if policy.ack_any_reply:
            ack_reasons.append("any_reply")
        if policy.ack_reply_bot_with_time_only:
            ack_reasons.append("reply_bot_with_time_only")
        if sender_is_reply_bot:
            ack_reasons.append("reply_bot")

        if duel_anti_repeat_hit:
            ack_reasons.append("duel_anti_repeat")
        if duel_outcome_hit:
            ack_reasons.append("duel_outcome")
        if trigger_hit:
            ack_reasons.append("reset_trigger")
        if success_hit:
            ack_reasons.append("success_marker")
        if failure_hit:
            ack_reasons.append("failure_marker")
        if cooldown_retry_hit:
            ack_reasons.append("cooldown_marker")
        if ack_marker_hit:
            ack_reasons.append("ack_marker")
        if daily_mind is not None:
            ack_reasons.append("daily_mind")
        log(
            "Accepted ack reply "
            f"message_id={getattr(m, 'id', None)} sender_id={getattr(m, 'sender_id', None)} "
            f"reply_to_own_message_id={getattr(replied_msg, 'id', None)} reasons={','.join(ack_reasons)}"
        )

        replied_to_own.add(replied_msg.id)
        reply_ts = reply_date.timestamp() if reply_date is not None else None
        if outcome.acked and (outcome.acked_at_ts or 0) > (reply_ts or 0):
            continue
        # Do not mix flags from separate commands or an older rejected reply.
        outcome = ReplyOutcome()
        outcome.acked = True
        outcome.command_message_id = replied_msg.id
        outcome.command_sent_at_ts = sent_date.timestamp() if sent_date else None
        outcome.ack_reply_sender_id = getattr(m, "sender_id", None)
        outcome.ack_reply_message_id = getattr(m, "id", None)
        outcome.ack_reply_text = bot_text
        outcome.terminal_status = "settled"
        if success_hit and not (failure_hit or trigger_hit or duel_anti_repeat_hit):
            outcome.succeeded = True
            outcome.terminal_status = "success"
        if failure_hit or duel_anti_repeat_hit:
            outcome.failed = True
            outcome.terminal_status = "failed"
        if duel_anti_repeat_hit:
            outcome.duel_anti_repeat = True

        if trigger_hit:
            outcome.reset_triggered = True
        if reply_date is not None:
            reply_ts = reply_date.timestamp()
            if outcome.acked_at_ts is None or reply_ts > outcome.acked_at_ts:
                outcome.acked_at_ts = reply_ts
                outcome.ack_reply_message_id = getattr(m, "id", None)
                outcome.ack_reply_text = bot_text
                if daily_mind is not None:
                    outcome.daily_mind_remaining, outcome.daily_mind_limit = daily_mind
        if cooldown_retry_hit and parsed_cooldown_seconds is not None:
            if outcome.cooldown_retry_seconds is None or parsed_cooldown_seconds > outcome.cooldown_retry_seconds:
                outcome.cooldown_retry_seconds = parsed_cooldown_seconds
                outcome.cooldown_reply_text = bot_text
                outcome.terminal_status = "cooldown"
    outcome.matched_ids = sorted(mid for mid in own_candidates if mid in replied_to_own)
    return outcome


def normalize_username(value: str | None) -> str:
    if not value:
        return ""
    return value.strip().lstrip("@").lower()


def is_matching_summary(content: str, expected_username: str) -> bool:
    if not any(marker in content for marker in STATUS_SUMMARY_MARKERS):
        return False
    normalized_expected = normalize_username(expected_username)
    if not normalized_expected:
        return True
    lowered = content.lower()
    return bool(re.search(r"(?<![\w@])@" + re.escape(normalized_expected) + r"(?![A-Za-z0-9_])", lowered))


def classify_status_reply(text: str, *, expected_username: str = "") -> tuple[str, int | None]:
    content = (text or "").strip()
    if not content:
        return "unknown", None
    if is_matching_summary(content, expected_username):
        return "summary", None
    if any(marker in content for marker in STATUS_NOT_IN_RETREAT_MARKERS):
        return "not_in_retreat", None
    if any(marker in content for marker in STATUS_COUNTDOWN_MARKERS):
        return "countdown", parse_duration_seconds(content)
    if any(marker in content for marker in STATUS_ALREADY_IN_RETREAT_MARKERS):
        return "already_in_retreat", None
    return "unknown", None


def classify_action_reply(text: str, *, expected_username: str = "") -> tuple[str, int | None]:
    content = (text or "").strip()
    if not content:
        return "unknown", None
    if any(marker in content for marker in ACTION_STARTED_MARKERS):
        duration = parse_duration_seconds(content) or DEFAULT_DURATION_SECONDS
        return "started", duration
    if any(marker in content for marker in ACTION_ALREADY_IN_RETREAT_MARKERS):
        return "already_in_retreat", None
    if is_matching_summary(content, expected_username):
        return "summary", None
    return "unknown", None
