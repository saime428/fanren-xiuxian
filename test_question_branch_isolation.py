"""Keep Xuangu assessment, cauldron questions, and Nanlonghou trades separate.

Known message shapes come from the existing authenticated-reference fixtures.
Interleavings, type conflicts and unknown same-stem questions are deliberately
synthetic fault cases, not claims about the overwritten 12294757 question.
"""
import json
import unittest

import test_nanlonghou_trade as trade_fixtures
import test_question_reply_monitor as fixtures
import test_xuangu_question_bank as quiz_fixtures
import test_xuangu_reference_chain as reference_fixtures
import xuangu_quiz as xuangu
from question_reply_monitor import CANONICAL_CHAT_ID, CANONICAL_TOPIC_ID, question_kind


class QuestionBranchIsolationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.QuestionReplyTests.asyncSetUp
    asyncTearDown = fixtures.QuestionReplyTests.asyncTearDown
    message = fixtures.QuestionReplyTests.message
    observe = fixtures.QuestionReplyTests.observe

    def monitor(self, flow=xuangu.FLOW, **settings):
        root = self.root
        self.root = root / flow
        self.root.mkdir(exist_ok=True)
        try:
            monitor = fixtures.QuestionReplyTests.monitor(
                self, flow, bank=settings.pop("bank", quiz_fixtures.configured_answers()),
                reply_text=".交换 功法" if flow == "jiaohuan_gongfa" else ".作答 A",
                **settings)
        finally:
            self.root = root
        monitor.trusted.update({8980154525, 8944702077})
        return monitor

    def notice(self, ident, variant, age=10):
        text = (quiz_fixtures.NOTICE.replace("@hfsscxf", "@demo_player_01") if variant == "窥鼎"
                else reference_fixtures.NOTICE.replace("@ziyuanyouxian", "@demo_player_01"))
        return self.message(ident, text, age=age, sender=8980154525)

    def result(self, ident, variant, option="B", parent=None):
        message = self.message(ident, f"【玄骨{variant}·答对】\n@demo_player_01 的答案 {option} 完全正确！", age=0)
        if parent is not None:
            message.reply_to.reply_to_msg_id = parent
        return message

    async def test_interleaved_three_branches_bind_notices_and_send_only_their_commands(self):
        quiz = self.monitor()
        trade = self.monitor("jiaohuan_gongfa")
        messages = [
            self.notice(100, "窥鼎", age=20),
            self.notice(101, "考校", age=10),
            self.message(102, trade_fixtures.NOTICE.replace("@cncc01", "@demo_player_01"), age=8),
            self.message(110, quiz_fixtures.question_text(quiz_fixtures.CASES[7], 2), age=2),
            self.message(111, quiz_fixtures.question_text(quiz_fixtures.CASES[0], 1), age=1),
            self.message(112, trade_fixtures.QUESTION.replace("@demo_player_03", "@demo_player_01"), age=0),
        ]
        for message in messages:
            self.observe(quiz, message)
            self.observe(trade, message)
        key = lambda ident: quiz.key(CANONICAL_CHAT_ID, ident)
        self.assertEqual(set(quiz.rows), {key(i) for i in (100, 101, 110, 111)})
        self.assertEqual(set(trade.rows), {key(i) for i in (102, 112)})
        self.assertEqual(quiz.rows[key(100)]["question_message_id"], 110)
        self.assertEqual(quiz.rows[key(101)]["question_message_id"], 111)
        self.assertEqual(trade.rows[key(102)]["question_message_id"], 112)
        for monitor, ident in ((quiz, 110), (quiz, 111), (trade, 112)):
            await monitor.dispatch(key(ident))
        self.assertEqual(self.client.sends, [
            (CANONICAL_CHAT_ID, ".作答 C", 110),
            (CANONICAL_CHAT_ID, ".作答 B", 111),
            (CANONICAL_CHAT_ID, ".交换 功法", 112),
        ])
        self.assertFalse(quiz.state_path.exists())
        self.assertFalse(trade.state_path.exists())

    async def test_unaddressed_question_cannot_inherit_ownership_from_another_variant(self):
        monitor = self.monitor()
        self.observe(monitor, self.notice(100, "考校"))
        question = self.message(110, quiz_fixtures.question_text(quiz_fixtures.CASES[7]).replace("@demo_player_01", "这位道友"))
        question.reply_to.reply_to_msg_id = 100
        question.reply_to.reply_to_top_id = CANONICAL_TOPIC_ID
        self.observe(monitor, question)
        self.assertNotIn(monitor.key(CANONICAL_CHAT_ID, question.id), monitor.rows)
        self.assertEqual(monitor.queue.qsize(), 0)

    async def test_matching_notice_reply_still_proves_unaddressed_question_ownership(self):
        monitor = self.monitor()
        self.observe(monitor, self.notice(100, "窥鼎"))
        question = self.message(110, quiz_fixtures.question_text(quiz_fixtures.CASES[7]).replace("@demo_player_01", "这位道友"))
        question.reply_to.reply_to_msg_id = 100
        question.reply_to.reply_to_top_id = CANONICAL_TOPIC_ID
        self.observe(monitor, question)
        key = monitor.key(CANONICAL_CHAT_ID, question.id)
        await monitor.dispatch(key)
        self.assertEqual(self.client.sends, [(CANONICAL_CHAT_ID, ".作答 A", question.id)])

    async def test_wrong_variant_or_untyped_result_cannot_teach_an_answer(self):
        monitor = self.monitor(auto_update_answer_bank=True, result_marker_groups=[("玄骨", "答案", "完全正")])
        question = self.message(110, quiz_fixtures.question_text(quiz_fixtures.CASES[7], 1))
        self.observe(monitor, question)
        wrong = self.result(120, "考校", parent=question.id)
        await monitor.learn_result(wrong, CANONICAL_CHAT_ID)
        untyped = self.message(121, "玄骨：答案 B 完全正确！", age=0)
        untyped.reply_to.reply_to_msg_id = question.id
        await monitor.learn_result(untyped, CANONICAL_CHAT_ID)
        self.assertFalse(monitor.learned_path.exists())
        self.assertEqual(monitor.learned_questions, {})

    async def test_same_stem_is_learned_and_reloaded_independently_for_each_variant(self):
        settings = {"auto_update_answer_bank": True, "result_marker_groups": [("玄骨", "答案", "完全正")]}
        monitor = self.monitor(bank=(), **settings)
        # Artificial identical stems ensure the type really participates in
        # persistence and lookup, rather than relying on today's distinct bank.
        common_stem = "这一场新出现的机缘所需之物是什么？"
        questions = []
        for index, (variant, answer) in enumerate((("考校", "B"), ("窥鼎", "C"))):
            case = (variant, common_stem, "虚天鼎")
            question = self.message(110+index, quiz_fixtures.question_text(case, options=["掌天瓶", "虚天鼎", "血玉蜘蛛", "辟邪神雷"]))
            questions.append((question, answer))
            self.observe(monitor, question)
            await monitor.learn_result(self.result(120+index, variant, answer, parent=question.id), CANONICAL_CHAT_ID)
        for instance in (monitor, self.monitor(bank=(), **settings)):
            for question, answer in questions:
                self.assertEqual(instance.answer(question.message), ".作答 " + answer)
        stored = json.loads(monitor.learned_path.read_text())["questions"]
        self.assertEqual({entry["variant"] for entry in stored}, {"考校", "窥鼎"})
        self.assertEqual(len(stored), 2)

    async def test_untyped_historical_learning_is_preserved_without_cross_variant_authority(self):
        monitor = self.monitor(bank=())
        case = ("窥鼎", "另一道从未核对分支的旧题？", "虚天鼎")
        original = {"account": monitor.account, "flow": monitor.flow,
                    "questions": [{"question": case[1], "answers": [case[2]]}]}
        monitor.learned_path.write_text(json.dumps(original, ensure_ascii=False))
        monitor.reload_answer_bank()
        self.assertIsNone(monitor.answer(quiz_fixtures.question_text(case)))
        self.assertEqual(json.loads(monitor.learned_path.read_text()), original)

    async def test_other_variant_success_does_not_clear_an_unanswered_kuiding_failure(self):
        monitor = self.monitor(bank=("虚天鼎",))
        rare = self.message(110, quiz_fixtures.question_text(quiz_fixtures.CASES[7]))
        self.observe(monitor, rare)
        await monitor.dispatch(monitor.key(CANONICAL_CHAT_ID, rare.id))
        self.assertEqual(monitor.health.data["business_issue"]["code"], "answer_unavailable")
        self.now += 2
        ordinary = self.message(111, quiz_fixtures.question_text(quiz_fixtures.CASES[0], 1))
        self.observe(monitor, ordinary)
        await monitor.dispatch(monitor.key(CANONICAL_CHAT_ID, ordinary.id))
        self.observe(monitor, self.result(120, "考校", "B"))
        self.assertEqual(monitor.rows[monitor.key(CANONICAL_CHAT_ID, ordinary.id)]["status"], "answer_confirmed")
        self.assertIsNotNone(monitor.health.data["business_issue"])
        self.assertEqual(monitor.health.data["business_issue"]["message_id"], rare.id)
        self.assertEqual(monitor.health.data["business_issue"]["variant"], "窥鼎")
        # A later verified cauldron answer can establish recovery for that
        # same branch; the preceding assessment result could not.
        self.now += 400
        recovered = self.message(130, quiz_fixtures.question_text(quiz_fixtures.CASES[9], 2))
        self.observe(monitor, recovered)
        await monitor.dispatch(monitor.key(CANONICAL_CHAT_ID, recovered.id))
        self.observe(monitor, self.result(140, "窥鼎", "C"))
        self.assertIsNone(monitor.health.data["business_issue"])

    async def test_wrong_variant_expiry_cannot_reject_a_submitted_cauldron_answer(self):
        monitor = self.monitor()
        question = self.message(110, quiz_fixtures.question_text(quiz_fixtures.CASES[7]))
        self.observe(monitor, question)
        key = monitor.key(CANONICAL_CHAT_ID, question.id)
        await monitor.dispatch(key)
        for variant, expected in (("考校", "awaiting_answer_result"), ("窥鼎", "reply_rejected")):
            text = f"【玄骨{variant}·题面失效】\n@demo_player_01 未能及时作答..."
            self.assertIsNone(question_kind(text, "jiaohuan_gongfa"))
            self.observe(monitor, self.message(question.id, text))
            self.assertEqual(monitor.rows[key]["status"], expected)

    async def test_question_with_conflicting_variant_header_and_intro_is_not_answered(self):
        monitor = self.monitor()
        text = "【玄骨考校】\n" + quiz_fixtures.question_text(quiz_fixtures.CASES[7])
        question = self.message(110, text)
        self.observe(monitor, question)
        self.assertEqual(monitor.queue.qsize(), 0)
        self.assertIsNone(monitor.answer(text))


if __name__ == "__main__":
    unittest.main()
