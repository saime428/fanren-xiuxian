import json
import unittest
from pathlib import Path


ROOT = Path(__file__).parent
ACCOUNT = "tg9100000002"


class demo_player_02YuanyingLiefengConfigTests(unittest.TestCase):
    def test_active_registry_contains_both_enabled_flows(self):
        payload = json.loads((ROOT / "config/active_flows.json").read_text(encoding="utf-8"))
        entries = {(item["account"], item["flow"]): item for item in payload["flows"]}
        for flow in ("yuanyingchuqiao", "tanxunliefeng"):
            item = entries[(ACCOUNT, flow)]
            self.assertTrue(item["enabled"])
            self.assertIn("send", item["kinds"])
            self.assertIn("watchdog", item["kinds"])

    def test_account_wrappers_use_isolated_account_and_username(self):
        yuanying = (ROOT / f"scripts/send_yuanyingchuqiao_{ACCOUNT}.sh").read_text(encoding="utf-8")
        liefeng = (ROOT / f"scripts/send_tanxunliefeng_{ACCOUNT}.sh").read_text(encoding="utf-8")
        self.assertIn(f"config/accounts/{ACCOUNT}.env", yuanying)
        self.assertIn(f"config/accounts/{ACCOUNT}.env", liefeng)
        observer = (ROOT / f"scripts/run_yuanyingchuqiao_return_observer_{ACCOUNT}.sh").read_text(encoding="utf-8")
        self.assertIn("TG_TRIGGER_TARGET_USERNAME='demo_player_02'", observer)

    def test_plists_point_to_account_specific_wrappers_and_state(self):
        for flow in ("yuanyingchuqiao", "tanxunliefeng"):
            for kind in ("send", "watchdog"):
                path = ROOT / f"scripts/ai.hermes.xiuxian.{ACCOUNT}.{flow}.{kind}.plist"
                text = path.read_text(encoding="utf-8")
                self.assertIn(f"{ACCOUNT}.{flow}.{kind}", text)
                self.assertIn(f"state/accounts/{ACCOUNT}/{flow}", text)


if __name__ == "__main__":
    unittest.main()
