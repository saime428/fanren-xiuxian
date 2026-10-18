"""Replay the authenticated 12300624 notice/expiry/result chain offline."""
import json
import re
import unittest
from datetime import datetime
from pathlib import Path

import test_question_reply_monitor as fixtures
from question_reply_monitor import CANONICAL_CHAT_ID, question_kind
from xuangu_quiz import parse_result

NOTICE = ("【天机异象 · 玄骨考校】\n一股晦涩而强大的神念扫过天地，正是精于算计的魔道巨擘【玄骨上人】！\n"
          "他的目光，似乎对活跃的修士 @ziyuanyouxian 产生了一丝兴趣...")
EXPIRED = "【玄骨考校·题面失效】\n@ziyuanyouxian 未能及时作答..."
TIMEOUT = ("【玄骨考校·超时】\n@ziyuanyouxian 面对玄骨上人的提问，竟迟迟无法作答，被视为不堪造就。\n"
           "玄骨上人略感失望，一道神念冲击让你损失了 1000 点修为。")
NOTICE_TS = datetime.fromisoformat("2026-09-13T00:15:48+00:00").timestamp()


class XuanguReferenceChainTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.QuestionReplyTests.asyncSetUp
    asyncTearDown = fixtures.QuestionReplyTests.asyncTearDown
    monitor = fixtures.QuestionReplyTests.monitor
    message = fixtures.QuestionReplyTests.message
    observe = fixtures.QuestionReplyTests.observe

    def trust_configured_bots(self, monitor):
        runner = Path("scripts/lib/run_xuangu_zuoda.sh").read_text()
        raw = re.search(r"TG_TRIGGER_TRUSTED_SENDER_IDS:-([^}]+)", runner).group(1)
        monitor.trusted = {int(value) for value in raw.split(",")}
        self.assertTrue({8918867302, 8388633812, 7965897083} <= monitor.trusted)

    async def test_actual_other_player_chain_is_ignored_by_all_six_managed_accounts(self):
        root = self.root
        accounts = {
            "tg9100000001": "demo_player_01", "tg9100000006": "demo_player_06", "tg9100000005": "demo_player_05",
            "tg9100000004": "demo_player_04", "tg9100000002": "demo_player_02", "tg9100000003": "demo_player_03",
        }
        for account, username in accounts.items():
            with self.subTest(account=account):
                self.root = root / account
                self.root.mkdir()
                self.now = NOTICE_TS
                monitor = self.monitor(account=account, username=username)
                self.trust_configured_bots(monitor)
                self.observe(monitor, self.message(12300624, NOTICE, sender=8918867302, age=0))
                self.now += 313
                self.observe(monitor, self.message(12300660, TIMEOUT, sender=7965897083, age=0))
                self.now += 4
                self.observe(monitor, self.message(12300626, EXPIRED, sender=8388633812, age=308))
                self.assertEqual(monitor.rows, {})
                self.assertEqual(monitor.answer_receipts, {})
                self.assertEqual(monitor.queue.qsize(), 0)
                self.assertIsNone(monitor.health.data["business_issue"])
                self.assertFalse((self.root / "last_success.json").exists())
        self.assertEqual(self.client.sends, [])

    async def test_actual_roles_are_notice_expired_question_and_timeout_result(self):
        self.assertEqual(question_kind(NOTICE, "xuangu_zuoda"), "notice")
        self.assertEqual(question_kind(EXPIRED, "xuangu_zuoda"), "expired")
        self.assertEqual(question_kind(TIMEOUT, "xuangu_zuoda"), "answer_result")
        self.assertEqual(parse_result(TIMEOUT), {
            "username": "ziyuanyouxian", "variant": "考校", "outcome": "timeout", "option": None,
        })

    async def test_owned_flow_uses_historical_question_not_the_missing_original(self):
        self.now = NOTICE_TS
        monitor = self.monitor()
        self.trust_configured_bots(monitor)
        self.observe(monitor, self.message(12300624, NOTICE.replace("@ziyuanyouxian", "@demo_player_01"), sender=8918867302, age=0))
        self.assertEqual(self.client.sends, [])
        self.now += 3
        unrelated = self.message(12300625, "1", sender=845669402, age=0)
        unrelated.reply_to.reply_to_msg_id = 458347
        self.observe(monitor, unrelated)
        self.now += 6
        # The real 12300626 body has been overwritten. Use the independently
        # saved historical 虚天鼎 question fixture, never claim it is this round.
        question = self.message(12300626, age=0)
        self.observe(monitor, question)
        key = monitor.key(CANONICAL_CHAT_ID, question.id)
        await monitor.dispatch(key)
        self.assertEqual(self.client.sends, [(CANONICAL_CHAT_ID, ".作答 B", 12300626)])
        self.assertEqual(monitor.rows[key]["status"], "awaiting_answer_result")
        self.assertFalse((self.root / "last_success.json").exists())
        # Replay the real timeout template after Telegram delivery: game
        # failure must remain failure even though the command was submitted.
        self.now += 304
        self.observe(monitor, self.message(12300660, TIMEOUT.replace("@ziyuanyouxian", "@demo_player_01"), sender=7965897083, age=0))
        self.now += 4
        self.observe(monitor, self.message(12300626, EXPIRED.replace("@ziyuanyouxian", "@demo_player_01"), sender=8388633812, age=308))
        self.assertEqual(monitor.rows[key]["status"], "answer_timed_out")
        self.assertEqual(monitor.health.data["business_issue"]["code"], "answer_timed_out")
        self.assertEqual(json.loads((self.root / "last_attempt.json").read_text())["status"], "answer_timed_out")
        self.assertFalse((self.root / "last_success.json").exists())


if __name__ == "__main__":
    unittest.main()
