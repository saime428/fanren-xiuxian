"""Regressions from the 2026-09-12 Wendao settlement and 180-second timeout."""
import json
import os
import shlex
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import reply_parsing
import send_topic_message as sender
from scripts import xiuxian_healthcheck as healthcheck


OWNER = 9100000006
BOT = 8918867302
TOPIC = 7310786
COMMAND_ID = 12283620
REPLY_ID = 12283621
PENDING = "你消耗了 1000 点修为，虔诚地向宗门长老问道，希望能获得一丝天机..."
REWARD = "【问道得宝】\n长老对你的勤勉颇为赞许，赐下诸多宝物！你获得了：\n- 修为增加了 813 点\n- 【一阶妖丹】x5\n- 灵石x232\n- 二级妖丹x2"


def wrapper_settings():
    # Read literal flow settings without sourcing credentials or running the
    # production shell wrapper, authorization probe or Telegram sender.
    values = {}
    text = (Path(__file__).parent / "scripts/send_wendao_tg9100000006.sh").read_text()
    for line in text.splitlines():
        if line.startswith("export TG_"):
            key, value = shlex.split(line.removeprefix("export "))[0].split("=", 1)
            if "$" not in value:
                values[key] = value
    return values


def classify(text, *, owner=OWNER, reply_to=COMMAND_ID, bot=BOT, date_ts=10000):
    command = SimpleNamespace(id=COMMAND_ID, sender_id=owner, message=".问道",
                              reply_to_msg_id=TOPIC, reply_to=None)
    reply = SimpleNamespace(id=REPLY_ID, sender_id=bot, message=text,
                            reply_to_msg_id=reply_to, reply_to=None,
                            date=datetime.fromtimestamp(date_ts, timezone.utc))
    return reply_parsing.classify_reply_messages(
        [command, reply], topic_id=TOPIC, own_sender_id=OWNER,
        target_message=".问道", sent_message_id=COMMAND_ID, reset_triggers=[],
        cooldown_markers=reply_parsing.parse_markers(os.environ["TG_COOLDOWN_MARKER_TEXT"]),
        policy=sender._ack_policy_from_env(BOT), log=lambda *args: None,
    )


class Clock:
    def __init__(self, elapsed=0):
        self.elapsed = float(elapsed)

    def monotonic(self):
        return self.elapsed

    def time(self):
        return 10000 + self.elapsed

    async def sleep(self, seconds):
        self.elapsed += seconds


class WendaoReplyTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, wrapper_settings(), clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def test_actual_reward_is_a_terminal_success_from_rotating_bots(self):
        for bot in (BOT, 8964348409):
            with self.subTest(bot=bot):
                outcome = classify(REWARD, bot=bot)
                self.assertTrue(outcome.acked)
                self.assertTrue(outcome.succeeded)
                self.assertFalse(outcome.failed)
                self.assertEqual(outcome.ack_reply_message_id, REPLY_ID)
                self.assertEqual(reply_parsing.decide_poll_action(outcome), "acknowledged")

    def test_spending_notification_waits_for_edited_settlement_even_from_named_bot(self):
        self.assertFalse(classify(PENDING).acked)
        self.assertTrue(classify(REWARD).succeeded)

    def test_sample_peer_12_elses_command_and_topic_root_reply_are_not_our_receipt(self):
        self.assertFalse(classify(REWARD, owner=9100000001).acked)
        self.assertFalse(classify(REWARD, reply_to=TOPIC).acked)

    def test_actual_twelve_hour_cooldown_remains_a_cooldown(self):
        outcome = classify("天机不可频繁窥探，请在 11小时54分钟30秒 后再来问道。")
        self.assertTrue(outcome.acked)
        self.assertFalse(outcome.succeeded)
        self.assertEqual(outcome.cooldown_retry_seconds, 42870)

    def test_success_guard_matches_observed_twelve_hour_server_cooldown(self):
        self.assertEqual(os.environ["TG_MIN_REPEAT_SECONDS"], "43200")
        self.assertEqual(os.environ["TG_PRE_REPEAT_PROBE_SECONDS"], "0")
        self.assertEqual(os.environ["TG_REQUIRE_ACK_REPLY"], "1")


class SenderCooldownBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        environment = patch.dict(os.environ, {
            **wrapper_settings(), "TG_API_ID": "123", "TG_API_HASH": "fixture",
            "TG_TARGET": "@example_game_group", "TG_TOPIC_ID": str(TOPIC),
            "TG_FLOW_SLUG": "wendao", "TG_STATE_DIR": str(self.root),
            "TG_SESSION_NAME": str(self.root / "fixture.session"),
            "XIU_ATTEMPT_LEDGER": "0",
        }, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.clock = Clock(5)
        self.cfg = sender.build_config()

    def run_context(self):
        return sender.SendRun(self.cfg, SimpleNamespace(), object(), OWNER,
                              sender._ack_policy_from_env(BOT), run_started_monotonic=0)

    async def test_154_second_wait_is_deferred_with_full_reply_window_reserved(self):
        with patch.object(sender, "time", self.clock), patch.object(reply_parsing, "time", self.clock), \
             patch.object(sender.asyncio, "sleep", new=AsyncMock()) as sleep:
            run = self.run_context()
            self.assertEqual(run.inline_wait_budget_seconds(), 127)
            waited = await run.maybe_wait_for_cooldown_retry(
                not_before_ts=self.clock.time() + 154, source="fixture-existing-cooldown")
        self.assertFalse(waited)
        sleep.assert_not_awaited()
        pending = json.loads(self.cfg.cooldown_state_path.read_text())[self.cfg.state_key]
        self.assertEqual(pending["not_before_ts"], 10159)
        self.assertFalse(self.cfg.last_attempt_path.exists())

    async def test_short_cooldown_can_still_wait_and_leave_room_for_receipt(self):
        with patch.object(sender, "time", self.clock), patch.object(reply_parsing, "time", self.clock), \
             patch.object(sender.asyncio, "sleep", new=AsyncMock(side_effect=self.clock.sleep)):
            run = self.run_context()
            self.assertTrue(await run.maybe_wait_for_cooldown_retry(
                not_before_ts=self.clock.time() + 60, source="fixture-short-cooldown"))
        self.assertEqual(self.clock.elapsed, 65)
        self.assertGreaterEqual(self.cfg.run_max_seconds - self.clock.elapsed, 48)

    async def test_connection_time_is_included_in_the_original_run_budget(self):
        async def start():
            self.clock.elapsed += 40

        async def retry(action, **_kwargs):
            return await action()

        client = SimpleNamespace(start=AsyncMock(side_effect=start),
                                 get_entity=AsyncMock(return_value=SimpleNamespace(id=BOT)),
                                 get_me=AsyncMock(return_value=SimpleNamespace(id=OWNER)),
                                 disconnect=AsyncMock())
        with patch.object(sender, "time", self.clock), patch.object(reply_parsing, "time", self.clock), \
             patch.object(sender, "load_dotenv"), \
             patch.object(sender, "telegram_client", return_value=client), \
             patch.object(sender, "with_telegram_retry", side_effect=retry), \
             patch.object(sender, "_run_send_cycle", new=AsyncMock()) as cycle:
            await sender.run_once()
            run = cycle.await_args.args[0]
            self.assertEqual(run.run_started_monotonic, 5)
            self.assertEqual(run.inline_wait_budget_seconds(), 92)

    async def test_late_reward_recovers_success_without_sending_again(self):
        self.cfg.last_attempt_path.write_text(json.dumps({
            "sent_message_id": COMMAND_ID, "status": "sent", "ts": 9995,
            "actual_send_ts": 9995,
        }))
        with patch.object(sender, "time", self.clock), patch.object(reply_parsing, "time", self.clock), \
             patch.object(sender, "should_stop_before_send", return_value=False), \
             patch.object(sender, "collect_recent_messages", AsyncMock(return_value=[])):
            run = self.run_context()
            run.gather = AsyncMock(return_value=classify(REWARD))
            run.send = AsyncMock()
            await sender._run_send_cycle(run)
        run.send.assert_not_awaited()
        success = json.loads(self.cfg.last_success_path.read_text())
        self.assertEqual(success["status"], "acknowledged")
        self.assertEqual(success["sent_message_id"], COMMAND_ID)
        self.assertEqual(success["ack_reply_message_id"], REPLY_ID)

    async def test_pending_message_edit_settles_the_original_send_inside_the_deadline(self):
        async def replies(sent_id, **_kwargs):
            if sent_id is None:
                return reply_parsing.ReplyOutcome()
            text = REWARD if self.clock.elapsed >= 23 else PENDING
            return classify(text, date_ts=self.clock.time())

        with patch.object(sender, "time", self.clock), patch.object(reply_parsing, "time", self.clock), \
             patch.object(sender, "should_stop_before_send", return_value=False), \
             patch.object(sender, "collect_recent_messages", new=AsyncMock(return_value=[])), \
             patch.object(sender.asyncio, "sleep", new=AsyncMock(side_effect=self.clock.sleep)):
            run = self.run_context()
            run.gather = AsyncMock(side_effect=replies)
            run.send = AsyncMock(return_value=SimpleNamespace(id=COMMAND_ID))
            await sender._run_send_cycle(run)
        run.send.assert_awaited_once()
        self.assertEqual(self.clock.elapsed, 23)
        success = json.loads(self.cfg.last_success_path.read_text())
        self.assertEqual(success["status"], "acknowledged")
        self.assertEqual(success["ack_reply_message_id"], REPLY_ID)
        self.assertEqual(success["ack_reply_text"], REWARD)


class CooldownHealthAttributionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name) / "accounts"
        self.directory = root / f"tg{OWNER}" / "wendao"
        self.directory.mkdir(parents=True)
        state_root = patch.object(healthcheck, "STATE_ROOT", root)
        state_root.start()
        self.addCleanup(state_root.stop)
        self.attempt = {"status": "cooldown_pending", "ts": time.time(),
                        "acked": True, "actor_id": OWNER, "flow_slug": "wendao", "sent_message_id": 10,
                        "ack_reply_message_id": 11, "ack_reply_sender_id": 8917921351,
                        "target": "@example_game_group", "topic_id": TOPIC, "message": ".问道"}
        (self.directory / "last_attempt.json").write_text(json.dumps(self.attempt))
        self.deadline = time.time() + 3600
        self.active_key = f"@example_game_group|{TOPIC}|.问道"
        self.active = {**self.attempt, "not_before_ts": self.deadline}
        self.legacy = {**self.active, "target": "-1009400000005", "not_before_ts": 1786650924}

    def classify(self, cooldowns):
        (self.directory / "cooldown_retry.json").write_text(json.dumps(cooldowns))
        return healthcheck.classify_kind(f"tg{OWNER}", "wendao", "send", "", True, {})

    def test_current_group_cooldown_wins_over_first_legacy_group_record(self):
        item = self.classify({f"-1009400000005|{TOPIC}|.问道": self.legacy,
                              self.active_key: self.active})
        self.assertEqual(item.cooldown_until_ts, self.deadline)
        self.assertEqual(item.severity, "ok")

    def test_other_group_or_mislabeled_payload_cannot_supply_the_deadline(self):
        for cooldowns in ({f"-1009400000005|{TOPIC}|.问道": self.legacy},
                          {self.active_key: self.legacy},
                          {self.active_key: {**self.active, "message": ".其他指令"}}):
            with self.subTest(cooldowns=cooldowns):
                self.assertIsNone(self.classify(cooldowns).cooldown_until_ts)


if __name__ == "__main__":
    unittest.main()
