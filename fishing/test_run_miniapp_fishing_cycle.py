import json
import plistlib
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import run_miniapp_fishing_cycle as cycle


class FishingRateLimitBackoffTests(unittest.TestCase):
    def test_midnight_reset_preserves_current_day_receipt_and_unexpired_server_backoff(self):
        import reset_miniapp_fishing_day as reset
        import time
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for account, result in {
                "tg1": {"settlement_date": today, "daily_cast_count": 1, "counted_settlement_ids": ["tg1:new"]},
                "tg2": {"settlement_date": "2020-01-01", "daily_cast_count": 5, "server_daily_limit_reached": True,
                        "retry_not_before_ts": time.time() + 300, "rate_limit_failures": 2},
            }.items():
                path = root / "accounts" / account / "fishing.json"
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps({"account": account, "result": result}))
            with patch.object(reset, "DEFAULT_STATE_ROOT", root), \
                    patch.object(reset, "load_fishing_config", return_value={"enabled": True, "accounts": ["tg1", "tg2"]}):
                self.assertEqual(0, reset.main())
            self.assertEqual(1, cycle.account_result(root, "tg1")["daily_cast_count"])
            carried = cycle.account_result(root, "tg2")
            self.assertEqual(0, carried["daily_cast_count"])
            self.assertGreater(carried["retry_not_before_ts"], time.time())
            self.assertEqual(2, carried["rate_limit_failures"])
            # yesterday's server refusal must not mark today's quota as used
            self.assertFalse(carried["server_daily_limit_reached"])
            self.assertFalse(cycle.quota_done(carried, None))

    def test_midnight_reset_skips_only_an_account_with_a_damaged_file(self):
        import reset_miniapp_fishing_day as reset
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for account, text in {"tg1": "{not json", "tg2": json.dumps({"account": "tg2", "result": {
                    "settlement_date": "2020-01-01", "daily_cast_count": 5}})}.items():
                path = root / "accounts" / account / "fishing.json"
                path.parent.mkdir(parents=True)
                path.write_text(text)
            with patch.object(reset, "DEFAULT_STATE_ROOT", root), \
                    patch.object(reset, "load_fishing_config", return_value={"enabled": True, "accounts": ["tg1", "tg2"]}):
                self.assertEqual(0, reset.main())
            self.assertEqual("{not json", (root / "accounts" / "tg1" / "fishing.json").read_text())
            self.assertFalse((root / "accounts" / "tg2" / "fishing.json").exists())
            self.assertTrue((root / "daily_reset.json").exists())

    def test_pending_official_result_deadline_is_not_cleared(self):
        result = cycle.apply_rate_limit_backoff({},
            {"status": "finish_pending_settlement", "retry_not_before_ts": 1120}, now_ts=1000)
        self.assertEqual(1120, result["retry_not_before_ts"])
        self.assertEqual([], cycle.eligible_accounts(["tg1"], {"tg1": result}, now_ts=1100))

    def test_overdue_unknown_result_gets_bounded_retry_instead_of_twenty_second_authentication(self):
        result = cycle.apply_rate_limit_backoff({},
            {"status": "finish_pending_settlement", "retry_not_before_ts": 800}, now_ts=1000)
        self.assertEqual(1120, result["retry_not_before_ts"])

    def test_business_failures_without_a_server_deadline_back_off_progressively(self):
        first = cycle.apply_rate_limit_backoff({}, {"status": "shop_failed"}, now_ts=1000)
        self.assertEqual((first["rate_limit_failures"], first["retry_not_before_ts"]), (1, 1120))
        second = cycle.apply_rate_limit_backoff(first, {"status": "no_rod"}, now_ts=2000)
        self.assertEqual((second["rate_limit_failures"], second["retry_not_before_ts"]), (2, 2300))
        self.assertEqual([], cycle.eligible_accounts(["tg1"], {"tg1": second}, now_ts=2100))
        settled = cycle.apply_rate_limit_backoff(second, {"status": "settled"}, now_ts=3000)
        self.assertEqual(settled["rate_limit_failures"], 0)

    def test_coordinator_lock_prevents_manual_and_launchd_overlapping_writers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with cycle.coordinator_lock(root), patch.object(cycle, "DEFAULT_STATE_ROOT", root), \
                    patch.object(cycle, "_run_cycle") as run:
                self.assertEqual(0, cycle.main())
                run.assert_not_called()

    def test_yesterdays_quota_cannot_skip_today_but_future_server_backoff_survives(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "accounts/tg1/fishing.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"account": "tg1", "result": {
                "daily_cast_count": 5, "counted_settlement_ids": ["old"],
                "settlement_date": "2020-01-01", "retry_not_before_ts": 2000000000}}))
            result = cycle.account_result(root, "tg1")
            self.assertEqual(0, result["daily_cast_count"])
            self.assertEqual([], result["counted_settlement_ids"])
            self.assertEqual(2000000000, result["retry_not_before_ts"])

    def test_invalid_or_other_account_state_cannot_start_network_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "accounts/tg1/fishing.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"account": "tg2", "result": {}}))
            with self.assertRaises(ValueError):
                cycle.account_result(root, "tg1")
        for deadline in (float("nan"), float("inf"), -1, "invalid"):
            with self.subTest(deadline=deadline), self.assertRaises(ValueError):
                cycle.eligible_accounts(["tg1"], {"tg1": {"retry_not_before_ts": deadline}}, now_ts=1000)

    def test_one_invalid_account_file_skips_only_that_account(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            foreign = state / "accounts" / "tg1" / "fishing.json"
            foreign.parent.mkdir(parents=True)
            foreign.write_text(json.dumps({"account": "tg2", "result": {}}), encoding="utf-8")
            run_all = AsyncMock(return_value=[("tg2", {"status": "waiting"})])
            with patch.object(cycle, "DEFAULT_STATE_ROOT", state),                  patch.object(cycle, "load_fishing_config", return_value={"enabled": True, "accounts": ["tg1", "tg2"]}),                  patch.object(cycle, "run_all", new=run_all),                  patch.object(cycle.time, "time", return_value=1000):
                self.assertEqual(0, cycle.main())
            self.assertEqual(["tg2"], run_all.await_args.args[0])
            self.assertEqual({"account": "tg2", "result": {}}, json.loads(foreign.read_text()))  # untouched
            result = json.loads((state / "fishing_coordinator.json").read_text())
            self.assertEqual((["tg1"], "running"), (result["invalid_accounts"], result["status"]))

    def test_rate_limit_uses_upstream_5_15_60_minute_backoff(self):
        now = 1000.0
        previous = {"daily_cast_count": 2, "settlement_date": "2026-09-14"}
        for failures, wait in ((0, 300), (1, 900), (2, 3600), (3, 3600)):
            prior = {**previous, "rate_limit_failures": failures}
            result = cycle.apply_rate_limit_backoff(
                prior,
                {"status": "failed", "error_code": "external_action_rate_limited",
                 "daily_cast_count": 2, "settlement_date": "2026-09-14"},
                now_ts=now,
            )
            self.assertEqual(failures + 1, result["rate_limit_failures"])
            self.assertEqual(now + wait, result["retry_not_before_ts"])

    def test_first_cast_waits_for_its_post_midnight_slot_but_later_casts_do_not(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        def ts(h, m):
            return datetime(2026, 9, 21, h, m, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        rows = {"tg9100000001": {"daily_cast_count": 0}, "tg9100000006": {"daily_cast_count": 0},
                "tg9100000002": {"daily_cast_count": 2}}
        accounts = ["tg9100000001", "tg9100000002", "tg9100000006"]
        # The gate opens at 00:00, so late in the day everyone with quota left is eligible.
        self.assertEqual(accounts, cycle.eligible_accounts(accounts, rows, now_ts=ts(23, 59)))
        # 00:05: fishing opens the day; demo_player_06 waits for its 00:10 offset.
        self.assertEqual(["tg9100000001", "tg9100000002"], cycle.eligible_accounts(accounts, rows, now_ts=ts(0, 5)))
        self.assertEqual(accounts, cycle.eligible_accounts(accounts, rows, now_ts=ts(0, 10)))
        self.assertTrue(cycle.admitted("tg9100000006", ts(23, 59)))

    def test_future_backoff_skips_network_but_keeps_other_account_eligible(self):
        rows = {
            "tg1": {"daily_cast_count": 1, "retry_not_before_ts": 1300},
            "tg2": {"daily_cast_count": 2, "retry_not_before_ts": 0},
            "tg3": {"daily_cast_count": 5},
        }
        self.assertEqual(
            ["tg2"], cycle.eligible_accounts(["tg1", "tg2", "tg3"], rows, now_ts=1000)
        )

    def test_success_resets_rate_limit_backoff_without_losing_count(self):
        result = cycle.apply_rate_limit_backoff(
            {"daily_cast_count": 2, "rate_limit_failures": 2, "retry_not_before_ts": 5000},
            {"status": "settled_reconciled", "daily_cast_count": 3},
            now_ts=1000,
        )
        self.assertEqual(3, result["daily_cast_count"])
        self.assertEqual(0, result["rate_limit_failures"])
        self.assertEqual(0, result["retry_not_before_ts"])

    def test_no_eligible_account_during_backoff_performs_zero_protocol_calls(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            config = root / "config.json"
            accounts = ["tg1", "tg2"]
            config.write_text(json.dumps({"enabled": True, "accounts": accounts}), encoding="utf-8")
            for account in accounts:
                path = state / "accounts" / account / "fishing.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"result": {
                    "daily_cast_count": 1, "retry_not_before_ts": 2000,
                    "rate_limit_failures": 1,
                }}), encoding="utf-8")
            protocol = AsyncMock()
            with patch.object(cycle, "DEFAULT_CONFIG_PATH", config), \
                 patch.object(cycle, "DEFAULT_STATE_ROOT", state), \
                 patch.object(cycle, "load_fishing_config", return_value={"enabled": True, "accounts": accounts}), \
                 patch.object(cycle, "UpstreamFishingProtocol", return_value=protocol), \
                 patch.object(cycle.time, "time", return_value=1000):
                self.assertEqual(0, cycle.main())
            protocol.run_account.assert_not_awaited()
            result = json.loads((state / "fishing_coordinator.json").read_text())
            self.assertEqual("rate_limited_wait", result["status"])
            self.assertEqual(2000, result["retry_not_before_ts"])

    def test_parallel_rate_limits_publish_the_earliest_retry_time(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            accounts = ["tg1", "tg2"]
            outcomes = [(account, {"status": "failed", "error_code": "external_action_rate_limited"})
                        for account in accounts]
            with patch.object(cycle, "DEFAULT_STATE_ROOT", state), \
                 patch.object(cycle, "load_fishing_config", return_value={"enabled": True, "accounts": accounts}), \
                 patch.object(cycle, "run_all", new=AsyncMock(return_value=outcomes)), \
                 patch.object(cycle.time, "time", return_value=1000):
                self.assertEqual(0, cycle.main())
            result = json.loads((state / "fishing_coordinator.json").read_text())
            self.assertEqual("rate_limited_wait", result["status"])
            self.assertEqual(1300, result["retry_not_before_ts"])
            self.assertEqual({"tg1": 0, "tg2": 0}, result["result"]["daily_cast_counts"])

    def test_launchagent_uses_project_venv_not_runtime_package_install(self):
        plist = plistlib.loads((Path(__file__).parent / "ai.hermes.xiuxian.miniapp-fishing.send.plist").read_bytes())
        arguments = plist["ProgramArguments"]
        self.assertEqual(
            "/Users/operator/.hermes/workspace/projects/xiuxian/.venv/bin/python3",
            arguments[0],
        )
        self.assertNotIn("uv", " ".join(arguments))
        self.assertNotIn("--with", arguments)


if __name__ == "__main__":
    unittest.main()
