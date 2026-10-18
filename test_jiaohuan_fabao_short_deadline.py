import json
import plistlib
import unittest
from pathlib import Path

from auto_reply_topic_event import normalized_event_age_seconds


ROOT = Path(__file__).resolve().parent
REGISTRY = ROOT / "config" / "active_flows.json"


class JiaohuanFabaoShortDeadlineTests(unittest.TestCase):
    def enabled_accounts(self) -> list[str]:
        payload = json.loads(REGISTRY.read_text(encoding="utf-8"))
        return sorted(
            item["account"]
            for item in payload["flows"]
            if item.get("enabled") and item.get("flow") == "jiaohuan_fabao" and "send" in item.get("kinds", []) and item.get("account") != "tg9100000001"
        )

    def test_event_age_guard_accepts_ten_minute_nanlonghou_deadline(self) -> None:
        self.assertEqual(normalized_event_age_seconds("600"), 600)
        self.assertEqual(normalized_event_age_seconds("0"), 1)

    def test_demo_player_01_is_excluded_from_nanlonghou_event(self) -> None:
        payload = json.loads(REGISTRY.read_text(encoding="utf-8"))
        self.assertFalse(
            any(
                item.get("enabled")
                and item.get("flow") == "jiaohuan_fabao"
                and item.get("account") == "tg9100000001"
                for item in payload["flows"]
            )
        )
        self.assertFalse((ROOT / "scripts" / "send_jiaohuan_fabao_tg9100000001.sh").exists())
        self.assertFalse(
            (ROOT / "scripts" / "ai.hermes.xiuxian.tg9100000001.jiaohuan_fabao.send.plist").exists()
        )

    def test_demo_player_06_uses_credential_source_but_keeps_business_identity(self) -> None:
        wrapper = (ROOT / "scripts" / "send_jiaohuan_fabao_tg9100000006.sh").read_text(encoding="utf-8")
        self.assertIn("source ./config/accounts/tg9100000007.env", wrapper)
        self.assertIn("XIU_ACCOUNT_SLUG='tg9100000006'", wrapper)
        self.assertIn("TG_SESSION_FALLBACK_NAME='state/tg9100000007'", wrapper)
        self.assertIn("TG_TRIGGER_TARGET_USERNAME='demo_player_06'", wrapper)
        self.assertIn("TG_AUTO_REPLY_MESSAGE='.交换 法宝'", wrapper)
        self.assertIn("TG_AUTO_REPLY_PERSISTENT=1", wrapper)
        self.assertIn("TG_AUTO_REPLY_RECONCILE_SECONDS=10", wrapper)
        self.assertIn("TG_LOG_SKIPS=0", wrapper)
        self.assertIn("TG_LOG_IDLE=0", wrapper)
        self.assertIn("TG_TRIGGER_MAX_EVENT_AGE_SECONDS=600", wrapper)

    def test_all_enabled_accounts_use_persistent_account_bound_listener(self) -> None:
        accounts = self.enabled_accounts()
        self.assertGreater(len(accounts), 0)
        for account in accounts:
            wrapper = (ROOT / "scripts" / f"send_jiaohuan_fabao_{account}.sh").read_text(encoding="utf-8")
            self.assertIn("TG_AUTO_REPLY_PERSISTENT", wrapper, account)
            self.assertIn("TG_AUTO_REPLY_RECONCILE_SECONDS=10", wrapper, account)
            self.assertIn("TG_LOG_SKIPS=0", wrapper, account)
            self.assertIn("TG_LOG_IDLE=0", wrapper, account)
            self.assertIn("TG_TRIGGER_MAX_EVENT_AGE_SECONDS=600", wrapper, account)

            plist_path = ROOT / "scripts" / f"ai.hermes.xiuxian.{account}.jiaohuan_fabao.send.plist"
            with plist_path.open("rb") as handle:
                plist = plistlib.load(handle)
            self.assertTrue(plist.get("KeepAlive"), account)
            self.assertTrue(plist.get("RunAtLoad"), account)
            self.assertNotIn("StartInterval", plist, account)
            self.assertLessEqual(int(plist.get("ThrottleInterval", 99)), 5, account)


if __name__ == "__main__":
    unittest.main()
