from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).parent / "scripts" / "xiuxian_healthcheck.py"
SPEC = importlib.util.spec_from_file_location("xiuxian_healthcheck_under_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
healthcheck = importlib.util.module_from_spec(SPEC)
import sys
sys.modules[SPEC.name] = healthcheck
SPEC.loader.exec_module(healthcheck)


class BoundedTailTests(unittest.TestCase):
    def test_tail_lines_does_not_read_large_file_from_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "large.log"
            prefix = ("old-line\n" * 200_000).encode()
            suffix = "new-a\nnew-b\nnew-c\n".encode()
            path.write_bytes(prefix + suffix)

            original_open = Path.open
            bytes_read = 0

            class CountingReader:
                def __init__(self, wrapped):
                    self._wrapped = wrapped

                def __enter__(self):
                    self._wrapped.__enter__()
                    return self

                def __exit__(self, *args):
                    return self._wrapped.__exit__(*args)

                def __getattr__(self, name):
                    return getattr(self._wrapped, name)

                def read(self, size: int = -1):
                    nonlocal bytes_read
                    data = self._wrapped.read(size)
                    bytes_read += len(data)
                    return data

            def counting_open(target: Path, *args, **kwargs):
                wrapped = original_open(target, *args, **kwargs)
                if Path(target) == path and "b" in str(kwargs.get("mode", args[0] if args else "r")):
                    return CountingReader(wrapped)
                return wrapped

            original_read_text = Path.read_text

            def reject_full_read(target: Path, *args, **kwargs):
                if Path(target) == path:
                    raise AssertionError("tail_lines must not read the whole file")
                return original_read_text(target, *args, **kwargs)

            with (
                patch.object(Path, "open", counting_open),
                patch.object(Path, "read_text", reject_full_read),
            ):
                lines = healthcheck.tail_lines(path, 3)

            self.assertEqual(lines, ["new-a", "new-b", "new-c"])
            self.assertLess(bytes_read, 128 * 1024)

    def test_tail_lines_hard_limits_single_line_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "single-line.log"
            path.write_bytes(b"x" * (healthcheck.MAX_TAIL_BYTES * 4))
            original_open = Path.open
            bytes_read = 0

            class CountingReader:
                def __init__(self, wrapped):
                    self._wrapped = wrapped

                def __enter__(self):
                    self._wrapped.__enter__()
                    return self

                def __exit__(self, *args):
                    return self._wrapped.__exit__(*args)

                def __getattr__(self, name):
                    return getattr(self._wrapped, name)

                def read(self, size: int = -1):
                    nonlocal bytes_read
                    data = self._wrapped.read(size)
                    bytes_read += len(data)
                    return data

            def counting_open(target: Path, *args, **kwargs):
                wrapped = original_open(target, *args, **kwargs)
                if Path(target) == path:
                    return CountingReader(wrapped)
                return wrapped

            with patch.object(Path, "open", counting_open):
                lines = healthcheck.tail_lines(path, 80)

            self.assertEqual(len(lines), 1)
            self.assertLessEqual(bytes_read, healthcheck.MAX_TAIL_BYTES)


class RecentLogSignalTests(unittest.TestCase):
    def test_newer_healthy_log_overrides_older_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            flow_dir = Path(tmp)
            old = flow_dir / "old.log"
            new = flow_dir / "new.log"
            old.write_text("Traceback (most recent call last):\nboom\n", encoding="utf-8")
            new.write_text("No new return-summary event found for markers=['x']\n", encoding="utf-8")
            now = time.time()
            os.utime(old, (now - 120, now - 120))
            os.utime(new, (now, now))

            self.assertEqual(healthcheck.find_recent_log_signal(flow_dir), (None, None))

    def test_newer_traceback_is_bad(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            flow_dir = Path(tmp)
            old = flow_dir / "old.log"
            new = flow_dir / "new.log"
            old.write_text("No new return-summary event found for markers=['x']\n", encoding="utf-8")
            new.write_text("Traceback (most recent call last):\nboom\n", encoding="utf-8")
            now = time.time()
            os.utime(old, (now - 120, now - 120))
            os.utime(new, (now, now))

            severity, signal = healthcheck.find_recent_log_signal(flow_dir)
            self.assertEqual(severity, "bad")
            self.assertEqual(signal, "Traceback (most recent call last):")

    def test_expected_hard_wait_system_exit_does_not_promote_sent_no_ack_to_bad(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            flow_dir = Path(tmp)
            log = flow_dir / "send.launchd.log"
            log.write_text(
                "Task exception was never retrieved\n"
                "Traceback (most recent call last):\n"
                "SystemExit: 75\n",
                encoding="utf-8",
            )

            self.assertEqual(healthcheck.find_recent_log_signal(flow_dir, "send"), (None, None))

    def test_only_exact_sender_hard_wait_exit_is_tolerated(self) -> None:
        for kind, terminal in (("send", "SystemExit: 750"), ("send", "SystemExit: 1"),
                               ("return_observer", "SystemExit: 75"),
                               ("send", "ValueError: bad state containing SystemExit: 75")):
            with self.subTest(kind=kind, terminal=terminal), tempfile.TemporaryDirectory() as tmp:
                flow_dir = Path(tmp)
                name = "send.launchd.log" if kind == "send" else "observer.launchd.log"
                (flow_dir / name).write_text("Traceback (most recent call last):\n" + terminal + "\n")
                self.assertEqual(healthcheck.find_recent_log_signal(flow_dir, kind)[0], "bad")

    def test_sender_runtime_timeout_remains_an_operational_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            flow_dir = Path(tmp)
            (flow_dir / "send.launchd.log").write_text(
                "[2026-09-11 12:00:00] send_topic_message.py exceeded TG_RUN_MAX_SECONDS=180\n"
            )
            self.assertEqual(healthcheck.find_recent_log_signal(flow_dir, "send")[0], "bad")

    def test_later_good_line_in_same_log_overrides_transient_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            flow_dir = Path(tmp)
            log = flow_dir / "send.launchd.log"
            log.write_text(
                "Traceback (most recent call last):\n"
                "ConnectionResetError: connection reset by peer\n"
                "Recent same-flow activity detected (120s < 42900s guard); skip duplicate send\n",
                encoding="utf-8",
            )

            self.assertEqual(healthcheck.find_recent_log_signal(flow_dir, "send"), (None, None))

    def test_cooldown_skip_line_is_healthy_after_transient_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            flow_dir = Path(tmp)
            log = flow_dir / "send.launchd.log"
            log.write_text(
                "Traceback (most recent call last):\n"
                "ConnectionResetError: connection reset by peer\n"
                "Cooldown retry pending for @group|1|.问道; skip send for another 120s\n",
                encoding="utf-8",
            )

            self.assertEqual(healthcheck.find_recent_log_signal(flow_dir, "send"), (None, None))

    def test_latest_return_observer_idle_marker_is_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            flow_dir = Path(tmp)
            log = flow_dir / "send.launchd.log"
            log.write_text(
                "Traceback (most recent call last):\n"
                "IncompleteReadError: 0 bytes read\n"
                "[2026-09-04 20:41:54] shuangxiu_wenyang cooldown remaining=91s\n",
                encoding="utf-8",
            )

            self.assertEqual(healthcheck.find_recent_log_signal(flow_dir, "send"), (None, None))

    def test_latest_return_observer_idle_marker_is_healthy_after_transport_noise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            flow_dir = Path(tmp)
            old = flow_dir / "old.log"
            new = flow_dir / "observer.launchd.log"
            old.write_text("Traceback (most recent call last):\nboom\n", encoding="utf-8")
            new.write_text(
                "[2026-07-28 11:18:46] No new return-summary event found for markers=['深度闭关总结']\n",
                encoding="utf-8",
            )
            now = time.time()
            os.utime(old, (now - 120, now - 120))
            os.utime(new, (now, now))

            self.assertEqual(healthcheck.find_recent_log_signal(flow_dir), (None, None))

    def test_latest_watchdog_ok_overrides_older_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            flow_dir = Path(tmp)
            old = flow_dir / "old.log"
            new = flow_dir / "watchdog.log"
            old.write_text("Traceback (most recent call last):\nboom\n", encoding="utf-8")
            new.write_text(
                "[2026-07-28 11:16:24] OK Last successful send age=27714s status=started\n",
                encoding="utf-8",
            )
            now = time.time()
            os.utime(old, (now - 120, now - 120))
            os.utime(new, (now, now))

            self.assertEqual(healthcheck.find_recent_log_signal(flow_dir), (None, None))

    def test_latest_disabled_registry_skip_is_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            flow_dir = Path(tmp)
            old = flow_dir / "old.log"
            new = flow_dir / "observer.watchdog.launchd.log"
            old.write_text("Traceback (most recent call last):\nboom\n", encoding="utf-8")
            new.write_text(
                "Disabled flow registry skip: tg1/flow_observer not present in config/active_flows.json\n",
                encoding="utf-8",
            )
            now = time.time()
            os.utime(old, (now - 120, now - 120))
            os.utime(new, (now, now))

            self.assertEqual(healthcheck.find_recent_log_signal(flow_dir), (None, None))

    def test_latest_watchdog_fail_remains_bad(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            flow_dir = Path(tmp)
            path = flow_dir / "watchdog.log"
            path.write_text(
                "[2026-07-28 11:20:41] FAIL Last successful send is too old\n",
                encoding="utf-8",
            )

            severity, signal = healthcheck.find_recent_log_signal(flow_dir)
            self.assertEqual(severity, "bad")
            self.assertIn("FAIL", signal or "")

    def test_bad_marker_wins_when_same_line_also_contains_good_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            flow_dir = Path(tmp)
            path = flow_dir / "watchdog.log"
            path.write_text("[2026-07-28] OK previous; FAIL current\n", encoding="utf-8")

            severity, signal = healthcheck.find_recent_log_signal(flow_dir)
            self.assertEqual(severity, "bad")
            self.assertIn("FAIL", signal or "")

    def test_disappearing_log_during_stat_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            flow_dir = Path(tmp)
            path = flow_dir / "vanishing.log"
            path.write_text("Traceback\n", encoding="utf-8")
            original_stat = Path.stat

            def flaky_stat(target: Path, *args, **kwargs):
                if Path(target) == path:
                    raise FileNotFoundError(path)
                return original_stat(target, *args, **kwargs)

            with patch.object(Path, "stat", flaky_stat):
                self.assertEqual(healthcheck.find_recent_log_signal(flow_dir), (None, None))

    def test_watchdog_kind_reads_observer_watchdog_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            flow_dir = Path(tmp)
            (flow_dir / "observer.watchdog.launchd.log").write_text(
                "[2026-07-28] FAIL observer watchdog stopped\n",
                encoding="utf-8",
            )
            severity, signal = healthcheck.find_recent_log_signal(flow_dir, "watchdog")
            self.assertEqual(severity, "bad")
            self.assertIn("FAIL", signal or "")

    def test_watchdog_kind_reads_named_watchdog_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            flow_dir = Path(tmp)
            (flow_dir / "watchdog_zhaohuanmoying.log").write_text(
                "[2026-07-28] FAIL named watchdog stopped\n",
                encoding="utf-8",
            )
            severity, signal = healthcheck.find_recent_log_signal(flow_dir, "watchdog")
            self.assertEqual(severity, "bad")
            self.assertIn("FAIL", signal or "")

    def test_only_recent_log_files_are_considered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            flow_dir = Path(tmp)
            now = time.time()
            for index in range(12):
                path = flow_dir / f"run-{index:02d}.log"
                text = "Traceback (most recent call last):\n" if index == 0 else f"idle-{index}\n"
                path.write_text(text, encoding="utf-8")
                os.utime(path, (now + index, now + index))
            newest = flow_dir / "run-11.log"
            newest.write_text("No new return-summary event found for markers=['x']\n", encoding="utf-8")

            with patch.object(healthcheck, "tail_lines", wraps=healthcheck.tail_lines) as tail_mock:
                self.assertEqual(healthcheck.find_recent_log_signal(flow_dir), (None, None))

            self.assertLessEqual(tail_mock.call_count, healthcheck.MAX_LOG_FILES_PER_FLOW)


class LogRetentionApplyTests(unittest.TestCase):
    def test_live_rolling_log_is_trimmed_in_place_to_tail_limit(self) -> None:
        planner_path = Path(__file__).parent / "scripts" / "xiuxian_log_retention_apply.py"
        spec = importlib.util.spec_from_file_location("xiuxian_log_retention_apply_under_test", planner_path)
        assert spec is not None and spec.loader is not None
        retention = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = retention
        spec.loader.exec_module(retention)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state" / "accounts"
            path = root / "tg1" / "flow1" / "send.launchd.log"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"old-" * 80 + b"LATEST-EVIDENCE")
            inode_before = path.stat().st_ino
            result = retention.trim_rolling_logs(root, keep_bytes=32)
            self.assertEqual(1, result["trimmed_count"])
            self.assertEqual(inode_before, path.stat().st_ino)
            self.assertTrue(path.read_bytes().endswith(b"LATEST-EVIDENCE"))

            # A line appended while the tail is being copied must survive.
            path.write_bytes(b"old-" * 80 + b"LATEST-EVIDENCE")
            real_pwrite = os.pwrite
            appended: list[bool] = []

            def pwrite_with_concurrent_append(fd, data, offset):
                if not appended:
                    appended.append(True)
                    with path.open("ab") as writer:
                        writer.write(b"APPENDED")
                return real_pwrite(fd, data, offset)

            with patch.object(retention.os, "pwrite", side_effect=pwrite_with_concurrent_append):
                result = retention.trim_rolling_logs(root, keep_bytes=32)
            self.assertEqual(path.read_bytes()[-23:], b"LATEST-EVIDENCEAPPENDED")
            self.assertEqual(result["items"][0]["after_bytes"], 32 + len(b"APPENDED"))


class LogRetentionPlanningTests(unittest.TestCase):
    def test_large_rolling_log_is_planned_for_rotation(self) -> None:
        planner_path = Path(__file__).parent / "scripts" / "xiuxian_log_retention.py"
        spec = importlib.util.spec_from_file_location("xiuxian_log_retention_under_test", planner_path)
        assert spec is not None and spec.loader is not None
        planner = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = planner
        spec.loader.exec_module(planner)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "state" / "accounts" / "tg1" / "flow1" / "send.launchd.log"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"x" * 200)

            plan = planner.build_retention_plan(
                root / "state" / "accounts",
                rolling_limit_bytes=100,
                rolling_keep_bytes=25,
                per_run_keep_count=10,
            )

            self.assertEqual(len(plan["rolling_logs"]), 1)
            item = plan["rolling_logs"][0]
            self.assertEqual(item["path"], str(path))
            self.assertEqual(item["size_bytes"], 200)
            self.assertEqual(item["estimated_reclaim_bytes"], 175)

    def test_per_run_logs_keep_latest_count(self) -> None:
        planner_path = Path(__file__).parent / "scripts" / "xiuxian_log_retention.py"
        spec = importlib.util.spec_from_file_location("xiuxian_log_retention_under_test_2", planner_path)
        assert spec is not None and spec.loader is not None
        planner = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = planner
        spec.loader.exec_module(planner)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            flow_dir = root / "state" / "accounts" / "tg1" / "jiaru_zhandou"
            flow_dir.mkdir(parents=True)
            now = time.time()
            for index in range(15):
                path = flow_dir / f"all-run-20260728-{index:06d}.log"
                path.write_bytes(b"x" * 10)
                os.utime(path, (now + index, now + index))

            plan = planner.build_retention_plan(
                root / "state" / "accounts",
                rolling_limit_bytes=1000,
                rolling_keep_bytes=100,
                per_run_keep_count=10,
            )

            self.assertEqual(len(plan["per_run_logs"]), 5)
            deleted_names = {Path(item["path"]).name for item in plan["per_run_logs"]}
            self.assertEqual(deleted_names, {f"all-run-20260728-{index:06d}.log" for index in range(5)})

    def test_symlinks_are_excluded_from_plan(self) -> None:
        planner_path = Path(__file__).parent / "scripts" / "xiuxian_log_retention.py"
        spec = importlib.util.spec_from_file_location("xiuxian_log_retention_under_test_3", planner_path)
        assert spec is not None and spec.loader is not None
        planner = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = planner
        spec.loader.exec_module(planner)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / "state" / "accounts"
            flow_dir = state_root / "tg1" / "flow1"
            flow_dir.mkdir(parents=True)
            outside = root / "outside.log"
            outside.write_bytes(b"x" * 200)
            (flow_dir / "send.launchd.log").symlink_to(outside)

            plan = planner.build_retention_plan(
                state_root,
                rolling_limit_bytes=100,
                rolling_keep_bytes=25,
                per_run_keep_count=10,
            )

            self.assertEqual(plan["rolling_logs"], [])
            self.assertEqual(plan["per_run_logs"], [])
            self.assertEqual(len(plan["skipped_symlinks"]), 1)

    def test_open_logs_are_flagged_and_excluded_from_reclaim_estimate(self) -> None:
        planner_path = Path(__file__).parent / "scripts" / "xiuxian_log_retention.py"
        spec = importlib.util.spec_from_file_location("xiuxian_log_retention_under_test_4", planner_path)
        assert spec is not None and spec.loader is not None
        planner = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = planner
        spec.loader.exec_module(planner)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "state" / "accounts" / "tg1" / "flow1" / "send.launchd.log"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"x" * 200)

            plan = planner.build_retention_plan(
                root / "state" / "accounts",
                rolling_limit_bytes=100,
                rolling_keep_bytes=25,
                per_run_keep_count=10,
                open_paths={path.resolve()},
            )

            self.assertEqual(len(plan["rolling_logs"]), 1)
            self.assertTrue(plan["rolling_logs"][0]["open_detected"])
            self.assertEqual(plan["rolling_logs"][0]["estimated_reclaim_bytes"], 0)
            self.assertEqual(plan["estimated_reclaim_bytes"], 0)

    def test_symlink_flow_directory_is_excluded_from_plan(self) -> None:
        planner_path = Path(__file__).parent / "scripts" / "xiuxian_log_retention.py"
        spec = importlib.util.spec_from_file_location("xiuxian_log_retention_under_test_5", planner_path)
        assert spec is not None and spec.loader is not None
        planner = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = planner
        spec.loader.exec_module(planner)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / "state" / "accounts"
            account_dir = state_root / "tg1"
            account_dir.mkdir(parents=True)
            outside = root / "outside-flow"
            outside.mkdir()
            (outside / "all-run-20260728-000001.log").write_bytes(b"x" * 100)
            (account_dir / "flow1").symlink_to(outside, target_is_directory=True)

            plan = planner.build_retention_plan(
                state_root,
                rolling_limit_bytes=10,
                rolling_keep_bytes=5,
                per_run_keep_count=0,
            )

            self.assertEqual(plan["rolling_logs"], [])
            self.assertEqual(plan["per_run_logs"], [])
            self.assertIn(str(account_dir / "flow1"), plan["skipped_symlinks"])

    def test_open_detection_failure_makes_reclaim_estimate_unknown(self) -> None:
        planner_path = Path(__file__).parent / "scripts" / "xiuxian_log_retention.py"
        spec = importlib.util.spec_from_file_location("xiuxian_log_retention_under_test_6", planner_path)
        assert spec is not None and spec.loader is not None
        planner = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = planner
        spec.loader.exec_module(planner)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "state" / "accounts" / "tg1" / "flow1" / "send.launchd.log"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"x" * 200)

            plan = planner.build_retention_plan(
                root / "state" / "accounts",
                rolling_limit_bytes=100,
                rolling_keep_bytes=25,
                per_run_keep_count=10,
                open_paths=set(),
                open_detection_status="unavailable",
            )

            self.assertEqual(plan["open_file_detection"], "unavailable")
            self.assertIsNone(plan["estimated_reclaim_bytes"])
            self.assertIsNone(plan["rolling_logs"][0]["estimated_reclaim_bytes"])

    def test_symlink_state_root_is_rejected(self) -> None:
        planner_path = Path(__file__).parent / "scripts" / "xiuxian_log_retention.py"
        spec = importlib.util.spec_from_file_location("xiuxian_log_retention_under_test_7", planner_path)
        assert spec is not None and spec.loader is not None
        planner = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = planner
        spec.loader.exec_module(planner)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside"
            (outside / "tg1" / "flow1").mkdir(parents=True)
            (outside / "tg1" / "flow1" / "send.launchd.log").write_bytes(b"x" * 200)
            linked_root = root / "accounts"
            linked_root.symlink_to(outside, target_is_directory=True)

            plan = planner.build_retention_plan(
                linked_root,
                rolling_limit_bytes=100,
                rolling_keep_bytes=25,
                per_run_keep_count=10,
            )

            self.assertEqual(plan["rolling_logs"], [])
            self.assertEqual(plan["per_run_logs"], [])
            self.assertEqual(plan["root_status"], "rejected_symlink")
            self.assertIsNone(plan["estimated_reclaim_bytes"])

    def test_symlink_ancestor_of_state_root_is_rejected(self) -> None:
        planner_path = Path(__file__).parent / "scripts" / "xiuxian_log_retention.py"
        spec = importlib.util.spec_from_file_location("xiuxian_log_retention_under_test_8", planner_path)
        assert spec is not None and spec.loader is not None
        planner = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = planner
        spec.loader.exec_module(planner)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real_parent = root / "real-parent"
            state_root = real_parent / "accounts"
            (state_root / "tg1" / "flow1").mkdir(parents=True)
            (state_root / "tg1" / "flow1" / "send.launchd.log").write_bytes(b"x" * 200)
            linked_parent = root / "linked-parent"
            linked_parent.symlink_to(real_parent, target_is_directory=True)

            plan = planner.build_retention_plan(
                linked_parent / "accounts",
                rolling_limit_bytes=100,
                rolling_keep_bytes=25,
                per_run_keep_count=10,
                trusted_root=root,
            )

            self.assertEqual(plan["rolling_logs"], [])
            self.assertEqual(plan["root_status"], "rejected_symlink_ancestor")
            self.assertIsNone(plan["estimated_reclaim_bytes"])

    def test_state_root_outside_trusted_root_is_rejected(self) -> None:
        planner_path = Path(__file__).parent / "scripts" / "xiuxian_log_retention.py"
        spec = importlib.util.spec_from_file_location("xiuxian_log_retention_under_test_10", planner_path)
        assert spec is not None and spec.loader is not None
        planner = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = planner
        spec.loader.exec_module(planner)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trusted = root / "trusted"
            trusted.mkdir()
            outside = root / "outside" / "accounts"
            (outside / "tg1" / "flow1").mkdir(parents=True)
            (outside / "tg1" / "flow1" / "send.launchd.log").write_bytes(b"x" * 200)
            plan = planner.build_retention_plan(
                outside,
                rolling_limit_bytes=100,
                rolling_keep_bytes=25,
                per_run_keep_count=10,
                trusted_root=trusted,
            )
            self.assertEqual(plan["root_status"], "rejected_outside_trusted_root")
            self.assertEqual(plan["rolling_logs"], [])
            self.assertIsNone(plan["estimated_reclaim_bytes"])

    def test_dotdot_escape_from_trusted_root_is_rejected(self) -> None:
        planner_path = Path(__file__).parent / "scripts" / "xiuxian_log_retention.py"
        spec = importlib.util.spec_from_file_location("xiuxian_log_retention_under_test_11", planner_path)
        assert spec is not None and spec.loader is not None
        planner = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = planner
        spec.loader.exec_module(planner)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trusted = root / "trusted"
            trusted.mkdir()
            outside = root / "outside" / "accounts"
            (outside / "tg1" / "flow1").mkdir(parents=True)
            escaped = trusted / ".." / "outside" / "accounts"
            plan = planner.build_retention_plan(
                escaped,
                rolling_limit_bytes=100,
                rolling_keep_bytes=25,
                per_run_keep_count=10,
                trusted_root=trusted,
            )
            self.assertEqual(plan["root_status"], "rejected_outside_trusted_root")
            self.assertEqual(plan["rolling_logs"], [])


class RetentionEdgeCaseTests(unittest.TestCase):
    def _planner(self):
        planner_path = Path(__file__).parent / "scripts" / "xiuxian_log_retention.py"
        spec = importlib.util.spec_from_file_location("xiuxian_log_retention_edge", planner_path)
        assert spec is not None and spec.loader is not None
        planner = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = planner
        spec.loader.exec_module(planner)
        return planner

    def test_nested_directory_symlink_is_reported(self) -> None:
        planner = self._planner()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / "state" / "accounts"
            flow_dir = state_root / "tg1" / "flow1"
            flow_dir.mkdir(parents=True)
            outside = root / "outside"
            outside.mkdir()
            (flow_dir / "nested").symlink_to(outside, target_is_directory=True)
            plan = planner.build_retention_plan(state_root, rolling_limit_bytes=100, rolling_keep_bytes=25, per_run_keep_count=10)
            self.assertIn(str(flow_dir / "nested"), plan["skipped_symlinks"])

    def test_negative_api_parameters_are_rejected(self) -> None:
        planner = self._planner()
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                planner.build_retention_plan(Path(tmp), rolling_limit_bytes=-1, rolling_keep_bytes=-5, per_run_keep_count=-1)


class ActiveConfigSecureOpenTests(unittest.TestCase):
    def test_config_ancestor_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trusted = root / "trusted"
            trusted.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (outside / "active_flows.json").write_text('{"flows": []}', encoding="utf-8")
            linked_config = trusted / "config"
            linked_config.symlink_to(outside, target_is_directory=True)
            with patch.object(healthcheck, "XIUXIAN_ROOT", trusted), patch.object(healthcheck, "CONFIG_PATH", linked_config / "active_flows.json"):
                flows, error = healthcheck.load_active_config()
            self.assertEqual(flows, [])
            self.assertIsNotNone(error)

    def test_fifo_symlink_and_oversized_config_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            valid = root / "valid.json"
            valid.write_text('{"flows": []}', encoding="utf-8")
            fifo = root / "fifo.json"
            os.mkfifo(fifo)
            symlink = root / "link.json"
            symlink.symlink_to(valid)
            oversized = root / "large.json"
            oversized.write_bytes(b" " * (healthcheck.MAX_CONFIG_JSON_BYTES + 1))
            for path in (fifo, symlink, oversized):
                with self.subTest(path=path), patch.object(healthcheck, "CONFIG_PATH", path):
                    flows, error = healthcheck.load_active_config()
                self.assertEqual(flows, [])
                self.assertIsNotNone(error)


class ActiveConfigFailureTests(unittest.TestCase):
    def test_missing_active_registry_is_degraded_not_healthy(self) -> None:
        missing = Path(tempfile.gettempdir()) / "definitely-missing-active-flows.json"
        with patch.object(healthcheck, "CONFIG_PATH", missing):
            flows, error = healthcheck.load_active_config()

        self.assertEqual(flows, [])
        self.assertIsNotNone(error)
        summary = healthcheck.summarize([], [], config_error=error)
        self.assertEqual(summary["overall_status"], "degraded")
        self.assertEqual(summary["counts"]["bad"], 1)

    def test_invalid_entries_are_rejected_as_config_error(self) -> None:
        invalid_entries = [
            {"flow": "flow1", "kinds": ["send"]},
            {"account": "tg1", "kinds": ["send"]},
            {"account": "../outside", "flow": "flow1", "kinds": ["send"]},
            {"account": "tg1", "flow": "/tmp/outside", "kinds": ["send"]},
            {"account": "tg1", "flow": "flow1", "kinds": "send"},
            {"account": "tg1", "flow": "flow1", "kinds": ["unknown"]},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "active_flows.json"
            config.write_text(json.dumps({"flows": invalid_entries}), encoding="utf-8")
            with patch.object(healthcheck, "CONFIG_PATH", config):
                flows, error = healthcheck.load_active_config()

        self.assertEqual(flows, [])
        self.assertIsNotNone(error)

    def test_valid_config_entries_are_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "active_flows.json"
            config.write_text(
                json.dumps(
                    {
                        "flows": [
                            {
                                "account": "tg9100000001",
                                "flow": "shendubiguan",
                                "kinds": ["send", "watchdog", "return_observer"],
                                "enabled": True,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(healthcheck, "CONFIG_PATH", config):
                flows, error = healthcheck.load_active_config()

        self.assertIsNone(error)
        self.assertEqual(len(flows), 1)

    def test_disabled_and_non_dict_entries_are_still_schema_validated(self) -> None:
        invalid_payloads = [
            {"flows": ["not-an-object"]},
            {"flows": [{"account": "tg1", "flow": "flow1", "kinds": ["send"], "enabled": 0}]},
            {"flows": [{"account": "tg1", "flow": "flow1", "kinds": ["send"], "enabled": "false"}]},
            {"flows": [{"account": "tg1", "flow": "flow1", "kinds": [{}], "enabled": False}]},
            {"flows": [{"account": "tg1", "flow": "flow1", "kinds": [[]], "enabled": True}]},
            {"flows": [{"account": "a\u0000x", "flow": "flow1", "kinds": ["send"], "enabled": True}]},
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as tmp:
                config = Path(tmp) / "active_flows.json"
                config.write_text(json.dumps(payload), encoding="utf-8")
                with patch.object(healthcheck, "CONFIG_PATH", config):
                    flows, error = healthcheck.load_active_config()
                self.assertEqual(flows, [])
                self.assertIsNotNone(error)


class AtomicSnapshotTests(unittest.TestCase):
    def test_retention_plan_cleans_temp_on_replace_failure(self) -> None:
        planner_path = Path(__file__).parent / "scripts" / "xiuxian_log_retention.py"
        spec = importlib.util.spec_from_file_location("xiuxian_log_retention_under_test_9", planner_path)
        assert spec is not None and spec.loader is not None
        planner = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = planner
        spec.loader.exec_module(planner)

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "plan.json"
            with patch.object(os, "replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    planner.write_json_atomic(output, {"x": 1})
            self.assertEqual(list(output.parent.glob(f".{output.name}.*")), [])

    def test_health_snapshot_uses_atomic_replace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "health.json"
            payload = {"summary": {"overall_status": "healthy"}}
            with patch.object(os, "replace", wraps=os.replace) as replace_mock:
                healthcheck.persist_json_atomic(output, payload)

            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), payload)
            self.assertEqual(replace_mock.call_count, 1)
            self.assertEqual(list(output.parent.glob(f".{output.name}.*")), [])

    def test_health_snapshot_cleans_temp_on_serialization_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "health.json"
            with self.assertRaises(TypeError):
                healthcheck.persist_json_atomic(output, {"bad": object()})
            self.assertEqual(list(output.parent.glob(f".{output.name}.*")), [])

    def test_health_snapshot_keeps_private_mode_and_cleans_temp_on_replace_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "health.json"
            with patch.object(os, "replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    healthcheck.persist_json_atomic(output, {"x": 1}, mode=0o600)
            self.assertEqual(list(output.parent.iterdir()), [])
            healthcheck.persist_json_atomic(output, {"x": 1}, mode=0o600)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)


class StructuredStateRobustnessTests(unittest.TestCase):
    def test_malformed_timestamps_do_not_crash_classification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "accounts"
            flow_dir = state_root / "tg1" / "flow1"
            flow_dir.mkdir(parents=True)
            (flow_dir / "last_attempt.json").write_text(
                json.dumps({"status": "started", "not_before_ts": "abc", "ts": "bad"}),
                encoding="utf-8",
            )
            (flow_dir / "cooldown_retry.json").write_text(
                json.dumps({"x": {"not_before_ts": "xyz"}}),
                encoding="utf-8",
            )
            with patch.object(healthcheck, "STATE_ROOT", state_root):
                item = healthcheck.classify_kind("tg1", "flow1", "send", "", True, {})

            self.assertEqual(item.status, "started")
            self.assertIsNone(item.cooldown_until_ts)

    def test_non_finite_timestamps_do_not_crash_classification(self) -> None:
        for bad_value in (float("nan"), float("inf"), float("-inf"), 1e300):
            with self.subTest(bad_value=bad_value), tempfile.TemporaryDirectory() as tmp:
                state_root = Path(tmp) / "accounts"
                flow_dir = state_root / "tg1" / "flow1"
                flow_dir.mkdir(parents=True)
                (flow_dir / "last_attempt.json").write_text(
                    json.dumps({"status": "started", "not_before_ts": bad_value}),
                    encoding="utf-8",
                )
                (flow_dir / "cooldown_retry.json").write_text(
                    json.dumps({"x": {"not_before_ts": bad_value}}),
                    encoding="utf-8",
                )
                with patch.object(healthcheck, "STATE_ROOT", state_root):
                    item = healthcheck.classify_kind("tg1", "flow1", "send", "", True, {})
                self.assertIsNone(item.cooldown_until_ts)

    def test_missing_state_root_is_reported_as_degraded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing-accounts"
            with patch.object(healthcheck, "STATE_ROOT", missing):
                legacy, error = healthcheck.collect_legacy_items("", [], {})
            self.assertEqual(legacy, [])
            self.assertIsNotNone(error)
            summary = healthcheck.summarize([], [], state_error=error)
            self.assertEqual(summary["overall_status"], "degraded")

    def test_non_string_status_does_not_crash_classification(self) -> None:
        for bad_status in ([], {}, 123):
            with self.subTest(bad_status=bad_status), tempfile.TemporaryDirectory() as tmp:
                state_root = Path(tmp) / "accounts"
                flow_dir = state_root / "tg1" / "flow1"
                flow_dir.mkdir(parents=True)
                (flow_dir / "last_attempt.json").write_text(
                    json.dumps({"status": bad_status}), encoding="utf-8"
                )
                with patch.object(healthcheck, "STATE_ROOT", state_root):
                    item = healthcheck.classify_kind("tg1", "flow1", "send", "", True, {})
                self.assertEqual(item.severity, "warn")
                self.assertEqual(item.status, "malformed_state")

    def test_nested_legacy_traversal_error_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "accounts"
            account_dir = state_root / "tg1"
            account_dir.mkdir(parents=True)
            original_iterdir = Path.iterdir

            def flaky_iterdir(target: Path):
                if Path(target) == account_dir:
                    raise PermissionError(account_dir)
                return original_iterdir(target)

            with patch.object(healthcheck, "STATE_ROOT", state_root), patch.object(Path, "iterdir", flaky_iterdir):
                legacy, error = healthcheck.collect_legacy_items("", [], {})
            self.assertEqual(legacy, [])
            self.assertIsNotNone(error)


class StateIntegrityTests(unittest.TestCase):
    def test_active_and_legacy_symlink_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "accounts"
            account_dir = state_root / "tg1"
            account_dir.mkdir(parents=True)
            outside = Path(tmp) / "outside"
            outside.mkdir()
            (outside / "send.launchd.log").write_text("FAIL SECRET_OUTSIDE\n", encoding="utf-8")
            (account_dir / "flow1").symlink_to(outside, target_is_directory=True)
            with patch.object(healthcheck, "STATE_ROOT", state_root):
                item = healthcheck.classify_kind("tg1", "flow1", "send", "", True, {})
                legacy, error = healthcheck.collect_legacy_items("", [], {})
            self.assertEqual(item.status, "invalid_state_path")
            self.assertNotIn("SECRET_OUTSIDE", item.note)
            self.assertEqual(legacy, [])
            self.assertIsNotNone(error)

    def test_stale_success_does_not_clear_pending_and_malformed_success_warns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "accounts"
            flow_dir = state_root / "tg1" / "flow1"
            flow_dir.mkdir(parents=True)
            now = time.time()
            (flow_dir / "last_attempt.json").write_text(json.dumps({"status": "sent_no_ack", "ts": now}), encoding="utf-8")
            (flow_dir / "last_success.json").write_text(json.dumps({"status": "acknowledged", "ts": 1}), encoding="utf-8")
            with patch.object(healthcheck, "STATE_ROOT", state_root):
                item = healthcheck.classify_kind("tg1", "flow1", "send", "", True, {})
            self.assertEqual(item.severity, "warn")
            (flow_dir / "last_success.json").write_text(json.dumps({"status": [], "ts": now}), encoding="utf-8")
            (flow_dir / "last_attempt.json").write_text(json.dumps({"status": "acknowledged", "ts": now}), encoding="utf-8")
            with patch.object(healthcheck, "STATE_ROOT", state_root):
                item = healthcheck.classify_kind("tg1", "flow1", "send", "", True, {})
            self.assertEqual(item.status, "malformed_state")

    def test_malformed_or_future_observer_state_is_not_healthy(self) -> None:
        for payload in ({"status": [], "ts": time.time()}, {"status": "unknown", "ts": time.time()}, {"status": "no_new_event", "ts": time.time() + 86400}):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as tmp:
                state_root = Path(tmp) / "accounts"
                flow_dir = state_root / "tg1" / "flow1"
                flow_dir.mkdir(parents=True)
                (flow_dir / "observer_last_seen.json").write_text(json.dumps(payload), encoding="utf-8")
                with patch.object(healthcheck, "STATE_ROOT", state_root), patch.object(healthcheck, "launchctl_snapshot", return_value="state = spawn scheduled"):
                    item = healthcheck.classify_kind("tg1", "flow1", "return_observer", "", True, {})
                self.assertNotEqual(item.severity, "ok")


class FinalAdversarialTests(unittest.TestCase):
    def test_state_and_log_file_symlinks_are_not_followed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / "accounts"
            flow_dir = state_root / "tg1" / "flow1"
            flow_dir.mkdir(parents=True)
            outside_state = root / "outside.json"
            outside_state.write_text(json.dumps({"status": "acknowledged", "ts": time.time()}), encoding="utf-8")
            outside_log = root / "outside.log"
            outside_log.write_text("FAIL SECRET_OUTSIDE\n", encoding="utf-8")
            (flow_dir / "last_attempt.json").symlink_to(outside_state)
            (flow_dir / "send.launchd.log").symlink_to(outside_log)
            with patch.object(healthcheck, "STATE_ROOT", state_root):
                item = healthcheck.classify_kind("tg1", "flow1", "send", "", True, {})
            self.assertEqual(item.status, "malformed_state")
            self.assertNotIn("SECRET_OUTSIDE", item.note)

    def test_invalid_json_and_non_object_state_are_malformed(self) -> None:
        for content in ("{broken", "[]"):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as tmp:
                state_root = Path(tmp) / "accounts"
                flow_dir = state_root / "tg1" / "flow1"
                flow_dir.mkdir(parents=True)
                (flow_dir / "last_attempt.json").write_text(json.dumps({"status": "acknowledged", "ts": time.time()}), encoding="utf-8")
                (flow_dir / "last_success.json").write_text(content, encoding="utf-8")
                with patch.object(healthcheck, "STATE_ROOT", state_root):
                    item = healthcheck.classify_kind("tg1", "flow1", "send", "", True, {})
                self.assertEqual(item.status, "malformed_state")

    def test_future_and_stale_success_cannot_clear_pending(self) -> None:
        for success_ts in (1, time.time() + 86400):
            with self.subTest(success_ts=success_ts), tempfile.TemporaryDirectory() as tmp:
                state_root = Path(tmp) / "accounts"
                flow_dir = state_root / "tg1" / "flow1"
                flow_dir.mkdir(parents=True)
                (flow_dir / "last_attempt.json").write_text(json.dumps({"status": "sent_no_ack", "ts": time.time()}), encoding="utf-8")
                (flow_dir / "last_success.json").write_text(json.dumps({"status": "acknowledged", "ts": success_ts}), encoding="utf-8")
                with patch.object(healthcheck, "STATE_ROOT", state_root):
                    item = healthcheck.classify_kind("tg1", "flow1", "send", "", True, {})
                self.assertEqual(item.severity, "warn")

    def test_stale_good_status_is_not_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "accounts"
            flow_dir = state_root / "tg1" / "flow1"
            flow_dir.mkdir(parents=True)
            (flow_dir / "last_attempt.json").write_text(json.dumps({"status": "acknowledged", "ts": 1}), encoding="utf-8")
            with patch.object(healthcheck, "STATE_ROOT", state_root):
                item = healthcheck.classify_kind("tg1", "flow1", "send", "", True, {})
            self.assertNotEqual(item.severity, "ok")

    def test_stale_expired_and_started_future_cooldown_are_not_healthy(self) -> None:
        cases = [
            ("flow0", {"status": "expired", "ts": 1}),
            ("flow1", {"status": "started", "ts": 1, "not_before_ts": time.time() + 3600}),
        ]
        for flow, payload in cases:
            with self.subTest(flow=flow), tempfile.TemporaryDirectory() as tmp:
                state_root = Path(tmp) / "accounts"
                flow_dir = state_root / "tg1" / flow
                flow_dir.mkdir(parents=True)
                (flow_dir / "last_attempt.json").write_text(json.dumps(payload), encoding="utf-8")
                with patch.object(healthcheck, "STATE_ROOT", state_root):
                    item = healthcheck.classify_kind("tg1", flow, "send", "", True, {})
                self.assertNotEqual(item.severity, "ok")

    def test_unrecognized_recent_observer_log_does_not_make_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "accounts"
            flow_dir = state_root / "tg1" / "flow1"
            flow_dir.mkdir(parents=True)
            (flow_dir / "observer.launchd.log").write_text("unrecognized activity only\n", encoding="utf-8")
            with patch.object(healthcheck, "STATE_ROOT", state_root), patch.object(healthcheck, "launchctl_snapshot", return_value="state = spawn scheduled"):
                item = healthcheck.classify_kind("tg1", "flow1", "return_observer", "", True, {})
            self.assertNotEqual(item.severity, "ok")

    def test_process_matching_uses_token_boundaries(self) -> None:
        proc = "python observe_return.py --account tg10 --flow flow10"
        self.assertFalse(healthcheck.flow_has_process(proc, "tg1", "flow1", "return_observer"))

    def test_live_observer_wins_over_unrelated_sender_state_damage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "accounts"
            flow_dir = state_root / "tg1" / "flow1"
            flow_dir.mkdir(parents=True)
            (flow_dir / "last_attempt.json").write_text(json.dumps({"status": []}), encoding="utf-8")
            proc = "python observe_return.py state/accounts/tg1/flow1"
            with patch.object(healthcheck, "STATE_ROOT", state_root), patch.object(healthcheck, "launchctl_snapshot", return_value="state = running"):
                item = healthcheck.classify_kind("tg1", "flow1", "return_observer", proc, True, {})
            self.assertEqual(item.status, "running")


class SecureFilePrimitiveTests(unittest.TestCase):
    def test_dangling_state_symlink_is_malformed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.symlink_to(Path(tmp) / "missing.json")
            payload, state = healthcheck.load_json_state(path)
            self.assertEqual(payload, {})
            self.assertEqual(state, "malformed")

    def test_fifo_state_is_rejected_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            os.mkfifo(path)
            payload, state = healthcheck.load_json_state(path)
            self.assertEqual(payload, {})
            self.assertEqual(state, "malformed")

    def test_oversized_state_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_bytes(b" " * (healthcheck.MAX_STATE_JSON_BYTES + 1))
            payload, state = healthcheck.load_json_state(path)
            self.assertEqual(payload, {})
            self.assertEqual(state, "malformed")

    def test_missing_no_follow_capability_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{}", encoding="utf-8")
            with patch.object(healthcheck, "O_NOFOLLOW", None):
                payload, state = healthcheck.load_json_state(path)
            self.assertEqual(payload, {})
            self.assertEqual(state, "unsupported")

    def test_fd_is_closed_when_fdopen_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "log.log"
            path.write_text("hello\n", encoding="utf-8")
            with patch.object(os, "open", return_value=123), patch.object(os, "fdopen", side_effect=OSError("boom")), patch.object(os, "close") as close_mock:
                self.assertEqual(healthcheck.tail_lines(path), [])
            close_mock.assert_called_once_with(123)

    def test_state_root_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real-accounts"
            (real / "tg1" / "flow1").mkdir(parents=True)
            linked = root / "accounts"
            linked.symlink_to(real, target_is_directory=True)
            with patch.object(healthcheck, "STATE_ROOT", linked):
                item = healthcheck.classify_kind("tg1", "flow1", "send", "", True, {})
            self.assertEqual(item.status, "invalid_state_path")

    def test_state_root_ancestor_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trusted = root / "trusted"
            trusted.mkdir()
            outside = root / "outside"
            (outside / "accounts" / "tg1" / "flow1").mkdir(parents=True)
            linked_state = trusted / "state"
            linked_state.symlink_to(outside, target_is_directory=True)
            with patch.object(healthcheck, "XIUXIAN_ROOT", trusted), patch.object(healthcheck, "STATE_ROOT", linked_state / "accounts"):
                item = healthcheck.classify_kind("tg1", "flow1", "send", "", True, {})
            self.assertEqual(item.status, "invalid_state_path")

    def test_json_read_is_hard_bounded_after_stat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{}", encoding="utf-8")
            original_fdopen = os.fdopen

            class GrowingReader:
                def __init__(self, wrapped):
                    self.wrapped = wrapped
                def __enter__(self):
                    self.wrapped.__enter__()
                    return self
                def __exit__(self, *args):
                    return self.wrapped.__exit__(*args)
                def read(self, size=-1):
                    return "{" + (" " * (healthcheck.MAX_STATE_JSON_BYTES + 1))

            def growing_fdopen(fd, mode, *args, **kwargs):
                return GrowingReader(original_fdopen(fd, mode, *args, **kwargs))

            with patch.object(os, "fdopen", growing_fdopen):
                payload, state = healthcheck.load_json_state(path)
            self.assertEqual(payload, {})
            self.assertEqual(state, "malformed")

    def test_unsupported_state_capability_is_malformed_in_classification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "accounts"
            flow_dir = state_root / "tg1" / "flow1"
            flow_dir.mkdir(parents=True)
            (flow_dir / "last_attempt.json").write_text("{}", encoding="utf-8")
            with patch.object(healthcheck, "STATE_ROOT", state_root), patch.object(healthcheck, "O_NOFOLLOW", None):
                item = healthcheck.classify_kind("tg1", "flow1", "send", "", True, {})
            self.assertEqual(item.status, "malformed_state")

    def test_legacy_rejects_symlink_state_root_before_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real"
            real.mkdir()
            linked = root / "accounts"
            linked.symlink_to(real, target_is_directory=True)
            with patch.object(healthcheck, "STATE_ROOT", linked):
                legacy, error = healthcheck.collect_legacy_items("", [], {})
            self.assertEqual(legacy, [])
            self.assertIsNotNone(error)

    def test_legacy_rejects_state_root_ancestor_symlink_before_listing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trusted = root / "trusted"
            trusted.mkdir()
            outside = root / "outside"
            (outside / "accounts" / "secret-account" / "secret-flow").mkdir(parents=True)
            linked_state = trusted / "state"
            linked_state.symlink_to(outside, target_is_directory=True)
            state_root = linked_state / "accounts"
            with patch.object(healthcheck, "XIUXIAN_ROOT", trusted), patch.object(healthcheck, "STATE_ROOT", state_root):
                legacy, error = healthcheck.collect_legacy_items("", [], {})
            self.assertEqual(legacy, [])
            self.assertIsNotNone(error)
            summary = healthcheck.summarize([], legacy, state_error=error)
            self.assertEqual(summary["overall_status"], "degraded")


class EventIdleEvidenceTests(unittest.TestCase):
    def test_future_good_log_does_not_hide_current_bad_log(self) -> None:
        for offset in (1, 30, 60, 86400):
            with self.subTest(offset=offset), tempfile.TemporaryDirectory() as tmp:
                flow_dir = Path(tmp)
                bad = flow_dir / "send.log"
                good = flow_dir / "send.launchd.log"
                bad.write_text("Traceback (most recent call last):\n", encoding="utf-8")
                good.write_text("No new return-summary event found for markers=['x']\n", encoding="utf-8")
                now = time.time()
                os.utime(bad, (now, now))
                os.utime(good, (now + offset, now + offset))
                severity, signal = healthcheck.find_recent_log_signal(flow_dir, "send")
                self.assertEqual(severity, "bad")
                self.assertIn("Traceback", signal or "")

    def test_retired_idle_markers_do_not_make_a_stateless_flow_healthy(self) -> None:
        for flow, line in (("xuangu_zuoda", "No matching event found for @tester in last 360 messages"),
                           ("jiaru_zhandou", "No recent entry message found with button 'x' in last 80 messages")):
            with self.subTest(flow=flow), tempfile.TemporaryDirectory() as tmp:
                state_root = Path(tmp) / "accounts"
                flow_dir = state_root / "tg1" / flow
                flow_dir.mkdir(parents=True)
                (flow_dir / "send.launchd.log").write_text(line + "\n", encoding="utf-8")
                with patch.object(healthcheck, "STATE_ROOT", state_root):
                    item = healthcheck.classify_kind("tg1", flow, "send", "", True, {})
                self.assertNotEqual(item.severity, "ok")


class PlannerRootTests(unittest.TestCase):
    def _planner(self, name: str):
        planner_path = Path(__file__).parent / "scripts" / "xiuxian_log_retention.py"
        spec = importlib.util.spec_from_file_location(name, planner_path)
        assert spec is not None and spec.loader is not None
        planner = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = planner
        spec.loader.exec_module(planner)
        return planner

    def test_regular_file_root_is_structurally_rejected(self) -> None:
        planner = self._planner("xiuxian_log_retention_file_root")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "accounts"
            root.write_text("not a directory", encoding="utf-8")
            plan = planner.build_retention_plan(root, rolling_limit_bytes=1, rolling_keep_bytes=0, per_run_keep_count=0)
            self.assertEqual(plan["root_status"], "not_directory")

    def test_plan_uses_open_detected_name(self) -> None:
        planner = self._planner("xiuxian_log_retention_open_name")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "accounts"
            path = root / "tg1" / "flow1" / "send.launchd.log"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"x" * 10)
            plan = planner.build_retention_plan(root, rolling_limit_bytes=1, rolling_keep_bytes=0, per_run_keep_count=0, open_paths={path.resolve()})
            self.assertIn("open_detected", plan["rolling_logs"][0])
            self.assertNotIn("open_for_write", plan["rolling_logs"][0])


class ObserverRecoveryPrecedenceTests(unittest.TestCase):
    def test_newer_healthy_state_clears_older_observer_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "accounts"
            flow_dir = state_root / "tg1" / "shendubiguan"
            flow_dir.mkdir(parents=True)
            log = flow_dir / "observer.launchd.log"
            log.write_text("Traceback (most recent call last):\nold failure\n", encoding="utf-8")
            old = time.time() - 60
            os.utime(log, (old, old))
            (flow_dir / "observer_last_seen.json").write_text(
                json.dumps({"status": "no_new_event", "ts": time.time()}), encoding="utf-8"
            )
            proc = "observe_return.py --account tg1 --flow shendubiguan"
            with patch.object(healthcheck, "STATE_ROOT", state_root):
                item = healthcheck.classify_kind("tg1", "shendubiguan", "return_observer", proc, True, {})
            self.assertEqual(item.severity, "ok")
            self.assertEqual(item.status, "running")

    def test_newer_observer_error_still_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "accounts"
            flow_dir = state_root / "tg1" / "shendubiguan"
            flow_dir.mkdir(parents=True)
            (flow_dir / "observer_last_seen.json").write_text(
                json.dumps({"status": "no_new_event", "ts": time.time() - 60}), encoding="utf-8"
            )
            log = flow_dir / "observer.launchd.log"
            log.write_text("Traceback (most recent call last):\nnew failure\n", encoding="utf-8")
            proc = "observe_return.py --account tg1 --flow shendubiguan"
            with patch.object(healthcheck, "STATE_ROOT", state_root):
                item = healthcheck.classify_kind("tg1", "shendubiguan", "return_observer", proc, True, {})
            self.assertEqual(item.severity, "bad")


class WatchdogIdleSemanticsTests(unittest.TestCase):
    def test_event_lease_watchdog_does_not_misclassify_confirmed_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "accounts"
            flow_dir = state_root / "tg1" / "shenji_budao"
            flow_dir.mkdir(parents=True)
            (flow_dir / "last_attempt.json").write_text(json.dumps({"status": "command_confirmed", "ts": time.time()}))
            (flow_dir / "watchdog.launchd.log").write_text(
                "Watchdog lease check complete; business confirmation is owned by the event monitor\n")
            with patch.object(healthcheck, "STATE_ROOT", state_root):
                item = healthcheck.classify_kind("tg1", "shenji_budao", "watchdog", "", True, {})
            self.assertEqual((item.severity, item.status), ("ok", "watchdog_idle"))

    def test_verified_newer_status_probe_explains_active_task_but_not_new_pending_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "accounts"
            flow_dir = state_root / "tg1" / "yuanyingchuqiao"
            flow_dir.mkdir(parents=True)
            now = time.time()
            (flow_dir / "last_attempt.json").write_text(json.dumps({"status": "settled", "ts": now - 100}))
            probe = {"status": "countdown_pending", "ts": now - 60, "not_before_ts": now + 3600,
                     "actor_id": 1, "flow_slug": "yuanyingchuqiao", "target": "@example_game_group",
                     "topic_id": 7310786, "message": ".元婴状态", "acked": True,
                     "sent_message_id": 100, "reply_message_id": 101, "reply_sender_id": 8917921351}
            with patch.object(healthcheck, "STATE_ROOT", state_root):
                for change in ({}, {"actor_id": 2}, {"reply_sender_id": 123}, {"acked": False},
                               {"ts": now - 200}, {"not_before_ts": now - 1}, {"message": ".其他指令"}):
                    with self.subTest(change=change):
                        (flow_dir / "status_probe.json").write_text(json.dumps({**probe, **change}))
                        item = healthcheck.classify_kind("tg1", "yuanyingchuqiao", "send", "", True, {})
                        self.assertEqual(item.severity, "warn" if change else "ok")
                (flow_dir / "status_probe.json").write_text(json.dumps(probe))
                (flow_dir / "command_pending.json").write_text(json.dumps({"pending": True, "actor_id": 1,
                    "flow_slug": "yuanyingchuqiao", "target": "@example_game_group", "topic_id": 7310786,
                    "next_retry_ts": now + 120}))
                item = healthcheck.classify_kind("tg1", "yuanyingchuqiao", "send", "", True, {})
                self.assertEqual((item.severity, item.status), ("warn", "awaiting_official_reply"))

    def test_watchdog_log_does_not_confirm_a_pending_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "accounts"
            flow_dir = state_root / "tg1" / "zhaohuanmoying"
            flow_dir.mkdir(parents=True)
            (flow_dir / "last_attempt.json").write_text(
                json.dumps({"status": "resent_pending_ack", "ts": time.time() - 120}), encoding="utf-8"
            )
            (flow_dir / "watchdog.launchd.log").write_text(
                "Healthy waiting state: pending attempt age=120s status=resent_pending_ack\n", encoding="utf-8"
            )
            with patch.object(healthcheck, "STATE_ROOT", state_root):
                item = healthcheck.classify_kind("tg1", "zhaohuanmoying", "watchdog", "", True, {})
            self.assertEqual(item.severity, "warn")
            self.assertEqual(item.status, "resent_pending_ack")

    def test_recent_stale_run_only_watchdog_log_is_idle_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "accounts"
            flow_dir = state_root / "tg1" / "shenji_budao"
            flow_dir.mkdir(parents=True)
            (flow_dir / "watchdog.launchd.log").write_text(
                "No success state yet at state/accounts/tg1/shenji_budao/last_success.json; watchdog is only enforcing stale-run health\n",
                encoding="utf-8",
            )
            with patch.object(healthcheck, "STATE_ROOT", state_root):
                item = healthcheck.classify_kind("tg1", "shenji_budao", "watchdog", "", True, {})
            self.assertEqual(item.severity, "ok")
            self.assertEqual(item.status, "watchdog_idle")


class SnapshotCacheTests(unittest.TestCase):
    def test_same_flow_kind_log_signal_is_loaded_once_per_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            flow_dir = Path(tmp)
            (flow_dir / "send.launchd.log").write_text("No new return-summary event found for markers=['x']\n", encoding="utf-8")
            cache: dict[tuple[Path, str], tuple[str | None, str | None]] = {}

            with patch.object(healthcheck, "find_recent_log_signal", wraps=healthcheck.find_recent_log_signal) as signal_mock:
                first = healthcheck.cached_log_signal(flow_dir, "send", cache)
                second = healthcheck.cached_log_signal(flow_dir, "send", cache)

            self.assertEqual(first, second)
            self.assertEqual(signal_mock.call_count, 1)

    def test_flow_state_files_are_read_once_across_kinds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "accounts"
            (state_root / "tg1" / "flow1").mkdir(parents=True)
            cache: dict = {}
            with patch.object(healthcheck, "STATE_ROOT", state_root), \
                    patch.object(healthcheck, "load_json_state", wraps=healthcheck.load_json_state) as loads, \
                    patch.object(healthcheck, "has_recent_watchdog_ok_signal", return_value=False) as watchdog:
                healthcheck.classify_kind("tg1", "flow1", "watchdog", "", True, cache)
                first = loads.call_count
                healthcheck.classify_kind("tg1", "flow1", "send", "", True, cache)
                healthcheck.classify_kind("tg1", "flow1", "watchdog", "", True, cache)

            self.assertGreater(first, 0)
            self.assertEqual(loads.call_count, first)
            self.assertEqual(watchdog.call_count, 1)

    def test_send_kind_does_not_read_observer_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            flow_dir = Path(tmp)
            (flow_dir / "send.launchd.log").write_text(
                "[2026-07-28] Previous success; new cycle started cleanly\n",
                encoding="utf-8",
            )
            (flow_dir / "observer.launchd.log").write_text(
                "Traceback (most recent call last):\n",
                encoding="utf-8",
            )

            self.assertEqual(healthcheck.find_recent_log_signal(flow_dir, "send"), (None, None))
            self.assertEqual(
                healthcheck.find_recent_log_signal(flow_dir, "return_observer"),
                ("bad", "Traceback (most recent call last):"),
            )


if __name__ == "__main__":
    unittest.main()
