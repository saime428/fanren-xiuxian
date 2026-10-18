import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from qingyuanzi_host import message_recent_enough


class EntryDeadlineTests(unittest.TestCase):
    def test_accepts_message_inside_60_second_admission_window(self):
        message = SimpleNamespace(date=datetime.now(timezone.utc) - timedelta(seconds=59))
        self.assertTrue(message_recent_enough(message))

    def test_rejects_message_after_official_60_second_admission_window(self):
        message = SimpleNamespace(date=datetime.now(timezone.utc) - timedelta(seconds=61))
        self.assertFalse(message_recent_enough(message))

    def test_rejects_future_message_beyond_small_clock_skew(self):
        message = SimpleNamespace(date=datetime.now(timezone.utc) + timedelta(seconds=31))
        self.assertFalse(message_recent_enough(message))


if __name__ == "__main__":
    unittest.main()
