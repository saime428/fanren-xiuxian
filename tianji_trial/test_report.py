import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tianji_trial.report import build_report, notify_if_due

SH = ZoneInfo("Asia/Shanghai")


def ts(h, m):
    return datetime(2026, 9, 21, h, m, tzinfo=SH).timestamp()


def done(account, identity):
    return {"account": account, "status": "completed", "receipt_confirmed": True, "last_date": "2026-09-21",
            "identities": [{"player_id": 1, "identity": identity, "status": "completed", "completed": 3, "limit": 3,
                            "summary": "3 关完成，评级 甲等/甲等/甲等，天机残痕 +43（额外 13），余额 600"}]}


class ReportTests(unittest.TestCase):
    def test_complete_report_and_day_end_partial(self):
        summaries = {"tg9100000001": done("tg9100000001", "示例修士一"), "tg9100000002": {"status": "retry", "identities": [
            {"identity": "庚承玄", "status": "retry", "completed": 1, "limit": 3, "error": "finish_reply_pending"}]}}
        self.assertIsNone(build_report(summaries, today="2026-09-21", now_ts=ts(2, 0)))
        key, text = build_report(summaries, today="2026-09-21", now_ts=ts(3, 10))
        self.assertEqual(key, "2026-09-21:partial")
        self.assertIn("【天机试炼】2026-09-21 日终统计：完成 1/6", text)
        self.assertIn("@demo_player_02 庚承玄: 1/3 retry finish_reply_pending", text)
        full = {a: done(a, "x") for a in ("tg9100000001", "tg9100000002", "tg9100000003", "tg9100000004", "tg9100000005", "tg9100000006")}
        key, text = build_report(full, today="2026-09-21", now_ts=ts(1, 45))
        self.assertEqual(key, "2026-09-21:complete")
        self.assertIn("全部账号完成 6/6", text)

    def test_private_root_without_an_injected_sender_never_reaches_hermes(self):
        """Regression for the seven spurious 0/6 partials pushed on 2026-09-21."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            import json
            p = root / "tianji_trial" / "state" / "accounts" / "tg9100000001" / "summary.json"
            p.parent.mkdir(parents=True)
            p.write_text(json.dumps(done("tg9100000001", "主魂"), ensure_ascii=False))
            from unittest.mock import patch
            with patch("miniapp_shared.notify.deliver_with_hermes", side_effect=AssertionError("real send")):
                outcome = notify_if_due(root, now_ts=ts(9, 41))
            self.assertEqual({"status": "skipped_non_production_root"}, outcome)

    def test_notify_once_from_summary_files(self):
        sent = []
        def sender(text):
            sent.append(text)
            return {"target": "telegram:1", "message_id": 5, "success": True}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            import json
            for a in ("tg9100000001", "tg9100000002", "tg9100000003", "tg9100000004", "tg9100000005", "tg9100000006"):
                p = root / "tianji_trial" / "state" / "accounts" / a / "summary.json"
                p.parent.mkdir(parents=True)
                p.write_text(json.dumps(done(a, "x"), ensure_ascii=False))
            self.assertEqual("delivered", notify_if_due(root, now_ts=ts(1, 45), sender=sender)["status"])
            self.assertEqual("already_delivered", notify_if_due(root, now_ts=ts(1, 50), sender=sender)["status"])
            self.assertEqual(1, len(sent))


if __name__ == "__main__":
    unittest.main()
