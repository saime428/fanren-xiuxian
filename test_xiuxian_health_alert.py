from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).parent / "scripts" / "xiuxian_health_alert.py"
SPEC = importlib.util.spec_from_file_location("xiuxian_health_alert_under_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
alert = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = alert
SPEC.loader.exec_module(alert)


def snapshot(*items):
    counts = {"ok": 0, "warn": 0, "bad": 0}
    for item in items:
        counts[item["severity"]] += 1
    return {
        "summary": {"overall_status": "degraded" if counts["bad"] else "warning", "counts": counts},
        "active_items": list(items),
    }


class XiuxianHealthAlertTests(unittest.TestCase):
    def run_notify(self, payload, state_path, confirmation=None):
        snapshot_path = state_path.parent / "snapshot.json"
        snapshot_path.write_text(json.dumps(payload), encoding="utf-8")
        confirmation_path = None
        if confirmation is not None:
            confirmation_path = state_path.parent / "confirmation.json"
            confirmation_path.write_text(json.dumps(confirmation), encoding="utf-8")
        return alert.notify(snapshot_path, state_path, confirmation_path)

    def test_a_new_bad_requires_the_same_evidence_in_both_observations(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "notification.json"
            bad = snapshot({"account": "tg1", "flow": "flow1", "kind": "send", "severity": "bad",
                            "status": "crashed", "note": "error A"})
            changed = snapshot({"account": "tg1", "flow": "flow1", "kind": "send", "severity": "bad",
                                "status": "crashed", "note": "error B"})
            self.assertEqual(self.run_notify(bad, state, snapshot()), "")
            self.assertEqual(alert.load_previous(state), set())
            self.assertEqual(self.run_notify(changed, state, bad), "")
            self.assertEqual(alert.load_previous(state), set())
            self.assertIn("error B", self.run_notify(changed, state, changed))
            self.assertEqual(self.run_notify(changed, state, changed), "")

    def test_one_clean_observation_does_not_rearm_a_flapping_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "notification.json"
            bad = snapshot({"account": "tg1", "flow": "flow1", "kind": "send", "severity": "bad", "status": "crashed"})
            healthy = snapshot({"account": "tg1", "flow": "flow1", "kind": "send", "severity": "ok", "status": "running"})
            self.assertTrue(self.run_notify(bad, state, bad))
            for first, second in ((bad, healthy), (healthy, bad), (bad, bad)):
                self.assertEqual(self.run_notify(second, state, first), "")
                self.assertTrue(alert.load_previous(state))
            self.assertEqual(self.run_notify(healthy, state, healthy), "")
            self.assertEqual(alert.load_previous(state), set())
            self.assertTrue(self.run_notify(bad, state, bad))

    def test_running_return_observer_and_warning_are_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "notification.json"
            message = self.run_notify(
                snapshot(
                    {"account": "tg9100000002", "flow": "shendubiguan", "kind": "return_observer", "severity": "ok", "status": "running"},
                    {"account": "tg1", "flow": "flow1", "kind": "send", "severity": "warn", "status": "success"},
                ),
                state,
            )
            self.assertEqual(message, "")

    def test_nonbad_business_outcomes_are_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "notification.json"
            for status in ("acknowledged", "countdown_pending", "failed", "settled_zero_contribution"):
                with self.subTest(status=status):
                    message = self.run_notify(
                        snapshot({"account": "tg1", "flow": "flow1", "kind": "send", "severity": "warn", "status": status}),
                        state,
                    )
                    self.assertEqual(message, "")

    def test_bad_idle_journal_and_cooldown_errors_are_actionable(self):
        for item in (
            {"flow": "qyz_miniapp", "kind": "qyz_monitor", "status": "idle_waiting_event", "note": "qyz_unfinished_mutation"},
            {"flow": "flow1", "kind": "send", "status": "countdown_pending", "note": "Permission denied", "cooldown_until_ts": 1999999999},
            {"flow": "flow1", "kind": "send", "status": "acknowledged", "note": "Traceback: current error"},
        ):
            with self.subTest(item=item):
                payload = snapshot({"account": "tg1", "severity": "bad", **item})
                self.assertEqual(len(alert.actionable_items(payload)), 1)

    def test_shared_qyz_failure_does_not_hide_the_seventh_distinct_issue(self):
        rows = [{"account": f"tg{i}", "flow": "qyz_miniapp", "kind": "qyz_monitor", "severity": "bad",
                 "status": "blocked_source_unavailable", "note": "qyz_source_or_identity_unverified"} for i in range(6)]
        rows.append({"account": "tg6", "flow": "another_flow", "kind": "send", "severity": "bad", "status": "crashed", "note": "Permission denied"})
        message = alert.build_alert(snapshot(*rows), rows)
        self.assertEqual(message.count("/qyz_miniapp"), 1)
        self.assertIn("tg5", message)
        self.assertIn("tg6/another_flow", message)
        self.assertIn("Permission denied", message)

    def test_invalid_registry_alerts_even_without_any_per_flow_rows(self):
        payload = {'active_items': [], 'summary': {'overall_status': 'degraded', 'counts': {'ok': 0, 'warn': 0, 'bad': 1},
                   'top_issues': [{'kind': 'active_config', 'severity': 'bad', 'note': 'helpers must be a list'}]}}
        items = alert.actionable_items(payload)
        self.assertEqual(len(items), 1)
        self.assertIn('system/active_config', alert.build_alert(payload, items))

    def test_real_wrapper_clears_dedupe_on_a_healthy_cycle(self):
        # Run the real wrapper/notifier under a minimal cron PATH, using an
        # isolated project interpreter and a collector fixture. No delivery.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scripts = root / 'scripts'
            scripts.mkdir()
            for name in ('xiuxian_active_healthcheck.sh', 'xiuxian_health_alert.py'):
                shutil.copyfile(MODULE_PATH.with_name(name), scripts / name)
            python = root / '.venv' / 'bin' / 'python'
            python.parent.mkdir(parents=True)
            python.symlink_to(sys.executable)
            (scripts / 'xiuxian_healthcheck.py').write_text(
                'import os, shutil\n'
                'from pathlib import Path\n'
                'root = Path(os.environ["XIUXIAN_ALERT_TEST_DIR"])\n'
                'shutil.copyfile(root / "input.json", os.environ["XIU_HEALTHCHECK_OUTPUT"])\n',
                encoding='utf-8')
            forbidden_python = root / 'python3'
            forbidden_python.write_text('#!/bin/sh\necho "unexpected PATH interpreter" >&2\nexit 99\n')
            forbidden_python.chmod(0o755)
            environment = {**os.environ, 'PATH': f'{root}:/usr/bin:/bin',
                           'XIUXIAN_ALERT_TEST_DIR': str(root),
                           'XIU_HEALTHCHECK_OUTPUT': str(root / 'snapshot.json'),
                           'XIU_HEALTHCHECK_NOTIFICATION_STATE': str(root / 'notification.json')}
            wrapper = scripts / 'xiuxian_active_healthcheck.sh'
            def run(payload):
                (root / 'input.json').write_text(json.dumps(payload), encoding='utf-8')
                return subprocess.run(['bash', str(wrapper)], env=environment, capture_output=True,
                                      text=True, timeout=15, check=True).stdout
            bad = snapshot({'account': 'tg1', 'flow': 'flow1', 'kind': 'send', 'severity': 'bad', 'status': 'crashed'})
            healthy = snapshot({'account': 'tg1', 'flow': 'flow1', 'kind': 'send', 'severity': 'ok', 'status': 'acknowledged'})
            self.assertTrue(run(bad).strip())
            self.assertEqual(run(bad), '')
            self.assertEqual(run(healthy), '')
            self.assertEqual(alert.load_previous(root / 'notification.json'), set())
            self.assertTrue(run(bad).strip())

    def test_one_genuine_new_bad_alerts_once_and_unchanged_bad_is_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "notification.json"
            payload = snapshot(
                {"account": "tg1", "flow": "flow1", "kind": "watchdog", "severity": "bad", "status": "not_authorized", "note": "Target session is not authorized"}
            )
            first = self.run_notify(payload, state)
            second = self.run_notify(payload, state)
            self.assertIn("tg1/flow1 [watchdog] = not_authorized", first)
            self.assertEqual(second, "")

    def test_recovery_clears_dedupe_so_a_later_regression_alerts(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "notification.json"
            bad = snapshot({"account": "tg1", "flow": "flow1", "kind": "send", "severity": "bad", "status": "crashed", "note": "Traceback"})
            healthy = snapshot({"account": "tg1", "flow": "flow1", "kind": "send", "severity": "ok", "status": "acknowledged"})
            self.assertTrue(self.run_notify(bad, state))
            self.assertEqual(self.run_notify(healthy, state), "")
            self.assertTrue(self.run_notify(bad, state))

    def test_changed_bad_evidence_is_newly_actionable(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "notification.json"
            first = snapshot({"account": "tg1", "flow": "flow1", "kind": "send", "severity": "bad", "status": "crashed", "note": "error A"})
            second = snapshot({"account": "tg1", "flow": "flow1", "kind": "send", "severity": "bad", "status": "crashed", "note": "error B"})
            self.assertTrue(self.run_notify(first, state))
            self.assertTrue(self.run_notify(second, state))


class FingerprintTests(unittest.TestCase):
    def test_fingerprint_ignores_the_log_timestamp_in_the_note(self):
        base = {"account": "tg1", "flow": "f", "kind": "send", "status": "bad"}
        first = alert.fingerprint({**base, "note": "[2026-10-15 20:00:00] Telegram timeout during send"})
        second = alert.fingerprint({**base, "note": "[2026-10-15 20:30:00] Telegram timeout during send"})
        self.assertEqual(first, second)
        self.assertNotEqual(first, alert.fingerprint({**base, "note": "[2026-10-15 20:30:00] different"}))

if __name__ == "__main__":
    unittest.main()
