import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from miniapp_shared.notify import Notifier
from miniapp_shared.summaries import journey_notification, star_notification, tower_notification

SH = ZoneInfo("Asia/Shanghai")


def _ts(hour, minute=0):
    return datetime(2026, 9, 20, hour, minute, tzinfo=SH).timestamp()


class NotifierTests(unittest.TestCase):
    def test_send_once_dedupes_and_records_failures_without_raising(self):
        sent = []

        def sender(text):
            sent.append(text)
            if len(sent) == 1:
                raise RuntimeError("hermes_send_failed: boom")
            return {"target": "telegram:1", "message_id": 42, "success": True}

        with tempfile.TemporaryDirectory() as tmp:
            notifier = Notifier(Path(tmp), "tower_miniapp", sender=sender, now=lambda: 1.0)
            first = notifier.send_once("2026-09-20:complete", "hello")
            self.assertEqual("delivery_failed", first["status"])
            second = notifier.send_once("2026-09-20:complete", "hello")
            self.assertEqual(("delivered", 42), (second["status"], second["receipt"]["message_id"]))
            third = notifier.send_once("2026-09-20:complete", "hello again")
            self.assertEqual("already_delivered", third["status"])
            self.assertEqual(2, len(sent))
            self.assertTrue((Path(tmp) / "state/tower_miniapp/notifications.json").is_file())


    def test_concurrent_processes_send_a_key_once(self):
        import threading
        import time
        sent = []

        def sender(text):
            sent.append(text)
            time.sleep(0.2)  # the CLI round trip during which another cron run starts
            return {"target": "telegram:1", "message_id": len(sent), "success": True}

        with tempfile.TemporaryDirectory() as tmp:
            outcomes = []
            runs = [threading.Thread(target=lambda: outcomes.append(
                Notifier(Path(tmp), "tianji_trial", sender=sender).send_once("2026-09-20:complete", "report")["status"]))
                for _ in range(2)]
            for run in runs:
                run.start()
            for run in runs:
                run.join()
        self.assertEqual(["report"], sent)
        self.assertEqual(["already_delivered", "delivered"], sorted(outcomes))

    def test_timed_out_send_is_unconfirmed_and_never_resent(self):
        import subprocess
        from unittest.mock import patch
        from miniapp_shared.notify import deliver_with_hermes
        calls = []

        def run(*args, **kwargs):
            calls.append(kwargs.get("input"))
            raise subprocess.TimeoutExpired(args[0], 30)

        sender = lambda text: deliver_with_hermes(text, target="telegram:1", binary="hermes", sleep=lambda _s: None)  # noqa: E731
        with tempfile.TemporaryDirectory() as tmp, patch("miniapp_shared.notify.subprocess.run", side_effect=run):
            notifier = Notifier(Path(tmp), "tianji_trial", sender=sender, now=lambda: 1.0)
            first = notifier.send_once("2026-09-20:complete", "report")
            second = notifier.send_once("2026-09-20:complete", "report")
            self.assertTrue(notifier.delivered("2026-09-20:complete"))
        self.assertEqual(("delivery_unconfirmed", "already_delivered"), (first["status"], second["status"]))
        self.assertEqual(1, len(calls))  # neither retried in the loop nor on the next tick


class FailureDetailTests(unittest.TestCase):
    def test_json_error_wins_over_the_pending_restart_banner(self):
        from miniapp_shared.notify import _failure_detail
        banner = ("⚠ A previous `hermes update` pulled new code but did not restart running gateways.\n"
                  "  Gateways may still be serving pre-update modules (mixed sys.modules).\n"
                  "  Run `hermes update` or `hermes gateway restart`.\n")
        self.assertEqual("Send failed: ConnectError('')",
                         _failure_detail('{"error": "Send failed: ConnectError(\'\')"}', banner))
        self.assertEqual("hermes send: --to is required", _failure_detail("", banner + "hermes send: --to is required\n"))
        self.assertEqual("delivery failed", _failure_detail("", banner))


class DeliverRetryTests(unittest.TestCase):
    def test_non_zero_exit_is_retried_but_an_unconfirmed_receipt_is_not(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        from miniapp_shared.notify import deliver_with_hermes
        calls = []
        outcomes = [SimpleNamespace(returncode=1, stdout='{"error": "Send failed: ConnectError(\'\')"}', stderr=""),
                    SimpleNamespace(returncode=0, stdout='{"success": true, "message_id": 77}', stderr="")]
        def run(*args, **kwargs):
            calls.append(kwargs.get("input"))
            return outcomes.pop(0)
        slept = []
        with patch("miniapp_shared.notify.subprocess.run", side_effect=run):
            receipt = deliver_with_hermes("hi", target="telegram:1", binary="hermes", sleep=slept.append)
        self.assertEqual((77, 2, [5.0]), (receipt["message_id"], len(calls), slept))
        outcomes = [SimpleNamespace(returncode=0, stdout='{"success": false}', stderr="")]
        with patch("miniapp_shared.notify.subprocess.run", side_effect=run), self.assertRaises(RuntimeError):
            deliver_with_hermes("hi", target="telegram:1", binary="hermes", sleep=slept.append)
        self.assertEqual(3, len(calls))
        outcomes = [SimpleNamespace(returncode=1, stdout="", stderr="boom")] * 3
        with patch("miniapp_shared.notify.subprocess.run", side_effect=run), self.assertRaises(RuntimeError) as ctx:
            deliver_with_hermes("hi", target="telegram:1", binary="hermes", sleep=slept.append)
        self.assertEqual(("hermes_send_failed: boom", 6), (str(ctx.exception), len(calls)))


class SummaryTests(unittest.TestCase):
    def test_tower_all_complete_and_partial_at_day_end(self):
        summary = {"day": "2026-09-20", "accounts": [
            {"account": "tg9100000001", "status": "completed", "identities": [
                {"identity": "示例修士一", "text": "通过 8 层，抵达第 9 层，止步第 9 层"},
                {"identity": "示例绑定角色", "text": "服务器已记录今日登塔"}]},
            {"account": "tg9100000005", "status": "daily_already_terminal", "identities": [
                {"identity": "无为子", "text": "通过 5 层，抵达第 6 层"}]},
        ]}
        key, text = tower_notification(summary, now_ts=_ts(21, 30))
        self.assertEqual("2026-09-20:complete", key)
        self.assertIn("全部账号完成 2/2", text)
        self.assertIn("@demo_player_01 示例修士一: 通过 8 层", text)
        self.assertIn("@demo_player_05 无为子", text)

        pending = {"day": "2026-09-20", "accounts": [
            {"account": "tg9100000001", "status": "completed", "identities": []},
            {"account": "tg9100000005", "status": "retry", "error": "pagoda_not_ready"}]}
        self.assertIsNone(tower_notification(pending, now_ts=_ts(22)))
        key, text = tower_notification(pending, now_ts=_ts(23, 50))
        self.assertEqual("2026-09-20:partial", key)
        self.assertIn("未完成（retry pagoda_not_ready）", text)

    def test_journey_all_complete(self):
        summary = {"day": "2026-09-20", "target_daily_count": 8, "accounts": [
            {"account": "tg9100000002", "status": "daily_complete", "identities": [
                {"identity": "庚承玄", "daily_count": 8, "target": 8, "status": "daily_complete"}]},
            {"account": "tg9100000006", "status": "daily_already_complete", "identities": [
                {"identity": "离劫隐", "daily_count": 8, "target": 8, "status": "daily_complete",
                 "server_note": "wild_experience_daily_limit"}]},
        ]}
        key, text = journey_notification(summary, now_ts=_ts(3))
        self.assertEqual("2026-09-20:complete", key)
        self.assertIn("全部账号完成 2/2，每账号 8 次", text)
        self.assertIn("@demo_player_02 庚承玄: 8/8 完成", text)
        self.assertIn("@demo_player_06 离劫隐: 8/8 完成 wild_experience_daily_limit", text)
        waiting = {"day": "2026-09-20", "accounts": [
            {"account": "tg9100000002", "status": "waiting_server", "identities": [
                {"identity": "庚承玄", "daily_count": 3, "target": 8, "status": "cooldown"}]}]}
        self.assertIsNone(journey_notification(waiting, now_ts=_ts(3)))

    def test_fishing_summary_names_casts_the_server_counted_without_a_settlement(self):
        from miniapp_shared.summaries import fishing_notification
        coordinator = {"status": "daily_complete", "accounts": ["tg9100000004", "tg9100000005"]}
        rows = [{"account": "tg9100000004", "daily_cast_count": 5, "confirmed_settlement_ids": ["a", "b", "c", "d"],
                 "status": "next_failed", "error_code": "fishing_daily_limit_reached"},
                {"account": "tg9100000005", "daily_cast_count": 5, "confirmed_settlement_ids": list("abcde"), "caught": True}]
        key, text = fishing_notification(coordinator, rows, now_ts=_ts(0, 30), daily_limit=5)
        self.assertTrue(key.endswith(":complete"))
        self.assertIn("@demo_player_04: 5/5 竿，今日结算 4 次，1 竿被服务器计数但未确认结算", text)
        self.assertIn("@demo_player_05: 5/5 竿，今日结算 5 次，最后一竿上钩", text)

    def test_star_batch_with_a_failed_refresh_still_settles(self):
        batch = {"accounts": [
            {"account": "tg9100000004", "status": "wait", "action_counts": {"soothe": 1, "collect": 0, "pull": 0},
             "plots": [{"empty": False}], "min_wait_sec": 3000, "updated_at": 5.0},
            {"account": "tg9100000005", "status": "refresh_failed", "error": "bad_response",
             "action_counts": {}, "updated_at": 5.0}]}
        key, text = star_notification(batch, now_ts=_ts(10))
        self.assertIn("@demo_player_05: 失败 refresh_failed bad_response", text)

    def test_star_batch_notifies_only_when_settled_with_actions(self):
        idle = {"accounts": [
            {"account": "tg9100000004", "status": "scheduled_wait"},
            {"account": "tg9100000005", "status": "scheduled_wait"}]}
        self.assertIsNone(star_notification(idle, now_ts=_ts(10)))
        no_action = {"accounts": [
            {"account": "tg9100000004", "status": "wait", "action_counts": {"soothe": 0, "collect": 0, "pull": 0},
             "plots": [{"empty": False}], "min_wait_sec": 3000, "updated_at": 1.0},
            {"account": "tg9100000005", "status": "wait", "action_counts": {}, "plots": [], "updated_at": 1.0}]}
        self.assertIsNone(star_notification(no_action, now_ts=_ts(10)))
        batch = {"accounts": [
            {"account": "tg9100000004", "status": "wait", "action_counts": {"soothe": 1, "collect": 1, "pull": 2},
             "plots": [{"empty": False}, {"empty": False}], "min_wait_sec": 129600, "updated_at": 5.0},
            {"account": "tg9100000005", "status": "action_failed", "error": "cultivation_insufficient",
             "action_counts": {"soothe": 0, "collect": 1, "pull": 0}, "updated_at": 5.0}]}
        key, text = star_notification(batch, now_ts=_ts(10))
        self.assertTrue(key.startswith("2026-09-20:"))
        self.assertIn("@demo_player_04: 安抚1 收集1 牵引天雷星2，2 盘（空 0），下次约 36.0 小时 后", text)
        self.assertIn("@demo_player_05: 失败 action_failed cultivation_insufficient", text)
        same_key, _ = star_notification(batch, now_ts=_ts(10, 5))
        self.assertEqual(key, same_key)


if __name__ == "__main__":
    unittest.main()
