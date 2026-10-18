"""Apply one official duel settlement once; delivery never consumes quota."""
from datetime import datetime
from zoneinfo import ZoneInfo

from command_confirmation import valid_receipt_identity


def apply_receipt(quota, receipt, *, account, today, limit=5):
    quota = dict(quota) if quota.get("date") == today else {"date": today, "sent_count": 0}
    quota["limit"] = min(5, limit)
    mid = receipt.get("sent_message_id") or receipt.get("message_id")
    rid = receipt.get("ack_reply_message_id")
    valid = valid_receipt_identity(receipt, actor_id=int(account.removeprefix("tg")),
                                   flow="doufa_demo_player_02", command=".斗法 demo_player_01")
    if not valid:
        return quota
    reply_day = datetime.fromtimestamp(float(receipt.get("ts") or 0), ZoneInfo("Asia/Shanghai")).date().isoformat()
    if reply_day != today:
        return quota
    if receipt.get("duel_anti_repeat") is True:
        quota["last_send_status"] = "anti_repeat_paused"
        return quota
    known = set(quota.get("counted_receipt_ids", []))
    if rid in known or receipt.get("failed") or receipt.get("status") not in {"acknowledged", "settled"}:
        return quota
    remaining, server_limit = receipt.get("daily_mind_remaining"), receipt.get("daily_mind_limit")
    current = max(0, int(quota.get("sent_count", 0)))
    if type(remaining) is int and type(server_limit) is int and 0 <= remaining <= server_limit:
        quota["sent_count"] = min(limit, max(current, server_limit - remaining))
    else:
        quota["sent_count"] = min(limit, current + 1)
    quota.update(counted_receipt_ids=sorted(known | {rid}), last_counted_message_id=mid,
                 last_send_ts=receipt.get("actual_send_ts"), last_send_status=receipt["status"])
    return quota
