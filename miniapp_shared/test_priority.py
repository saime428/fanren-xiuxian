import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from miniapp_shared.priority import STATUS, qyz_battle_window, yield_to_qyz

SH = ZoneInfo("Asia/Shanghai")


def ts(hour, minute, second=0):
    return datetime(2026, 9, 21, hour, minute, second, tzinfo=SH).timestamp()


class PriorityWindowTests(unittest.TestCase):
    def test_daily_clock_window_covers_verification_battle_and_report(self):
        self.assertFalse(qyz_battle_window(ts(13, 35, 59))["active"])
        self.assertTrue(qyz_battle_window(ts(13, 36))["active"])
        self.assertTrue(qyz_battle_window(ts(13, 45))["active"])
        self.assertTrue(qyz_battle_window(ts(13, 51, 59))["active"])
        self.assertFalse(qyz_battle_window(ts(13, 52))["active"])
        self.assertFalse(qyz_battle_window(ts(0, 5))["active"])

    def test_unscheduled_entry_evidence_extends_the_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = root / "qingyuanzi-new-source" / "state" / "world_boss_shared_entry.json"
            entry.parent.mkdir(parents=True)
            entry.write_text(json.dumps({"message_id": 1, "fingerprint": "a" * 64,
                                         "published_at": "2026-09-21T20:10:00"}))
            self.assertEqual("battle_entry_in_progress", qyz_battle_window(ts(20, 15), root)["reason"])
            self.assertFalse(qyz_battle_window(ts(20, 23), root)["active"])
            self.assertFalse(qyz_battle_window(ts(20, 5), root)["active"])
            entry.write_text("{not json")
            self.assertFalse(qyz_battle_window(ts(20, 15), root)["active"])

    def test_yield_result_names_flow_and_reason(self):
        self.assertIsNone(yield_to_qyz("tower_miniapp", ts(9, 0)))
        result = yield_to_qyz("tower_miniapp", ts(13, 40))
        self.assertEqual((STATUS, "tower_miniapp", "daily_battle_clock_window"),
                         (result["status"], result["flow"], result["reason"]))


if __name__ == "__main__":
    unittest.main()
