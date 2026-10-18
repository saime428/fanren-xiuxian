"""Official reply identity and durable retries for a single account/command.

The flow sender owns command_pending.json under its existing send lock. The
deadline dispatcher only reads it. Telegram delivery never resolves this file.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from miniapp_shared.accounts import GAME_CHAT_ID, GAME_GROUP, GAME_TOPIC_ID
from state_store import load_json_dict, persist_json_atomic

OFFICIAL_REPLY_SENDER_IDS = frozenset({
    7228205882, 7965897083, 8735907987, 8816935632, 8918867302, 8944702077,
    8964348409, 8980154525, 8788895566, 8917921351, 8388633812, 8861328042,
})
CANONICAL_CHAT_ID = GAME_CHAT_ID
CANONICAL_TOPIC_ID = GAME_TOPIC_ID
CANONICAL_TARGET = f"@{GAME_GROUP}"
UNCONFIRMED_SEND_STATUSES = frozenset({
    "sent", "sent_no_ack", "resent_pending_ack", "failure_resent_pending_ack",
    "send_submitted", "send_outcome_unknown", "reply_retry_pending", "started",
})


def official_message(message, *, allowed=OFFICIAL_REPLY_SENDER_IDS, chat_id=None):
    return (getattr(message, "sender_id", None) in allowed
            and getattr(message, "fwd_from", None) is None
            and (chat_id is None or getattr(message, "chat_id", chat_id) == chat_id))


def no_reply_interval(*, minimum=120.0):
    return max(float(minimum), float(os.getenv("TG_NO_REPLY_RETRY_SECONDS", minimum)))


def validate_command_identity(target, topic_id, actor_id, *, expected_account=""):
    """Validate the outbound route and the business actor, never a session alias."""
    if str(target) not in {CANONICAL_TARGET, str(CANONICAL_CHAT_ID)} or int(topic_id) != CANONICAL_TOPIC_ID:
        raise ValueError("command outbound route is not the canonical topic")
    if not actor_id or (expected_account and expected_account != f"tg{actor_id}"):
        raise ValueError("command sender account mismatch")


def valid_receipt_identity(payload, *, actor_id, flow, command=None):
    """Check durable receipt attribution before using it as a cadence/health guard."""
    if not isinstance(payload, dict):
        return False
    sent_id = payload.get("sent_message_id") or payload.get("message_id")
    reply_id = payload.get("ack_reply_message_id") or payload.get("reply_message_id")
    sender_id = payload.get("ack_reply_sender_id") or payload.get("reply_sender_id")
    return (payload.get("acked") is True
            and type(payload.get("actor_id")) is int and payload["actor_id"] == actor_id
            and payload.get("flow_slug") == flow
            and str(payload.get("target")) in {CANONICAL_TARGET, str(CANONICAL_CHAT_ID)}
            and type(payload.get("topic_id")) is int and payload["topic_id"] == CANONICAL_TOPIC_ID
            and isinstance(payload.get("message"), str) and bool(payload["message"].strip())
            and (command is None or payload["message"] == command)
            and type(sent_id) is int and sent_id > 0
            and type(reply_id) is int and reply_id > sent_id
            and type(sender_id) is int and sender_id in OFFICIAL_REPLY_SENDER_IDS)


def select_reply_scan_ids(message_ids, checked_at, *, now, limit=2, min_interval=60):
    """Bound network lookups without dropping any unconfirmed command copy."""
    ids = sorted({int(mid) for mid in message_ids if mid})
    if not ids or limit <= 0:
        return []
    due = [mid for mid in ids if now - float(checked_at.get(str(mid), 0)) >= min_interval]
    # The newest send gets an early lookup; the remaining slot rotates fairly
    # through older copies. Recent/push receipts still match every copy.
    selected = sorted(due, key=lambda mid: (mid != ids[-1], checked_at.get(str(mid), 0), mid))[:limit]
    for mid in selected:
        checked_at[str(mid)] = now
    return selected


class PendingCommand:
    def __init__(self, state_dir, *, flow, command, target, topic_id, actor_id=0, clock=None,
                 filename="command_pending.json"):
        self.path = Path(state_dir) / filename
        self.clock = clock or time.time
        self.identity = {"flow_slug": flow, "message": command, "target": str(target),
                         "topic_id": topic_id, "actor_id": actor_id}
        self.data = load_json_dict(self.path)
        if self.data and any(self.data.get(k) != v for k, v in self.identity.items()):
            raise ValueError("pending command identity mismatch")
        if not self.data:
            self.data = {**self.identity, "version": 1, "pending": False, "attempts": []}

    @property
    def pending(self):
        return bool(self.data.get("pending"))

    @property
    def message_ids(self):
        return [int(row["message_id"]) for row in self.data.get("attempts", [])
                if row.get("message_id") and not row.get("resolved")]

    def save(self):
        self.data["updated_at_ts"] = self.clock()
        persist_json_atomic(self.path, self.data)

    def reply_scan_ids(self, *, limit=2, min_interval=60):
        selected = select_reply_scan_ids(self.message_ids, self.data.setdefault("reply_scans_by_message", {}),
                                        now=self.clock(), limit=limit, min_interval=min_interval)
        if selected:
            self.save()
        return selected

    def import_attempt(self, row, interval):
        """Migrate a delivered-but-unconfirmed legacy attempt without erasing it."""
        if self.path.exists() or row.get("status") not in UNCONFIRMED_SEND_STATUSES or row.get("acked"):
            return
        if row.get("message") not in (None, self.identity["message"]):
            return
        ids = []
        for key in ("original_sent_message_id", "sent_message_id", "message_id", "resent_message_id"):
            if row.get(key) and int(row[key]) not in ids:
                ids.append(int(row[key]))
        if not ids:
            return
        ts = float(row.get("actual_send_ts") or row.get("ts") or self.clock())
        self.data.update(pending=True, first_attempt_ts=ts, next_retry_ts=ts + interval,
                         attempts=[{"message_id": mid, "sent_at_ts": ts, "legacy": True} for mid in ids])
        self.save()

    def begin(self, interval):
        now = self.clock()
        if not self.pending:
            self.data = {**self.identity, "version": 1, "pending": True,
                         "first_attempt_ts": now, "attempts": []}
        self.data["attempts"].append({"submitted_at_ts": now, "message_id": None})
        self.data.update(pending=True, status="send_submitted", next_retry_ts=now + interval,
                         retry_interval_seconds=float(interval))
        self.save()  # Write intent before any network mutation.

    def sent(self, message):
        mid = getattr(message, "id", None)
        if not mid:
            self.unknown(RuntimeError("no message id"))
            return
        self.data["attempts"][-1].update(message_id=int(mid), sent_at_ts=self.clock())
        self.data["next_retry_ts"] = max(float(self.data.get("next_retry_ts") or 0),
                                         self.clock() + float(self.data.get("retry_interval_seconds") or 0))
        self.data["status"] = "awaiting_reply"
        self.save()

    def dispatched(self, interval):
        """Record the real RPC boundary after waiting for the global send gate."""
        now = self.clock()
        self.data["attempts"][-1]["dispatched_at_ts"] = now
        self.data["next_retry_ts"] = max(float(self.data.get("next_retry_ts") or 0), now + interval)
        self.save()

    def unknown(self, exc):
        self.data.update(status="send_outcome_unknown", error_type=type(exc).__name__)
        wait = float(getattr(exc, "seconds", 0) or 0)
        if wait > 0:
            self.data["next_retry_ts"] = max(float(self.data.get("next_retry_ts") or 0), self.clock() + wait + 1)
        self.save()

    def recover_deliveries(self, messages):
        """A lost RPC result can recover IDs, but still needs a business reply."""
        changed = False
        for row in list(self.data.get("attempts", [])):
            if row.get("message_id") or row.get("resolved"):
                continue
            start = float(row.get("dispatched_at_ts") or row.get("submitted_at_ts") or 0)
            recovered_ids = []
            for message in messages:
                reply = getattr(message, "reply_to", None)
                topic = getattr(reply, "reply_to_top_id", None) or getattr(reply, "reply_to_msg_id", None)
                date = getattr(message, "date", None)
                if (getattr(message, "sender_id", None) == self.identity["actor_id"]
                        and (getattr(message, "message", "") or "").strip() == self.identity["message"]
                        and topic == self.identity["topic_id"] and date
                        and start - 1 <= date.timestamp() <= start + 60
                        and getattr(message, "id", None) not in self.message_ids):
                    self.data["attempts"].append({"message_id": message.id, "sent_at_ts": date.timestamp(),
                                                  "recovered": True})
                    recovered_ids.append(message.id)
                    changed = True
            if recovered_ids:
                row.update(resolved=True, recovered_message_ids=recovered_ids)
        if changed:
            self.save()

    def reject_attempt(self, message_id, receipt_id):
        for row in self.data.get("attempts", []):
            if row.get("message_id") == message_id:
                row.update(resolved=True, receipt_message_id=receipt_id)
        self.save()

    def finish(self, *, status, receipt_id, reply_ts):
        self.data.update(pending=False, status=status, receipt_message_id=receipt_id,
                         reply_ts=reply_ts, next_retry_ts=None)
        self.data.pop("error_type", None)
        self.save()

    def retry_payload(self):
        return {"pending_command_path": str(self.path),
                "retry_not_before_ts": self.data.get("next_retry_ts"),
                "unconfirmed_message_ids": self.message_ids,
                "attempt_count": len(self.data.get("attempts", []))}


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == "--pending":
        directory = Path(sys.argv[2])
        pending = load_json_dict(directory / "command_pending.json").get("pending")
        attempt = load_json_dict(directory / "last_attempt.json")
        legacy = (attempt.get("status") in UNCONFIRMED_SEND_STATUSES and not attempt.get("acked")
                  and attempt.get("message") == os.getenv("TG_MESSAGE")
                  and attempt.get("flow_slug") == os.getenv("TG_FLOW_SLUG"))
        raise SystemExit(0 if pending or legacy else 1)
    if len(sys.argv) >= 3 and sys.argv[1] == "--cooldown-remaining":
        # Wrapper cache check: print the seconds a sender should still skip
        # because of a verified official countdown; print nothing to go ahead.
        # Reads the wrapper's TG_* / XIU_ACCOUNT_SLUG environment.
        import time

        def seconds(value):
            try:
                return float(value or 0)
            except (TypeError, ValueError):
                return 0.0

        mode, commands = sys.argv[2], set(sys.argv[3:])
        environ = os.environ
        actor = int(environ["XIU_ACCOUNT_SLUG"].removeprefix("tg"))
        flow = environ["TG_FLOW_SLUG"]
        grace = 0
        if mode == "cooldown_retry" and not commands:
            # The official cooldown retry recorded for this exact route+command.
            message = environ["TG_MESSAGE"]
            record = load_json_dict(Path(environ["TG_COOLDOWN_STATE_PATH"])).get(
                f"{environ['TG_TARGET']}|{environ['TG_TOPIC_ID']}|{message}")
            usable = valid_receipt_identity(record, actor_id=actor, flow=flow, command=message)
        elif mode == "status_probe" and commands:
            # The last verified status-probe countdown, if it came from one of
            # COMMANDS. An unconfirmed command or probe is never hidden behind it.
            state_dir = Path(environ["TG_STATE_DIR"])
            unconfirmed = any(
                data.get("pending") or data.get("status") in {"action_timeout", "send_submitted", "send_outcome_unknown"}
                for data in (load_json_dict(state_dir / name) for name in
                             ("command_pending.json", "status_probe_pending.json", "last_attempt.json")))
            record = load_json_dict(Path(environ["TG_STATUS_PROBE_PATH"]))
            usable = (not unconfirmed and record.get("status") == "countdown_pending"
                      and valid_receipt_identity(record, actor_id=actor, flow=flow)
                      and record.get("message") in commands)
            grace = int(environ.get("TG_STATUS_PROBE_CACHE_GRACE_SECONDS", "60"))
        else:
            raise SystemExit("usage: command_confirmation.py --cooldown-remaining "
                             "cooldown_retry | status_probe COMMAND...")
        remaining = int(max(0.0, seconds(record.get("not_before_ts")) - time.time()) + 0.999999) if usable else 0
        if remaining > grace:
            print(remaining)
        raise SystemExit(0)
    raise SystemExit("usage: command_confirmation.py --pending STATE_DIR")
