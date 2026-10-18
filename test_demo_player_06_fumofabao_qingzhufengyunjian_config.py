import json
import plistlib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ACCOUNT = "tg9100000006"
FLOW = "fumofabao_qingzhufengyunjian_shenlei"
COMMAND = ".抚摸法宝 青竹蜂云剑（神雷版）"


class demo_player_06FumoFabaoConfigTests(unittest.TestCase):
    def test_wrapper_is_account_scoped_and_uses_exact_command(self):
        wrapper = (ROOT / "scripts" / f"send_{FLOW}_{ACCOUNT}.sh").read_text(encoding="utf-8")
        self.assertIn("source ./config/accounts/tg9100000007.env", wrapper)
        self.assertIn("export XIU_ACCOUNT_SLUG='tg9100000006'", wrapper)
        self.assertIn(f"export XIU_FLOW_SLUG='{FLOW}'", wrapper)
        self.assertIn(f"export TG_MESSAGE='{COMMAND}'", wrapper)
        self.assertIn("export TG_MIN_REPEAT_SECONDS='7200'", wrapper)
        self.assertIn("export TG_DISABLE_IMMEDIATE_RESEND='1'", wrapper)
        self.assertIn("export TG_REQUIRE_SUCCESS_OR_COOLDOWN='1'", wrapper)
        self.assertIn("export TG_COOLDOWN_COUNTS_AS_SUCCESS='0'", wrapper)
        self.assertIn("没有这件拥有器灵的法宝|或者名字输入错误", wrapper)

    def test_launchagent_runs_every_two_hours(self):
        path = ROOT / "scripts" / f"ai.hermes.xiuxian.{ACCOUNT}.{FLOW}.send.plist"
        payload = plistlib.loads(path.read_bytes())
        self.assertEqual(f"ai.hermes.xiuxian.{ACCOUNT}.{FLOW}.send", payload["Label"])
        self.assertEqual(7200, payload["StartInterval"])
        self.assertFalse(payload["RunAtLoad"])

    def test_active_registry_contains_demo_player_06_flow(self):
        payload = json.loads((ROOT / "config" / "active_flows.json").read_text(encoding="utf-8"))
        rows = [row for row in payload["flows"] if row.get("account") == ACCOUNT and row.get("flow") == FLOW]
        self.assertEqual(1, len(rows))
        self.assertTrue(rows[0]["enabled"])
        self.assertIn("send", rows[0]["kinds"])


if __name__ == "__main__":
    unittest.main()
