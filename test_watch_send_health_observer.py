import json
import plistlib
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import watch_send_health as health

ROOT = Path(__file__).resolve().parent
ACCOUNT = "tg9100000002"  # registry: shendubiguan with an enabled return_observer kind


class ObserverWatchdogTests(unittest.TestCase):
    def run_watchdog(self, payload, *, pause_all=False):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "accounts" / ACCOUNT / "shendubiguan"
            state_dir.mkdir(parents=True)
            if payload is not None:
                (state_dir / "observer_health.json").write_text(json.dumps(payload), encoding="utf-8")
            if pause_all:
                (state_dir.parent / "pause_all").touch()
            with patch.object(health, "OBSERVER_MODE", True), \
                    patch.object(health, "STATE_DIR", state_dir), \
                    patch.object(health, "FLOW_SLUG", "shendubiguan"), \
                    patch.object(health, "LOG_PATH", state_dir / "watchdog.log"), \
                    patch.object(health, "MAX_AGE_SECONDS", 1800), \
                    patch.dict("os.environ", {"XIU_ACCOUNT_SLUG": ACCOUNT, "TG_FLOW_SLUG": "shendubiguan"}):
                code = health.main()
                log = (state_dir / "watchdog.log").read_text(encoding="utf-8")
        return code, log

    def observer(self, **changes):
        now = time.time()
        return {"account": ACCOUNT, "flow": "shendubiguan", "kind": "return_observer",
                "status": "healthy", "started_at_ts": now - 7200, "last_success_ts": now - 60, **changes}

    def test_fresh_verified_scan_is_ok(self):
        code, log = self.run_watchdog(self.observer())
        self.assertEqual(0, code)
        self.assertIn("] OK Observer verified scan", log)

    def test_stale_missing_or_misattributed_observer_is_bad(self):
        cases = {
            "stale": self.observer(last_success_ts=time.time() - 3600),
            "never_scanned": self.observer(last_success_ts=None),
            "other_account": self.observer(account="tg9100000003"),
            "missing": None,
        }
        for name, payload in cases.items():
            with self.subTest(name):
                code, log = self.run_watchdog(payload)
                self.assertEqual(1, code)
                self.assertIn("] FAIL Observer", log)

    def test_account_level_pause_skips_the_check(self):
        code, log = self.run_watchdog(None, pause_all=True)
        self.assertEqual(0, code)
        self.assertIn("Paused via", log)
        self.assertIn(f"{ACCOUNT}/pause_all", log)

    def test_registered_observer_watchdogs_use_observer_mode_on_an_interval(self):
        registry = json.loads((ROOT / "config/active_flows.json").read_text(encoding="utf-8"))
        labels = [h["label"] for h in registry["helpers"]
                  if h["label"].endswith(".return_observer.watchdog") and h.get("enabled", True)]
        self.assertEqual(9, len(labels))
        for label in labels:
            with self.subTest(label):
                plist = plistlib.loads((ROOT / "scripts" / f"{label}.plist").read_bytes())
                self.assertEqual(600, plist["StartInterval"])
                self.assertNotIn("KeepAlive", plist)
                script = re.search(r"\./scripts/(watch_\S+\.sh)", plist["ProgramArguments"][-1]).group(1)
                text = (ROOT / "scripts" / script).read_text(encoding="utf-8")
                self.assertIn("export TG_WATCHDOG_MODE='observer'", text)
                self.assertIn('export TG_FLOW_SLUG="${XIU_FLOW_SLUG}"', text)


if __name__ == "__main__":
    unittest.main()
