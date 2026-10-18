import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import qingyuanzi_host as host


class AdmissionScanScheduleTests(unittest.TestCase):
    def test_only_one_account_performs_history_polling(self):
        scanners = [account for account in host.ACCOUNTS if host.account_scans_telegram_history(account)]
        self.assertEqual(scanners, [host.ENTRY_DISCOVERY_ACCOUNT])

    def test_fast_scan_starts_thirty_seconds_before_daily_one_minute_window(self):
        cases = [((13, 39, 29, 999999), 15), ((13, 39, 30, 0), 5),
                 ((13, 40, 0, 0), 5), ((13, 40, 59, 999999), 5),
                 ((13, 41, 0, 0), 15), ((21, 40, 0, 0), 15)]
        for clock, expected in cases:
            with self.subTest(clock=clock):
                self.assertEqual(host.admission_scan_interval_seconds(datetime(2026, 9, 12, *clock)), expected)

    def test_scan_schedule_uses_beijing_time_for_aware_inputs(self):
        fast = datetime(2026, 9, 12, 13, 39, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        closed = datetime(2026, 9, 12, 13, 41, tzinfo=ZoneInfo("Asia/Shanghai"))
        for tz in (timezone.utc, ZoneInfo("Asia/Shanghai"), ZoneInfo("America/Los_Angeles")):
            with self.subTest(timezone=tz):
                self.assertEqual(host.admission_scan_interval_seconds(fast.astimezone(tz)), 5)
                self.assertEqual(host.admission_scan_interval_seconds(closed.astimezone(tz)), 15)

    def test_random_openings_keep_history_recovery_at_every_other_hour(self):
        for hour in range(24):
            self.assertEqual(host.admission_scan_interval_seconds(datetime(2026, 9, 12, hour, 0)), 15)

    def test_followers_poll_local_shared_state_without_telegram_history_calls(self):
        followers = [account for account in host.ACCOUNTS if account != host.ENTRY_DISCOVERY_ACCOUNT]
        self.assertTrue(followers)
        self.assertTrue(all(not host.account_scans_telegram_history(account) for account in followers))
        self.assertEqual(host.SHARED_ENTRY_STATE_POLL_SECONDS, 0.25)


if __name__ == "__main__":
    unittest.main()
