import json
import tempfile
import unittest
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

from run_miniapp_fishing_cycle import _notify_if_due

SH = ZoneInfo("Asia/Shanghai")
TODAY = datetime.now(SH).date()


def _ts(hour, minute=0):
    # account_result() resets counts whose settlement_date is not the real
    # Asia/Shanghai today, so the fixture must live on the real current day.
    return datetime.combine(TODAY, dtime(hour, minute), tzinfo=SH).timestamp()


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class FishingNotificationTests(unittest.TestCase):
    def test_daily_complete_sends_once_and_partial_only_at_day_end(self):
        accounts = ["tg9100000002", "tg9100000003"]
        day = TODAY.isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "miniapp_fishing"
            notifier_root = Path(tmp) / "fishing_root"
            _write(state_root / "fishing_coordinator.json",
                   {"status": "daily_complete", "accounts": accounts, "result": {"daily_cast_counts": {}}})
            for account in accounts:
                _write(state_root / "accounts" / account / "fishing.json", {"account": account, "result": {
                    "status": "settled_reconciled", "caught": True, "daily_cast_count": 5,
                    "settlement_date": day, "counted_settlement_ids": ["a", "b", "c", "d", "e"],
                    "confirmed_settlement_ids": ["a", "b", "c", "d", "e"], "retry_not_before_ts": 0.0}})
            sent = []

            def sender(text):
                sent.append(text)
                return {"target": "telegram:1", "message_id": 7, "success": True}

            first = _notify_if_due(state_root, now_ts=_ts(15), sender=sender, notifier_root=notifier_root)
            self.assertEqual(("delivered", f"{day}:complete"), (first["status"], first["key"]))
            self.assertIn(f"【灵溪垂钓】{day} 全部账号完成 2/2，每账号 5 竿", sent[0])
            self.assertIn("@demo_player_02: 5/5 竿，今日结算 5 次，最后一竿上钩", sent[0])
            again = _notify_if_due(state_root, now_ts=_ts(15, 5), sender=sender, notifier_root=notifier_root)
            self.assertEqual("already_delivered", again["status"])
            self.assertEqual(1, len(sent))
            self.assertTrue((notifier_root / "state/miniapp_fishing/notifications.json").is_file())

            # An incomplete day is silent until 23:45, then reported once as partial.
            _write(state_root / "fishing_coordinator.json", {"status": "running", "accounts": accounts, "result": {}})
            _write(state_root / "accounts" / accounts[1] / "fishing.json", {"account": accounts[1], "result": {
                "status": "failed", "error_code": "fishing_rod_missing", "daily_cast_count": 2,
                "settlement_date": day, "confirmed_settlement_ids": ["a", "b"], "retry_not_before_ts": 0.0}})
            self.assertIsNone(_notify_if_due(state_root, now_ts=_ts(20), sender=sender, notifier_root=notifier_root))
            partial = _notify_if_due(state_root, now_ts=_ts(23, 50), sender=sender, notifier_root=notifier_root)
            self.assertEqual(f"{day}:partial", partial["key"])
            self.assertIn("@demo_player_03: 2/5 竿，今日结算 2 次，状态 failed fishing_rod_missing", sent[-1])

    def test_missing_coordinator_is_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(_notify_if_due(Path(tmp), now_ts=_ts(15), sender=lambda _t: {}, notifier_root=Path(tmp)))

    def test_account_that_finishes_after_the_summary_gets_one_late_report(self):
        # 2026-09-26: @demo_player_01 joined after the 00:27 summary of the other
        # accounts; its 15 casts were never reported because the key was used.
        day = TODAY.isoformat()
        early, late = ["tg9100000002", "tg9100000003"], "tg9100000001"
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "miniapp_fishing"
            notifier_root = Path(tmp) / "fishing_root"
            _write(state_root / "fishing_coordinator.json", {"status": "daily_complete", "accounts": early, "result": {}})
            for account in early:
                _write(state_root / "accounts" / account / "fishing.json", {"account": account, "result": {
                    "status": "settled_reconciled", "daily_cast_count": 5, "settlement_date": day,
                    "confirmed_settlement_ids": list("abcde"), "retry_not_before_ts": 0.0}})
            sent = []

            def sender(text):
                sent.append(text)
                return {"target": "telegram:1", "message_id": len(sent), "success": True}

            def notify(hour, minute=0):
                return _notify_if_due(state_root, now_ts=_ts(hour, minute), sender=sender, notifier_root=notifier_root)

            self.assertEqual(f"{day}:complete", notify(0, 27)["key"])
            self.assertIn("全部账号完成 2/2", sent[0])

            # The late account is added and fishes to the server limit.
            _write(state_root / "fishing_coordinator.json",
                   {"status": "running", "accounts": [late, *early], "result": {}})
            result = {"status": "settled_reconciled", "daily_cast_count": 7, "daily_cast_limit": None,
                      "server_daily_limit_reached": False, "settlement_date": day,
                      "confirmed_settlement_ids": [f"s{i}" for i in range(7)], "retry_not_before_ts": 0.0,
                      "chum": {"status": "active", "used_today": {"rice": 0, "grass": 1, "demon": 1}}}
            _write(state_root / "accounts" / late / "fishing.json", {"account": late, "result": result})
            self.assertIsNone(notify(0, 50))
            _write(state_root / "fishing_coordinator.json",
                   {"status": "daily_complete", "accounts": [late, *early], "result": {}})
            result.update(status="next_failed", error_code="fishing_daily_limit_reached", daily_cast_count=15,
                          server_daily_limit_reached=True, confirmed_settlement_ids=[f"s{i}" for i in range(15)],
                          chum={"status": "active", "used_today": {"rice": 0, "grass": 2, "demon": 1}})
            _write(state_root / "accounts" / late / "fishing.json", {"account": late, "result": result})

            supplement = notify(1, 15)
            self.assertEqual(("delivered", f"{day}:late:{late}"), (supplement["status"], supplement["key"]))
            self.assertEqual(2, len(sent))
            self.assertIn(f"【灵溪垂钓】{day} 补报：@demo_player_01 在当日汇总发出后才完成", sent[1])
            self.assertIn("@demo_player_01: 15 竿（服务器上限已到），今日结算 15 次，打窝 灵草窝×2、妖腥窝×1", sent[1])
            self.assertNotIn("@demo_player_02", sent[1])
            # Reported once; later ticks stay quiet.
            self.assertEqual("already_delivered", notify(1, 20)["status"])
            self.assertEqual(2, len(sent))

    def test_day_without_a_coverage_record_counts_as_fully_reported(self):
        day = TODAY.isoformat()
        accounts = ["tg9100000001", "tg9100000002"]
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "miniapp_fishing"
            notifier_root = Path(tmp) / "fishing_root"
            _write(state_root / "fishing_coordinator.json", {"status": "daily_complete", "accounts": accounts})
            for account in accounts:
                _write(state_root / "accounts" / account / "fishing.json", {"account": account, "result": {
                    "status": "settled_reconciled", "daily_cast_count": 5, "settlement_date": day,
                    "confirmed_settlement_ids": list("abcde"), "retry_not_before_ts": 0.0}})
            # Delivered before reported_accounts.json existed.
            _write(notifier_root / "state/miniapp_fishing/notifications.json",
                   {"delivered": [{"key": f"{day}:complete", "at": 1.0}], "failures": []})
            sent = []
            outcome = _notify_if_due(state_root, now_ts=_ts(2), sender=lambda text: sent.append(text) or {},
                                     notifier_root=notifier_root)
            self.assertEqual("already_delivered", outcome["status"])
            self.assertEqual([], sent)


if __name__ == "__main__":
    unittest.main()
