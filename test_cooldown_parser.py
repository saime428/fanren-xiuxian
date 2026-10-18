import datetime as dt
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from send_topic_message import (
    compute_inline_cooldown_wait_budget_seconds,
    cleanup_replied_messages,
    persist_attempt_state,
    parse_cooldown_markers,
)
from reply_parsing import compute_repeat_guard_seconds, parse_cooldown_retry_delay_seconds


MARKERS = parse_cooldown_markers(
    '时间没到|時間沒到|还不能|還不能|后可再|後可再|后再来|後再來|后再试|後再試'
)


class CooldownParserTests(unittest.TestCase):
    def test_persist_attempt_state_keeps_latest_resent_message_id(self):
        from pathlib import Path
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            attempt_path = Path(tmpdir) / 'last_attempt.json'
            persist_attempt_state(
                attempt_path,
                {
                    'ts': 1.0,
                    'message_id': 10544505,
                    'sent_message_id': 10544505,
                    'status': 'resent_pending_ack',
                    'completion_reason': 'received cooldown reply and resent after waiting; waiting for next confirmed non-cooldown reply',
                    'acked': False,
                    'reset_trigger_text': '',
                    'reset_triggered_after_send': False,
                    'cooldown_reply_after_send': '【野外历练】\n山中灵机未复，请在 2分钟36秒 后再来。',
                    'cooldown_retry_seconds_after_send': 156,
                    'resent_message_id': 10544505,
                },
            )

            saved = json.loads(attempt_path.read_text(encoding='utf-8'))

        self.assertEqual(saved['message_id'], 10544505)
        self.assertEqual(saved['sent_message_id'], 10544505)
        self.assertEqual(saved['status'], 'resent_pending_ack')
        self.assertEqual(saved['resent_message_id'], 10544505)

    def test_inline_cooldown_wait_budget_preserves_fractional_headroom(self):
        with patch('send_topic_message.time.monotonic', return_value=125.1):
            budget = compute_inline_cooldown_wait_budget_seconds(
                run_started_monotonic=0.0,
                run_max_seconds=180,
                max_inline_wait_seconds=9000,
                safety_margin_seconds=5,
            )
        self.assertAlmostEqual(budget, 49.9, places=1)

    def test_cooldown_wait_rounds_up_instead_of_flooring(self):
        with patch('send_topic_message.time.time', return_value=100.1):
            wait_seconds = max(0, int((101.0 - 100.1) + 0.999999))
        self.assertEqual(wait_seconds, 1)

    def test_pre_repeat_probe_guard_uses_early_window_only_after_success(self):
        self.assertEqual(
            compute_repeat_guard_seconds(
                min_repeat_seconds=6900,
                pre_repeat_probe_seconds=150,
                recent_success_ts=123.0,
            ),
            6750,
        )
        self.assertEqual(
            compute_repeat_guard_seconds(
                min_repeat_seconds=6900,
                pre_repeat_probe_seconds=150,
                recent_success_ts=0.0,
            ),
            6900,
        )

    def test_zero_second_cooldown_variants(self):
        cases = [
            '【野外历练】\n山中灵机未复，请在 0秒 后再来。',
            '【野外历练】\n山中灵机未复，請在 0秒 後再來。',
            '【野外历练】\n山中灵机未复，请在 0 分 0 秒 后再来。',
            '【野外历练】\n山中灵机未复，请在 00:00 后再来。',
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(parse_cooldown_retry_delay_seconds(text, MARKERS), 0)

    def test_positive_cooldown_variants(self):
        self.assertEqual(parse_cooldown_retry_delay_seconds('请在 01:02 后再来。', MARKERS), 62)
        self.assertEqual(parse_cooldown_retry_delay_seconds('请在 1小时2分钟3秒 后再来。', MARKERS), 3723)
        self.assertEqual(parse_cooldown_retry_delay_seconds('請在 1小時2分鐘3秒 後再來。', MARKERS), 3723)

    def test_non_matching_text_returns_none(self):
        self.assertIsNone(parse_cooldown_retry_delay_seconds('灵机未复，但没有时间格式。', MARKERS))
        self.assertIsNone(parse_cooldown_retry_delay_seconds('时间没到', MARKERS))


class CleanupReplyDetectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_cooldown_reply_from_untrusted_sender_cannot_confirm_a_command(self):
        own_message = SimpleNamespace(
            id=1001,
            sender_id=111,
            message='.野外历练',
            reply_to_msg_id=7310786,
            reply_to=SimpleNamespace(reply_to_msg_id=7310786, reply_to_top_id=None),
        )
        cooldown_reply = SimpleNamespace(
            id=1002,
            sender_id=8547797815,
            message='【野外历练】\n山中灵机未复，请在 0秒 后再来。',
            reply_to_msg_id=1001,
            reply_to=SimpleNamespace(reply_to_msg_id=1001, reply_to_top_id=7310786),
            date=dt.datetime(2026, 5, 26, 1, 2, 3, tzinfo=dt.timezone.utc),
        )
        client = SimpleNamespace(
            delete_messages=AsyncMock(),
            is_connected=lambda: True,
            connect=AsyncMock(),
        )

        with patch('send_topic_message.collect_recent_messages', new=AsyncMock(return_value=[cooldown_reply, own_message])):
            matched_ids, acked, reset_triggered, cooldown_retry_seconds, cooldown_reply_text, acked_at_ts, ack_reply_message_id = await cleanup_replied_messages(
                client=client,
                chat_id="@example_game_group",
                topic_id=7310786,
                own_sender_id=111,
                target_message='.野外历练',
                reply_bot_id=8757550896,
                sent_message_id=1001,
                scan_limit=60,
                reset_triggers=(),
                cooldown_markers=MARKERS,
            )

        self.assertEqual(matched_ids, [])
        self.assertFalse(acked)
        self.assertFalse(reset_triggered)
        self.assertIsNone(cooldown_retry_seconds)
        self.assertEqual(cooldown_reply_text, '')
        self.assertIsNone(acked_at_ts)
        self.assertIsNone(ack_reply_message_id)
        client.delete_messages.assert_not_awaited()

    async def test_sent_message_id_filters_out_old_replies(self):
        old_own_message = SimpleNamespace(
            id=1001,
            sender_id=111,
            message='.闭关修炼',
            reply_to_msg_id=7310786,
            reply_to=SimpleNamespace(reply_to_msg_id=7310786, reply_to_top_id=None),
        )
        new_own_message = SimpleNamespace(
            id=1003,
            sender_id=111,
            message='.闭关修炼',
            reply_to_msg_id=7310786,
            reply_to=SimpleNamespace(reply_to_msg_id=7310786, reply_to_top_id=None),
        )
        old_reset_reply = SimpleNamespace(
            id=1002,
            sender_id=8757550896,
            message='冷却时间被重置，可以立刻再次闭关',
            reply_to_msg_id=1001,
            reply_to=SimpleNamespace(reply_to_msg_id=1001, reply_to_top_id=7310786),
            date=dt.datetime(2026, 5, 26, 1, 2, 3, tzinfo=dt.timezone.utc),
        )
        client = SimpleNamespace(
            delete_messages=AsyncMock(),
            is_connected=lambda: True,
            connect=AsyncMock(),
        )

        with patch('send_topic_message.collect_recent_messages', new=AsyncMock(return_value=[old_reset_reply, new_own_message, old_own_message])):
            matched_ids, acked, reset_triggered, cooldown_retry_seconds, cooldown_reply_text, acked_at_ts, ack_reply_message_id = await cleanup_replied_messages(
                client=client,
                chat_id="@example_game_group",
                topic_id=7310786,
                own_sender_id=111,
                target_message='.闭关修炼',
                reply_bot_id=8757550896,
                sent_message_id=1003,
                scan_limit=60,
                reset_triggers=('冷却时间被重置',),
                cooldown_markers=MARKERS,
            )

        self.assertEqual(matched_ids, [])
        self.assertFalse(acked)
        self.assertFalse(reset_triggered)
        self.assertIsNone(cooldown_retry_seconds)
        self.assertEqual(cooldown_reply_text, '')
        self.assertIsNone(acked_at_ts)
        self.assertIsNone(ack_reply_message_id)

    async def test_sent_message_id_can_be_fetched_when_original_send_scrolled_out(self):
        fetched_own_message = SimpleNamespace(
            id=1001,
            sender_id=111,
            message='.抚摸法宝 玄天斩灵剑',
            reply_to_msg_id=7310786,
            reply_to=SimpleNamespace(reply_to_msg_id=7310786, reply_to_top_id=None),
        )
        success_reply = SimpleNamespace(
            id=1002,
            sender_id=8917921351,
            message='“斩灵”发出一声清脆的鸣叫，灵光流转。\n(默契 +2, 经验 +16)',
            reply_to_msg_id=1001,
            reply_to=SimpleNamespace(reply_to_msg_id=1001, reply_to_top_id=7310786),
            date=dt.datetime(2026, 6, 17, 7, 30, 26, tzinfo=dt.timezone.utc),
        )
        client = SimpleNamespace(
            delete_messages=AsyncMock(),
            get_messages=AsyncMock(return_value=fetched_own_message),
            is_connected=lambda: True,
            connect=AsyncMock(),
        )

        with (
            patch('send_topic_message.collect_recent_messages', new=AsyncMock(return_value=[success_reply])),
            patch.dict('os.environ', {'TG_SUCCESS_MARKER_TEXT': '斩灵”发出一声清脆的鸣叫|默契 +|经验 +'}, clear=False),
        ):
            matched_ids, acked, reset_triggered, cooldown_retry_seconds, cooldown_reply_text, acked_at_ts, ack_reply_message_id = await cleanup_replied_messages(
                client=client,
                chat_id="@example_game_group",
                topic_id=7310786,
                own_sender_id=111,
                target_message='.抚摸法宝 玄天斩灵剑',
                reply_bot_id=8757550896,
                sent_message_id=1001,
                scan_limit=60,
                reset_triggers=(),
                cooldown_markers=MARKERS,
            )

        self.assertEqual(matched_ids, [1001])
        self.assertTrue(acked)
        self.assertFalse(reset_triggered)
        self.assertIsNone(cooldown_retry_seconds)
        self.assertEqual(cooldown_reply_text, '')
        self.assertEqual(acked_at_ts, dt.datetime(2026, 6, 17, 7, 30, 26, tzinfo=dt.timezone.utc).timestamp())
        self.assertEqual(ack_reply_message_id, 1002)
        client.get_messages.assert_any_await("@example_game_group", ids=1001)


class FumoFabaoCooldownParserTests(unittest.TestCase):
    def test_fumofabao_120min_countdown_parses(self):
        text = '【抚摸法宝】\n天尊提醒：法宝餘韻未散，请在 120分钟 后再来。'
        self.assertEqual(parse_cooldown_retry_delay_seconds(text, MARKERS), 7200)


if __name__ == '__main__':
    unittest.main()
