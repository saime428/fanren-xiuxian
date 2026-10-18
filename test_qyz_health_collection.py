"""QYZ health is account evidence, not a process-only green indicator."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from scripts import xiuxian_healthcheck as h


class QyzHealthTests(unittest.TestCase):
    def test_official_damage_reduction_stays_bad_after_idle_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(h, "XIUXIAN_ROOT", Path(tmp)):
            root = Path(tmp) / "qingyuanzi-new-source/state/accounts/tg9100000001"
            root.mkdir(parents=True)
            health = {"account": "tg9100000001", "flow": "qyz_miniapp", "ts": time.time(), "pid": 123,
                      "execution_owner": "official_protocol", "runtime_version": h.QYZ_RUNTIME_VERSION,
                      "status": "idle_waiting_event", "monitor_active": True, "connected": True,
                      "monitored_chat_ids": list(h.QYZ_SOURCE_TOPICS), "identity_verified": True,
                      "membership_verified": True, "source_verified": True, "source_checked_at": time.time(),
                      "policy_checked_at": time.time(), "verification_required": False,
                      "verification_available": True, "blockers": []}
            # The pre-fix v16 journal called this settled_perfect and then idle.
            # Also cover an earlier penalized hit followed by a normal finish.
            for official in (
                {"finish": {"server_result": {"automation_risk": {"damageMultiplier": .18}}}},
                {"finish": {"server_result": {"automation_risk": {"damageMultiplier": 1}}},
                 "hits": [{"server_hit": {"automationDamageMultiplier": .18}}]},
            ):
                event = {"status": "settled_perfect", "mutation_started": True,
                         "protocol_result": {"reference_outcome": {"diagnostics": official}}}
                (root / "world_boss_state.json").write_text(json.dumps({
                    "world_boss_health": health, "world_boss_events": [event]}))
                result = h.classify_qyz("tg9100000001", "qyz_miniapp",
                                       "user 123 0 0 0 0 ?? S 00:00 0:00 python qingyuanzi_host.py", True)
                self.assertEqual(result.severity, "bad")
                self.assertEqual(result.note, "qyz_server_damage_reduced: min_multiplier=0.18")

    def test_retained_full_battle_diagnostics_do_not_make_health_unreadable(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(h, "XIUXIAN_ROOT", Path(tmp)):
            root = Path(tmp) / "qingyuanzi-new-source/state/accounts/tg9100000001"
            root.mkdir(parents=True)
            health = {"account": "tg9100000001", "flow": "qyz_miniapp", "ts": time.time(), "pid": 123,
                      "execution_owner": "official_protocol", "runtime_version": h.QYZ_RUNTIME_VERSION,
                      "status": "idle_waiting_event", "monitor_active": True, "connected": True,
                      "monitored_chat_ids": list(h.QYZ_SOURCE_TOPICS), "identity_verified": True,
                      "membership_verified": True, "source_verified": True, "source_checked_at": time.time(),
                      "policy_checked_at": time.time(), "verification_required": False,
                      "verification_available": True, "blockers": []}
            # The host retains 20 complete event journals. The health reader's
            # generic 1 MiB limit is too small for their per-window diagnostics.
            events = [{"status": "settled_perfect", "protocol_result": {"diagnostics": "x" * 65000}}
                      for _ in range(20)]
            path = root / "world_boss_state.json"
            path.write_text(json.dumps({"world_boss_health": health, "world_boss_events": events}))
            self.assertGreater(path.stat().st_size, h.MAX_STATE_JSON_BYTES)
            result = h.classify_qyz("tg9100000001", "qyz_miniapp",
                                   "user 123 0 0 0 0 ?? S 00:00 0:00 python qingyuanzi_host.py", True)
            self.assertEqual(result.severity, "ok", result.note)
            with path.open("ab") as stream:
                stream.write(b" " * (8 * 1024 * 1024))
            oversized = h.classify_qyz("tg9100000001", "qyz_miniapp",
                                      "user 123 0 0 0 0 ?? S 00:00 0:00 python qingyuanzi_host.py", True)
            self.assertEqual(oversized.severity, "bad")
            self.assertEqual(oversized.note, "qyz_health_missing_or_invalid")

    def test_idle_heartbeat_cannot_hide_an_unfinished_journal_even_before_a_newer_result(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(h, "XIUXIAN_ROOT", Path(tmp)):
            root = Path(tmp) / "qingyuanzi-new-source/state/accounts/tg9100000001"
            root.mkdir(parents=True)
            valid = {"account": "tg9100000001", "flow": "qyz_miniapp", "ts": time.time(), "pid": 123,
                     "execution_owner": "official_protocol", "runtime_version": h.QYZ_RUNTIME_VERSION,
                     "status": "idle_waiting_event", "monitor_active": True, "connected": True,
                     "monitored_chat_ids": list(h.QYZ_SOURCE_TOPICS),
                     "identity_verified": True, "membership_verified": True, "source_verified": True,
                     "source_checked_at": time.time(), "policy_checked_at": time.time(),
                     "verification_required": False, "verification_available": True, "blockers": []}
            def classify(records, **extra):
                (root / "world_boss_state.json").write_text(json.dumps({
                    "world_boss_health": {**valid, **extra}, "world_boss_events": records}))
                return h.classify_qyz("tg9100000001", "qyz_miniapp",
                                     "user 123 0 0 0 0 ?? S 00:00 0:00 python qingyuanzi_host.py", True)
            for phase in ("start_submitted", "begin_submitted", "hit_submitted", "hit_accepted", "finish_confirmed"):
                with self.subTest(phase=phase):
                    record = {"status": phase, "fingerprint": "a" * 64, "mutation_started": True}
                    self.assertEqual(classify([record]).note, "qyz_unfinished_mutation")
                    self.assertEqual(classify([record, {"status": "settled_perfect"}]).severity, "bad")
                    self.assertEqual(classify([record], status="battle_in_progress",
                                             event_fingerprint="a" * 64).severity, "ok")
                    self.assertEqual(classify([record], status="battle_in_progress",
                                             event_fingerprint="b" * 64).severity, "bad")
            self.assertEqual(classify([{"status": "settled_partial", "mutation_started": True}]).severity, "bad")
            self.assertEqual(classify([{"status": "settled_perfect", "mutation_started": True}]).severity, "ok")

    def test_expired_failed_events_stop_blocking_and_a_newer_official_board_is_a_missed_event(self):
        from datetime import datetime, timedelta
        from qyz_state import event_needs_reconciliation
        fresh = {"status": "runtime_error", "error": "boss_windows_invalid", "mutation_started": True,
                 "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        stale = {**fresh, "updated_at": (datetime.now() - timedelta(hours=10)).strftime("%Y-%m-%d %H:%M:%S")}
        self.assertTrue(event_needs_reconciliation(fresh))
        self.assertFalse(event_needs_reconciliation(stale))
        self.assertTrue(event_needs_reconciliation(stale, window_seconds=None))
        self.assertTrue(event_needs_reconciliation({**stale, "updated_at": "garbage"}))
        with tempfile.TemporaryDirectory() as tmp, patch.object(h, "XIUXIAN_ROOT", Path(tmp)):
            root = Path(tmp) / "qingyuanzi-new-source/state/accounts/tg9100000001"
            root.mkdir(parents=True)
            valid = {"account": "tg9100000001", "flow": "qyz_miniapp", "ts": time.time(), "pid": 123,
                     "execution_owner": "official_protocol", "runtime_version": h.QYZ_RUNTIME_VERSION,
                     "status": "idle_waiting_event", "monitor_active": True, "connected": True,
                     "monitored_chat_ids": list(h.QYZ_SOURCE_TOPICS),
                     "identity_verified": True, "membership_verified": True, "source_verified": True,
                     "source_checked_at": time.time(), "policy_checked_at": time.time(),
                     "verification_required": False, "verification_available": True, "blockers": []}
            def classify(records):
                (root / "world_boss_state.json").write_text(json.dumps({
                    "world_boss_health": valid, "world_boss_events": records}))
                return h.classify_qyz("tg9100000001", "qyz_miniapp",
                                     "user 123 0 0 0 0 ?? S 00:00 0:00 python qingyuanzi_host.py", True)
            # Yesterday's failure is still a failed latest event, not pending work.
            self.assertEqual(classify([stale]).note, "qyz_latest_event_unresolved")
            self.assertEqual(classify([fresh]).note, "qyz_unfinished_mutation")
            settled = {"status": "settled_partial", "mutation_started": True,
                       "updated_at": (datetime.now() - timedelta(hours=11)).strftime("%Y-%m-%d %H:%M:%S")}
            board = Path(tmp) / "qingyuanzi-new-source/state/world_boss_shared_board.json"
            board.write_text(json.dumps({"message_id": 12436634, "rows": [{"rank": 1, "username": "x", "score": 1}],
                                         "published_at": (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")}))
            self.assertEqual(classify([settled]).note, "qyz_event_missed: board=12436634")
            same_event = {**settled, "updated_at": (datetime.now() - timedelta(minutes=63)).strftime("%Y-%m-%d %H:%M:%S")}
            self.assertEqual(classify([same_event]).severity, "bad")  # settled_partial stays bad
            self.assertNotIn("qyz_event_missed", classify([same_event]).note)
            board.write_text(json.dumps({"message_id": 1, "rows": [{"rank": 1}],
                                         "published_at": (datetime.now() - timedelta(days=2)).isoformat(timespec="seconds")}))
            self.assertNotIn("qyz_event_missed", classify([settled]).note)

    def test_slimmed_archived_records_keep_the_damage_multiplier_evidence(self):
        from qyz_state import slim_event_record, server_damage_multiplier_min
        record = {"message_id": 12421535, "status": "settled_perfect", "mutation_started": True,
                  "receipts": [{"index": 1, "server_perfect": True}], "window_states": {"w": {"transitions": []}},
                  "missed_window_records": [],
                  "protocol_result": {"status": "settled_perfect", "accepted_hits": 16, "server_perfects": 16,
                                      "server_result": {"grade": "甲等"},
                                      "transport_diagnostics": [{"phase": "hit"}] * 200,
                                      "window_states": [{"index": 1}], "verification_requests": [{"status": "accepted"}],
                                      "reference_outcome": {"grade": "甲等", "hit_count": 16, "diagnostics": {
                                          "window_reveal": {"log": [{"x": 1}] * 40},
                                          "finish": {"request": {"path": "/finish"},
                                                     "server_result": {"automation_risk": {"damageMultiplier": 1.0}}},
                                          "hits": [{"timing_plans": [1, 2, 3], "server_hit": {"automationDamageMultiplier": .18, "damageYi": 5}}]}}}}
        self.assertTrue(slim_event_record(record))
        self.assertFalse(slim_event_record(record))
        self.assertEqual(server_damage_multiplier_min(record), .18)
        self.assertNotIn("window_states", record)
        self.assertNotIn("transport_diagnostics", record["protocol_result"])
        self.assertEqual(record["receipts"], [{"index": 1, "server_perfect": True}])
        self.assertEqual(record["protocol_result"]["reference_outcome"]["hit_count"], 16)
        self.assertLess(len(json.dumps(record)), 900)

    def test_missing_stale_blocked_or_wrong_owner_is_bad(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(h, "XIUXIAN_ROOT", Path(tmp)):
            root = Path(tmp) / "qingyuanzi-new-source/state/accounts/tg9100000001"
            root.mkdir(parents=True)
            path = root / "world_boss_state.json"
            def classify():
                return h.classify_kind("tg9100000001", "qyz_miniapp", "qyz_monitor",
                                       "user 123 0 0 0 0 ?? S 00:00 0:00 python qingyuanzi_host.py", True, {})
            self.assertEqual(classify().severity, "bad")
            valid = {"account": "tg9100000001", "flow": "qyz_miniapp", "ts": time.time(), "pid": 123,
                     "execution_owner": "official_protocol", "runtime_version": h.QYZ_RUNTIME_VERSION,
                     "status": "idle_waiting_event", "monitor_active": True, "connected": True,
                     "monitored_chat_ids": list(h.QYZ_SOURCE_TOPICS),
                     "identity_verified": True, "membership_verified": True, "source_verified": True,
                     "source_checked_at": time.time(), "policy_checked_at": time.time(),
                     "verification_required": False, "verification_available": True, "blockers": []}
            for extra in ({"ts": time.time()-500}, {"source_verified": False},
                          {"account": "tg999"}, {"execution_owner": "official_page"},
                          {"verification_required": True}, {"monitor_active": False},
                          {"status": "unknown_mutation_outcome"}, {"pid": 999},
                          {"policy_checked_at": time.time()-500}, {"connected": False}):
                with self.subTest(extra=extra):
                    path.write_text(json.dumps({"world_boss_health": {**valid, **extra}}))
                    self.assertEqual(classify().severity, "bad")
            path.write_text(json.dumps({"world_boss_health": valid}))
            self.assertEqual(classify().severity, "ok")
            path.write_text(json.dumps({"world_boss_health": {
                **valid, "verification_required": True, "verification_method": "reference_native_browser"}}))
            self.assertEqual(classify().severity, "ok")

    def test_latest_failed_event_surfaces_exact_sanitized_error(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(h, "XIUXIAN_ROOT", Path(tmp)):
            root = Path(tmp) / "qingyuanzi-new-source/state/accounts/tg9100000001"
            root.mkdir(parents=True)
            health = {
                "account": "tg9100000001", "flow": "qyz_miniapp", "ts": time.time(), "pid": 123,
                "execution_owner": "official_protocol", "runtime_version": h.QYZ_RUNTIME_VERSION,
                "status": "runtime_error", "monitor_active": True, "connected": True,
                "monitored_chat_ids": list(h.QYZ_SOURCE_TOPICS),
                "identity_verified": True, "membership_verified": True, "source_verified": True,
                "source_checked_at": time.time(), "policy_checked_at": time.time(),
                "verification_required": True, "verification_available": True,
                "verification_method": "reference_native_browser",
                "blockers": ["latest_battle_not_successful"],
            }
            event = {
                "status": "runtime_error", "fingerprint": "a" * 64,
                "error": "official_webview_origin_invalid", "mutation_started": False,
            }
            (root / "world_boss_state.json").write_text(json.dumps({
                "world_boss_health": health, "world_boss_events": [event]}))

            result = h.classify_qyz(
                "tg9100000001", "qyz_miniapp",
                "user 123 0 0 0 0 ?? S 00:00 0:00 python qingyuanzi_host.py", True,
            )

            self.assertEqual(result.severity, "bad")
            self.assertEqual(result.note, "qyz_latest_event_error=official_webview_origin_invalid")

    def test_malformed_latest_event_status_never_crashes_health_collection(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(h, "XIUXIAN_ROOT", Path(tmp)):
            root = Path(tmp) / "qingyuanzi-new-source/state/accounts/tg9100000001"
            root.mkdir(parents=True)
            health = {
                "account": "tg9100000001", "flow": "qyz_miniapp", "ts": time.time(), "pid": 123,
                "execution_owner": "official_protocol", "runtime_version": h.QYZ_RUNTIME_VERSION,
                "status": "runtime_error", "monitor_active": True, "connected": True,
                "monitored_chat_ids": list(h.QYZ_SOURCE_TOPICS),
                "identity_verified": True, "membership_verified": True, "source_verified": True,
                "source_checked_at": time.time(), "policy_checked_at": time.time(),
                "verification_required": False, "verification_available": True,
                "blockers": ["latest_battle_not_successful"],
            }
            for malformed in ([], {}, None, "x" * 200):
                for health_extra in ({}, {"status":"idle_waiting_event", "blockers":[]}):
                    with self.subTest(status=malformed, health_extra=health_extra):
                        (root / "world_boss_state.json").write_text(json.dumps({
                            "world_boss_health": {**health, **health_extra},
                            "world_boss_events": [{"status": malformed, "error": "safe_error"}],
                        }))
                        result = h.classify_qyz(
                            "tg9100000001", "qyz_miniapp",
                            "user 123 0 0 0 0 ?? S 00:00 0:00 python qingyuanzi_host.py", True,
                        )
                        self.assertEqual(result.severity, "bad")
                        self.assertEqual(result.note, "qyz_event_journal_invalid")

    def test_perfect_settlement_needs_consistent_server_evidence(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(h, "XIUXIAN_ROOT", Path(tmp)):
            root = Path(tmp) / "qingyuanzi-new-source/state/accounts/tg9100000001"
            root.mkdir(parents=True)
            value = {"account": "tg9100000001", "flow": "qyz_miniapp", "ts": time.time(), "pid": 123,
                     "execution_owner": "official_protocol", "runtime_version": h.QYZ_RUNTIME_VERSION,
                     "status": "settled_perfect", "monitor_active": True, "connected": True,
                     "monitored_chat_ids": list(h.QYZ_SOURCE_TOPICS),
                     "identity_verified": True, "membership_verified": True, "source_verified": True,
                     "source_checked_at": time.time(), "policy_checked_at": time.time(),
                     "verification_required": False, "verification_available": True,
                     "finish_confirmed": True, "expected_windows": 16, "accepted_hits": 16, "server_perfects": 16,
                     "missed_windows": 0, "local_missed_windows": 0, "local_dead": False}
            def classify(extra):
                (root / "world_boss_state.json").write_text(json.dumps({"world_boss_health": {**value, **extra}}))
                return h.classify_kind("tg9100000001", "qyz_miniapp", "qyz_monitor",
                                       "user 123 0 0 0 0 ?? S 00:00 0:00 python qingyuanzi_host.py", True, {})
            self.assertEqual(classify({}).severity, "ok")
            for extra in ({"accepted_hits": 15, "server_perfects": 15}, {"missed_windows": 1},
                          {"local_dead": True}, {"finish_confirmed": False},
                          {"local_dead": None}, {"missed_windows": None},
                          {"local_missed_windows": 1}, {"local_missed_windows": None}):
                with self.subTest(extra=extra):
                    self.assertEqual(classify(extra).severity, "bad")


if __name__ == "__main__":
    unittest.main()
