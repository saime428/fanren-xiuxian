import asyncio
import os
import tempfile
import time
import unittest
import unittest.mock
from types import SimpleNamespace
from pathlib import Path

from reply_parsing import (
    classify_action_reply,
    classify_status_reply,
    parse_cooldown_retry_delay_seconds,
    parse_duration_seconds,
)
from observe_return import event_matches, run_observer_loop
from send_shendubiguan import compute_cached_not_before_ts, lock_busy_exit_code, retryable_outcome_exit_code
from send_shendubiguan import extract_matching_replies
from state_store import FlowLockBusy, flow_lock, load_json_dict, persist_json_atomic
from telegram_send_gate import _claim_send_slot, resolve_gate_path, PROJECT_GATE_PATH
from watch_send_health import attempt_is_recent_and_pending

MARKERS = ["时间没到", "后再来", "後再來", "后再试", "後再試"]


class AnchoredCooldownParserTests(unittest.TestCase):
    def test_unrelated_numbers_do_not_inflate_wait(self):
        text = "【闭关修炼】\n本次闭关时长 8小时，修为 +120。\n灵气未复，请在 3分钟 后再来。"
        self.assertEqual(parse_cooldown_retry_delay_seconds(text, MARKERS), 180)

    def test_contribution_points_are_ignored(self):
        text = "【野外历练】贡献 20分，山中灵机未复，请在 45秒 后再来。"
        self.assertEqual(parse_cooldown_retry_delay_seconds(text, MARKERS), 45)

    def test_traditional_anchor(self):
        self.assertEqual(parse_cooldown_retry_delay_seconds("修為 +99，請在 2分鐘 後再試。", MARKERS), 120)

    def test_whole_text_fallback_without_anchor(self):
        self.assertEqual(parse_cooldown_retry_delay_seconds("冷却剩余 1分钟30秒，时间没到。", MARKERS), 90)

    def test_parse_duration_seconds_units_and_clock(self):
        self.assertEqual(parse_duration_seconds("预计还需 7小时59分钟"), 7 * 3600 + 59 * 60)
        self.assertEqual(parse_duration_seconds("01:02:03"), 3723)
        self.assertIsNone(parse_duration_seconds("没有数字"))


class ShendubiguanClassifierTests(unittest.TestCase):
    def test_status_countdown(self):
        kind, seconds = classify_status_reply("你正在深度闭关，预计还需 7小时59分钟")
        self.assertEqual(kind, "countdown")
        self.assertEqual(seconds, 7 * 3600 + 59 * 60)

    def test_status_summary_requires_username_when_given(self):
        text = "【深度闭关总结】@sample_peer_12 修为大涨"
        self.assertEqual(classify_status_reply(text, expected_username="sample_peer_12")[0], "summary")
        self.assertEqual(classify_status_reply(text, expected_username="other")[0], "unknown")

    def test_status_not_in_retreat(self):
        self.assertEqual(classify_status_reply("你并未处于深度闭关之中")[0], "not_in_retreat")

    def test_action_started_uses_parsed_duration(self):
        kind, seconds = classify_action_reply("你已进入深度闭关状态，预计 8小时 后出关")
        self.assertEqual(kind, "started")
        self.assertEqual(seconds, 8 * 3600)

    def test_cached_not_before_prefers_latest(self):
        now = time.time()
        receipt = {"acked": True, "actor_id": 9100000001, "flow_slug": "shendubiguan",
                   "target": "@example_game_group", "topic_id": 7310786, "sent_message_id": 1,
                   "reply_message_id": 2, "reply_sender_id": 8917921351}
        probe = {**receipt, "message": ".查看闭关", "status": "countdown_pending", "not_before_ts": now + 100}
        success = {**receipt, "message": ".深度闭关", "status": "started", "ts": now - 10, "not_before_ts": now + 500}
        self.assertEqual(compute_cached_not_before_ts(probe, success, min_repeat_seconds=28800,
                                                     actor_id=9100000001), now + 500)

    def test_cached_not_before_derives_from_success_ts_when_missing(self):
        now = time.time()
        success = {"status": "started", "ts": now - 100, "acked": True, "actor_id": 9100000001,
                   "flow_slug": "shendubiguan", "message": ".深度闭关", "target": "@example_game_group",
                   "topic_id": 7310786, "sent_message_id": 1, "reply_message_id": 2, "reply_sender_id": 8917921351}
        computed = compute_cached_not_before_ts({}, success, min_repeat_seconds=28800, actor_id=9100000001)
        self.assertAlmostEqual(computed, now - 100 + 28800, delta=1.0)

    def test_unconfirmed_started_state_cannot_establish_a_countdown(self):
        success = {"status": "started", "ts": time.time(), "not_before_ts": time.time() + 28800}
        self.assertEqual(compute_cached_not_before_ts({}, success, min_repeat_seconds=28800,
                                                     actor_id=9100000001), 0)

    def test_cached_not_before_empty_state(self):
        self.assertEqual(compute_cached_not_before_ts({}, {}, min_repeat_seconds=28800), 0.0)


class ShendubiguanObserverKickTests(unittest.TestCase):
    def test_immediate_observer_kick_lock_busy_is_retryable_failure(self) -> None:
        self.assertEqual(lock_busy_exit_code(observer_immediate_kick=True), 75)

    def test_scheduled_lock_busy_remains_clean_skip(self) -> None:
        self.assertEqual(lock_busy_exit_code(observer_immediate_kick=False), 0)

    def test_observer_kick_retries_incomplete_outcomes(self) -> None:
        for status in (
            "paused",
            "status_send_failed",
            "probe_timeout",
            "unknown",
            "action_send_failed",
            "action_timeout",
            "summary",
        ):
            with self.subTest(status=status):
                self.assertEqual(retryable_outcome_exit_code(status, observer_immediate_kick=True), 75)

    def test_observer_kick_accepts_completed_or_healthy_outcomes(self) -> None:
        for status in ("started", "already_in_retreat", "countdown_pending", "cached_countdown_pending"):
            with self.subTest(status=status):
                self.assertEqual(retryable_outcome_exit_code(status, observer_immediate_kick=True), 0)
        self.assertEqual(retryable_outcome_exit_code("action_timeout", observer_immediate_kick=False), 0)


class ReturnObserverEventSecurityTests(unittest.TestCase):
    def test_event_requires_exact_username_token_and_trusted_sender(self) -> None:
        base = dict(id=101, date=None, reply_to=SimpleNamespace(reply_to_top_id=5))
        forged = SimpleNamespace(**base, sender_id=999, message="【深度闭关总结】 @demo_player_05")
        prefix_collision = SimpleNamespace(**base, sender_id=123, message="【深度闭关总结】 @sample_peer_20")
        valid = SimpleNamespace(**base, sender_id=123, message="【深度闭关总结】 @demo_player_05")
        kwargs = dict(markers=["深度闭关总结"], normalized_topic_id=5, target_username="demo_player_05", trusted_sender_ids={123}, last_seen_event_id=0)
        self.assertIsNone(event_matches(forged, **kwargs))
        self.assertIsNone(event_matches(prefix_collision, **kwargs))
        self.assertIsNotNone(event_matches(valid, **kwargs))

    def test_old_topic_root_reply_is_not_accepted_for_new_send(self) -> None:
        old = SimpleNamespace(
            id=90,
            sender_id=123,
            message="你已进入深度闭关状态，预计 8小时 后出关",
            reply_to_msg_id=5,
            reply_to=SimpleNamespace(reply_to_msg_id=5, reply_to_top_id=5),
            date=None,
        )
        replies = extract_matching_replies(
            [old], topic_id=5, sent_message_id=100, own_sender_id=999
        )
        self.assertEqual(replies, [])



class PersistentObserverLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_loop_reuses_process_and_sleeps_between_scans(self) -> None:
        calls: list[str] = []

        async def run_cycle() -> int:
            calls.append("cycle")
            return 0

        async def sleep(seconds: float) -> None:
            calls.append(f"sleep:{seconds}")

        await run_observer_loop(
            run_cycle,
            poll_seconds=3,
            max_cycles=3,
            sleep=sleep,
        )
        self.assertEqual(calls, ["cycle", "sleep:3", "cycle", "sleep:3", "cycle"])

    async def test_retryable_cycle_failure_does_not_end_loop(self) -> None:
        outcomes = iter([75, 0])
        calls: list[str] = []

        async def run_cycle() -> int:
            calls.append("cycle")
            return next(outcomes)

        async def sleep(seconds: float) -> None:
            calls.append(f"sleep:{seconds}")

        await run_observer_loop(run_cycle, poll_seconds=2, max_cycles=2, sleep=sleep)
        self.assertEqual(calls, ["cycle", "sleep:2", "cycle"])

    async def test_hung_cycle_times_out_and_next_cycle_runs(self) -> None:
        calls = 0
        sleeps: list[float] = []

        async def run_cycle() -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                await asyncio.sleep(60)
            return 0

        async def sleep(seconds: float) -> None:
            sleeps.append(seconds)

        await run_observer_loop(
            run_cycle,
            poll_seconds=5,
            cycle_timeout_seconds=0.01,
            max_cycles=2,
            sleep=sleep,
        )
        self.assertEqual(calls, 2)
        self.assertEqual(sleeps, [5])


class StateStoreTests(unittest.TestCase):
    def test_persist_json_atomic_round_trip_and_no_tmp_leftover(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            persist_json_atomic(path, {"ts": 1.5, "status": "acknowledged"})
            self.assertEqual(load_json_dict(path), {"ts": 1.5, "status": "acknowledged"})
            leftovers = [p for p in Path(tmpdir).iterdir() if p.name != "state.json"]
            self.assertEqual(leftovers, [])

    def test_flow_lock_excludes_second_holder_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "send.lock"
            with flow_lock(lock_path):
                self.assertTrue(lock_path.exists())
                with self.assertRaises(FlowLockBusy):
                    with flow_lock(lock_path):
                        pass
            self.assertFalse(lock_path.exists())


class SendGateTests(unittest.TestCase):
    def test_claim_then_immediate_second_claim_waits(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            gate_path = Path(tmpdir) / "gate.json"
            self.assertEqual(_claim_send_slot(gate_path, 5.0, "first"), 0.0)
            wait = _claim_send_slot(gate_path, 5.0, "second")
            self.assertGreater(wait, 0.0)
            self.assertLessEqual(wait, 5.0)

    def test_resolve_gate_path_honors_global_env(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            shared = str(Path(tmpdir) / "global_gate.json")
            with unittest.mock.patch.dict(os.environ, {"TG_GLOBAL_SEND_GATE_STATE_PATH": shared}, clear=False):
                self.assertEqual(resolve_gate_path(Path("state/accounts/a/flow")), Path(shared))
            with unittest.mock.patch.dict(os.environ, {"TG_GLOBAL_SEND_GATE_STATE_PATH": ""}, clear=False):
                # Without the env the gate is still one project-wide file, never a per-flow one.
                self.assertEqual(resolve_gate_path(Path("state/accounts/a/flow")), PROJECT_GATE_PATH)


class WatchdogPendingTests(unittest.TestCase):
    def test_resent_pending_ack_counts_as_pending(self):
        now = time.time()
        attempt = {"status": "resent_pending_ack", "ts": now - 30}
        pending, msg = attempt_is_recent_and_pending(attempt, now)
        self.assertTrue(pending)
        self.assertIn("status=resent_pending_ack", msg)

    def test_old_attempt_is_not_pending(self):
        now = time.time()
        attempt = {"status": "resent_pending_ack", "ts": now - 10 * 24 * 3600}
        pending, _ = attempt_is_recent_and_pending(attempt, now)
        self.assertFalse(pending)

    def test_acknowledged_attempt_is_not_pending(self):
        now = time.time()
        attempt = {"status": "acknowledged", "ts": now - 30}
        pending, _ = attempt_is_recent_and_pending(attempt, now)
        self.assertFalse(pending)


class CooldownRemainingCliTests(unittest.TestCase):
    """command_confirmation.py --cooldown-remaining, the wrappers' cache check."""

    def receipt(self, message, not_before_ts, **changes):
        return {"acked": True, "actor_id": 9100000002, "flow_slug": "flow", "target": "@example_game_group",
                "topic_id": 7310786, "message": message, "sent_message_id": 10,
                "ack_reply_message_id": 11, "ack_reply_sender_id": 8944702077,
                "not_before_ts": not_before_ts, **changes}

    def run_cli(self, state_dir, *args):
        import subprocess
        import sys
        env = {**os.environ, "XIU_ACCOUNT_SLUG": "tg9100000002", "TG_FLOW_SLUG": "flow",
               "TG_STATE_DIR": str(state_dir), "TG_TARGET": "@example_game_group", "TG_TOPIC_ID": "7310786",
               "TG_MESSAGE": ".探寻裂缝", "TG_COOLDOWN_STATE_PATH": str(state_dir / "cooldown_retry.json"),
               "TG_STATUS_PROBE_PATH": str(state_dir / "status_probe.json")}
        script = Path(__file__).resolve().parent / "command_confirmation.py"
        result = subprocess.run([sys.executable, str(script), "--cooldown-remaining", *args],
                                env=env, capture_output=True, text=True, check=True)
        return result.stdout.strip()

    def test_cooldown_retry_prints_only_a_verified_future_countdown(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            key = "@example_game_group|7310786|.探寻裂缝"
            cases = {
                "future": (self.receipt(".探寻裂缝", now + 600), True),
                "expired": (self.receipt(".探寻裂缝", now - 5), False),
                "other_account": (self.receipt(".探寻裂缝", now + 600, actor_id=9100000003), False),
                "other_command": (self.receipt(".元婴出窍", now + 600), False),
            }
            for name, (record, expected) in cases.items():
                with self.subTest(name):
                    persist_json_atomic(state / "cooldown_retry.json", {key: record})
                    output = self.run_cli(state, "cooldown_retry")
                    self.assertEqual(expected, output.isdigit() and 590 <= int(output) <= 601, output)

    def test_status_probe_respects_grace_commands_and_unconfirmed_sends(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            persist_json_atomic(state / "status_probe.json",
                                self.receipt(".查看闭关", now + 900, status="countdown_pending"))
            self.assertTrue(self.run_cli(state, "status_probe", ".查看闭关", ".深度闭关").isdigit())
            self.assertEqual("", self.run_cli(state, "status_probe", ".元婴状态"))
            persist_json_atomic(state / "status_probe.json",
                                self.receipt(".查看闭关", now + 30, status="countdown_pending"))
            self.assertEqual("", self.run_cli(state, "status_probe", ".查看闭关"))  # within the 60 s grace
            persist_json_atomic(state / "status_probe.json",
                                self.receipt(".查看闭关", now + 900, status="countdown_pending"))
            persist_json_atomic(state / "status_probe_pending.json", {"pending": True})
            self.assertEqual("", self.run_cli(state, "status_probe", ".查看闭关"))

    def test_wrappers_share_the_cli_instead_of_inline_copies(self):
        scripts = Path(__file__).resolve().parent / "scripts"
        inline = [p.name for p in scripts.rglob("*.sh") if 'wait_seconds="$({' in p.read_text(encoding="utf-8")]
        self.assertEqual([], inline)
        for wrapper in sorted(scripts.glob("send_tanxunliefeng_tg*.sh")):
            with self.subTest(wrapper.name):
                self.assertTrue(wrapper.read_text(encoding="utf-8").endswith(
                    "source ./scripts/lib/run_tanxunliefeng_send.sh\n"))
        self.assertIn("--cooldown-remaining cooldown_retry",
                      (scripts / "lib/run_tanxunliefeng_send.sh").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
