import unittest
from pathlib import Path


class AutoReplyTopicEventSelectionTests(unittest.TestCase):
    def test_shenji_budao_wrapper_keeps_disaster_events_long_enough_for_recovery(self) -> None:
        from pathlib import Path
        text = Path("scripts/send_shenji_budao_tg9100000001.sh").read_text(encoding="utf-8")
        self.assertIn("export TG_SCAN_LIMIT='1000'", text)
        self.assertIn("export TG_TRIGGER_MAX_EVENT_AGE_SECONDS='900'", text)
        self.assertIn("export TG_TOPIC_MATCH_MODE='any'", text)

    def test_xuangu_runner_includes_real_correct_answer_and_event_shape(self) -> None:
        text = Path("scripts/lib/run_xuangu_zuoda.sh").read_text(encoding="utf-8")
        self.assertIn("元婴后", text)
        self.assertIn("乾蓝冰焰是何时", text)
        self.assertIn("300秒", text)

    def test_demo_player_06_wrapper_uses_alias_session_but_business_account_state(self) -> None:
        text = Path("scripts/send_xuangu_zuoda_tg9100000006.sh").read_text(encoding="utf-8")
        self.assertIn("config/accounts/tg9100000007.env", text)
        self.assertIn("XIU_ACCOUNT_SLUG='tg9100000006'", text)
        self.assertIn("TG_TRIGGER_TARGET_USERNAME='demo_player_06'", text)

    def test_retired_credential_slug_has_no_xuangu_business_listener(self) -> None:
        self.assertFalse(Path("scripts/send_xuangu_zuoda_tg9100000007.sh").exists())
        self.assertFalse(Path("scripts/ai.hermes.xiuxian.tg9100000007.xuangu_zuoda.send.plist").exists())

    def test_all_current_six_xuangu_listeners_are_persistent_and_fast(self) -> None:
        runner = Path("scripts/lib/run_xuangu_zuoda.sh").read_text(encoding="utf-8")
        self.assertIn("TG_AUTO_REPLY_PERSISTENT", runner)
        self.assertIn("TG_AUTO_REPLY_RECONCILE_SECONDS", runner)
        self.assertNotIn("TG_AUTO_REPLY_POLL_SECONDS", runner)
        self.assertIn("TG_LOG_SKIPS", runner)
        accounts = (
            "tg9100000001",
            "tg9100000005",
            "tg9100000006",
            "tg9100000004",
            "tg9100000002",
            "tg9100000003",
        )
        for account in accounts:
            plist = Path(f"scripts/ai.hermes.xiuxian.{account}.xuangu_zuoda.send.plist").read_text(encoding="utf-8")
            self.assertIn("<key>KeepAlive</key>", plist, account)
            self.assertNotIn("<key>StartInterval</key>", plist, account)

    def test_xuangu_registry_contains_exactly_current_six_accounts(self) -> None:
        import json
        registry = json.loads(Path("config/active_flows.json").read_text(encoding="utf-8"))
        actual = {
            item["account"]
            for item in registry["flows"]
            if item.get("flow") == "xuangu_zuoda"
        }
        self.assertEqual(
            {
                "tg9100000001",
                "tg9100000005",
                "tg9100000006",
                "tg9100000004",
                "tg9100000002",
                "tg9100000003",
            },
            actual,
        )


if __name__ == "__main__":
    unittest.main()
