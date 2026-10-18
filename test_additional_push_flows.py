import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path

from nanlonghou_trade import TRADE_FLOW_COMMANDS


ROOT = Path(__file__).resolve().parent


class AdditionalPushFlowTests(unittest.TestCase):
    def test_auto_reply_wrappers_pin_trusted_sender_ids(self) -> None:
        scripts = [
            ROOT / "scripts/lib/run_jiaohuan.sh",
            ROOT / "scripts/lib/run_xuangu_zuoda.sh",
            ROOT / "scripts/run_jiyin_choice_account.sh",
            ROOT / "scripts/send_shenji_budao_tg9100000001.sh",
        ]
        for script in scripts:
            with self.subTest(script=script):
                self.assertIn("TG_TRIGGER_TRUSTED_SENDER_IDS", script.read_text(encoding="utf-8"))

    def test_jiaohuan_shared_runner_is_persistent_push(self) -> None:
        text = (ROOT / "scripts/lib/run_jiaohuan.sh").read_text(encoding="utf-8")
        self.assertIn("TG_AUTO_REPLY_PERSISTENT='1'", text)
        self.assertIn("TG_AUTO_REPLY_RECONCILE_SECONDS='10'", text)
        self.assertIn("TG_TRIGGER_MAX_EVENT_AGE_SECONDS='600'", text)
        self.assertIn('TG_LOG_IDLE="${TG_LOG_IDLE:-0}"', text)

    def test_jiaohuan_shared_runner_answers_with_the_flow_choice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / ".venv/bin/python3"
            fake.parent.mkdir(parents=True)
            fake.write_text('#!/bin/sh\nprintf "%s|%s|%s" "$TG_FLOW_SLUG" "$TG_AUTO_REPLY_MESSAGE" "$TG_TRIGGER_MARKER_TEXT"\n')
            fake.chmod(0o755)

            def run(flow):
                return subprocess.run(
                    ["bash", "-c", 'xiu_ensure_session_authorized() { :; }; source "$1"', "bash",
                     str(ROOT / "scripts/lib/run_jiaohuan.sh")],
                    cwd=tmp, env={**os.environ, "XIU_FLOW_SLUG": flow}, capture_output=True, text=True)

            for flow, choice in TRADE_FLOW_COMMANDS.items():
                with self.subTest(flow=flow):
                    self.assertEqual(f"{flow}|{choice}|南陇侯|回复本消息|{choice}", run(flow).stdout)
            self.assertEqual(64, run("jiaohuan_other").returncode)

    def test_shenji_budao_uses_push_with_sixty_second_recovery(self) -> None:
        text = (ROOT / "scripts/send_shenji_budao_tg9100000001.sh").read_text(encoding="utf-8")
        self.assertIn("TG_AUTO_REPLY_RECONCILE_SECONDS='60'", text)
        self.assertNotIn("TG_AUTO_REPLY_POLL_SECONDS", text)

    def test_additional_push_plists_are_keepalive_not_interval_jobs(self) -> None:
        labels = (
            "ai.hermes.xiuxian.tg9100000001.shenji_budao.send",
            "ai.hermes.xiuxian.tg9100000001.jiaohuan_gongfa.send",
        )
        for label in labels:
            payload = plistlib.loads((ROOT / "scripts" / f"{label}.plist").read_bytes())
            self.assertTrue(payload["RunAtLoad"])
            self.assertTrue(payload["KeepAlive"])
            self.assertNotIn("StartInterval", payload)


if __name__ == "__main__":
    unittest.main()
