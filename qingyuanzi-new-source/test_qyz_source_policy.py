"""Entry-driven source policy; fixtures never contact Telegram or the game API."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import qingyuanzi_host as host


def entry_message(*, chat_id=-999, message_id=12229459, topic_id=host.CANONICAL_TOPIC_ID,
                  bot_username="hantianzun16_bot", sender_id=None, is_bot=True):
    sender = SimpleNamespace(username=bot_username, bot=is_bot,
        id=sender_id if sender_id is not None else {"hantianzun16_bot":7965897083, "hantianzun23_bot":8980154525}.get(bot_username,1234))
    return SimpleNamespace(
        id=message_id, chat_id=chat_id, date=datetime.now(timezone.utc),
        reply_to=SimpleNamespace(reply_to_top_id=topic_id, reply_to_msg_id=topic_id),
        raw_text="世界通告 真仙试锋开启", sender=sender, sender_id=sender.id,
        get_sender=AsyncMock(return_value=sender),
        buttons=[[SimpleNamespace(text="进入真仙战场", url=f"https://t.me/{bot_username}?startapp=qyz_fixture_only_1")]],
    )


class CanonicalSourceTests(unittest.TestCase):
    def test_reference_sender_check_follows_the_any_bot_policy(self):
        from qyz_reference.compat import is_game_bot_sender
        self.assertTrue(is_game_bot_sender(None, entry_message(bot_username="hantianzun99_bot").sender))
        self.assertFalse(is_game_bot_sender(None, entry_message(is_bot=False).sender))

    def test_todays_reference_and_future_entries_do_not_pin_the_sender(self):
        for username in ("hantianzun23_bot", "hantianzun99_bot", "another_game_bot"):
            with self.subTest(username=username):
                msg = entry_message(chat_id=host.CANONICAL_CHAT_ID, message_id=12248312, bot_username=username)
                msg.reply_to.reply_to_top_id = None
                entry = host.extract_world_boss_entry(msg, sender=msg.sender)
                self.assertIsNotNone(entry)
                self.assertEqual(entry.bot_username, username)
                self.assertEqual(entry.message_id, 12248312)

    def test_only_the_two_requested_chat_scopes_are_accepted(self):
        for overrides in ({"message_id":0}, {"topic_id":42}, {"topic_id":None}, {"chat_id":-1009400000003}):
            msg = entry_message(**{"chat_id":host.CANONICAL_CHAT_ID, **overrides})
            self.assertIsNone(host.extract_world_boss_entry(msg))
        for topic_id in (None,42):
            msg = entry_message(chat_id=-1009400000005, message_id=1075814, topic_id=topic_id)
            self.assertIsNotNone(host.extract_world_boss_entry(msg))
        self.assertEqual(set(host.TARGET_CHAT_IDS), {-1009400000004,-1009400000005})

    def test_sender_can_differ_from_entry_bot_and_title_or_label_can_change(self):
        msg = entry_message(chat_id=host.CANONICAL_CHAT_ID, bot_username="announcement_bot", is_bot=False)
        msg.buttons[0][0].url = "https://t.me/hantianzun23_bot?startapp=qyz_fixture_entry"
        msg.buttons[0][0].text = "进入战场"
        msg.raw_text = "新的青元子入口"
        entry = host.extract_world_boss_entry(msg, sender=msg.sender)
        self.assertEqual(entry.bot_username, "hantianzun23_bot")
        self.assertIsNone(entry.bot_id)
        self.assertIsNotNone(host.extract_world_boss_entry(msg))

    def test_text_and_hidden_links_are_entries_without_buttons(self):
        url = "https://t.me/another_game_bot?startapp=qyz_fixture_text"
        for hidden in (False,True):
            msg = entry_message(chat_id=-1009400000005, topic_id=None)
            msg.buttons = []
            msg.raw_text = "青元子入口" if hidden else "青元子入口 " + url
            msg.entities = [SimpleNamespace(url=url)] if hidden else []
            self.assertIsNotNone(host.extract_world_boss_entry(msg))

    def test_only_a_telegram_qyz_deep_link_identifies_the_entry(self):
        for url in ("https://example.com/example_bot?startapp=qyz_fixture", "https://t.me/example_bot?startapp=df_fixture",
                    "https://t.me/example_bot", "http://t.me/example_bot?startapp=qyz_fixture"):
            msg = entry_message(chat_id=host.CANONICAL_CHAT_ID)
            msg.buttons[0][0].url = url
            self.assertIsNone(host.extract_world_boss_entry(msg))

    def test_forwarding_does_not_refresh_an_expired_entry(self):
        msg = entry_message(chat_id=host.CANONICAL_CHAT_ID)
        msg.fwd_from = SimpleNamespace(date=datetime.now(timezone.utc)-timedelta(seconds=61))
        self.assertIsNotNone(host.extract_world_boss_entry(msg))
        self.assertFalse(host.message_recent_enough(msg))
        msg.fwd_from.date = datetime.now(timezone.utc)-timedelta(seconds=10)
        self.assertTrue(host.message_recent_enough(msg))

    def test_foreign_chat_is_rejected_before_webview(self):
        self.assertIsNone(host.extract_world_boss_entry(entry_message()))


if __name__ == "__main__":
    unittest.main()
