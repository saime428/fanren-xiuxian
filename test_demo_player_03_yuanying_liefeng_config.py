import json
import plistlib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ACCOUNT = "tg9100000003"
USERNAME = "demo_player_03"


class demo_player_03YuanyingLiefengConfigTests(unittest.TestCase):
    def test_active_registry_contains_both_enabled_flows(self):
        payload = json.loads((ROOT / "config/active_flows.json").read_text(encoding="utf-8"))
        found = {
            entry["flow"]: entry
            for entry in payload["flows"]
            if entry.get("account") == ACCOUNT and entry.get("flow") in {"yuanyingchuqiao", "tanxunliefeng"}
        }
        self.assertEqual({"yuanyingchuqiao", "tanxunliefeng"}, set(found))
        self.assertEqual(["send", "watchdog", "return_observer"], found["yuanyingchuqiao"]["kinds"])
        self.assertEqual(["send", "watchdog"], found["tanxunliefeng"]["kinds"])
        self.assertTrue(all(entry.get("enabled") is True for entry in found.values()))

    def test_wrappers_are_account_and_username_isolated(self):
        yuanying = (ROOT / f"scripts/send_yuanyingchuqiao_{ACCOUNT}.sh").read_text(encoding="utf-8")
        observer = (ROOT / f"scripts/run_yuanyingchuqiao_return_observer_{ACCOUNT}.sh").read_text(encoding="utf-8")
        liefeng = (ROOT / f"scripts/send_tanxunliefeng_{ACCOUNT}.sh").read_text(encoding="utf-8")
        for text in (yuanying, observer, liefeng):
            self.assertIn(f"config/accounts/{ACCOUNT}.env", text)
        self.assertIn(f"TG_TRIGGER_TARGET_USERNAME='{USERNAME}'", observer)
        self.assertNotIn("tg9100000002", yuanying + observer + liefeng)
        self.assertNotIn("demo_player_02", observer)

    def test_all_six_plists_point_to_account_specific_state(self):
        labels = (
            "yuanyingchuqiao.send",
            "yuanyingchuqiao.watchdog",
            "yuanyingchuqiao.return_observer",
            "yuanyingchuqiao.return_observer.watchdog",
            "tanxunliefeng.send",
            "tanxunliefeng.watchdog",
        )
        for suffix in labels:
            path = ROOT / "scripts" / f"ai.hermes.xiuxian.{ACCOUNT}.{suffix}.plist"
            payload = plistlib.loads(path.read_bytes())
            self.assertEqual(f"ai.hermes.xiuxian.{ACCOUNT}.{suffix}", payload["Label"])
            command = payload["ProgramArguments"][-1]
            self.assertIn(ACCOUNT, command)
            self.assertNotIn("tg9100000002", command)


if __name__ == "__main__":
    unittest.main()
