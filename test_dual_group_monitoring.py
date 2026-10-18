import subprocess
import tempfile
import unittest
from pathlib import Path

from telegram_monitor_targets import parse_monitor_targets


class DualGroupMonitoringTests(unittest.TestCase):
    def _helper_env(self, game_env=None, account_env=""):
        """TG_TARGET, TG_TOPIC_ID and TG_MONITOR_TARGETS after sourcing the shared helper."""
        lib = Path("scripts/lib/account_flow_paths.sh").resolve()
        command = "\n".join((account_env, "export XIU_ACCOUNT_SLUG=tg9100000002 XIU_FLOW_SLUG=route_test",
                             'source "$1"', r'printf "%s\n" "$TG_TARGET" "$TG_TOPIC_ID" "$TG_MONITOR_TARGETS"'))
        with tempfile.TemporaryDirectory() as tmp:
            if game_env is not None:
                (Path(tmp) / "config/accounts").mkdir(parents=True)
                (Path(tmp) / "config/accounts/game.env").write_text(game_env, encoding="utf-8")
            result = subprocess.run(["/bin/bash", "-c", command, "bash", str(lib)], cwd=tmp,
                                    check=True, capture_output=True, text=True)
        return result.stdout.split()

    def test_all_account_flows_pin_outbound_commands_to_canonical_topic(self):
        placeholder = ["@example_game_group", "7310786", "@example_game_group|-1009400000005"]
        self.assertEqual(placeholder, self._helper_env())
        # An account env file cannot redirect commands; only game.env names the group.
        self.assertEqual(placeholder, self._helper_env(account_env="export XIU_GAME_TARGET=@elsewhere"))
        self.assertEqual(["@realgroup", "42", "@realgroup|-1001"], self._helper_env(
            "XIU_GAME_TARGET='@realgroup'\nXIU_GAME_TOPIC_ID='42'\nXIU_GAME_SECOND_CHAT_ID='-1001'\n"))

    def test_event_replies_send_to_canonical_target_not_source_chat(self):
        source = Path("question_reply_monitor.py").read_text(encoding="utf-8")
        self.assertIn("self.outbound = self.source_by_chat[CANONICAL_CHAT_ID][1]", source)
        self.assertIn("self.client.send_message(self.outbound", source)
        self.assertIn("def fixed_choice_readonly_source", source)

    def test_parse_monitor_targets_preserves_primary_and_numeric_secondary(self):
        expected = ["@example_game_group", -1009400000005]
        self.assertEqual(expected, parse_monitor_targets("@example_game_group|-1009400000005"))

    def test_all_account_flows_inherit_two_monitor_targets(self):
        self.assertEqual("@example_game_group|-1009400000005", self._helper_env()[2])

    def test_root_monitor_group_disables_forum_topic_filter(self):
        # The auto_reply one-shot scanner that carried the same rule was unreferenced and removed.
        observer_text = Path("observe_return.py").read_text(encoding="utf-8")
        self.assertIn("normalized_topic_id=None if source_is_root else normalized_topic_id", observer_text)

    def test_dual_group_listeners_keep_source_chat_as_detection_evidence_only(self):
        auto_text = Path("question_reply_monitor.py").read_text(encoding="utf-8")
        observer_text = Path("observe_return.py").read_text(encoding="utf-8")
        self.assertIn('"source_chat": str(source_target)', auto_text)
        self.assertIn("self.client.send_message(self.outbound", auto_text)
        self.assertNotIn("client.send_message(entity=source_entity", auto_text)
        self.assertIn('"source_chat": str(newest_event["source_chat"])', observer_text)
        self.assertIn("for source_target in source_targets", observer_text)


if __name__ == "__main__":
    unittest.main()
