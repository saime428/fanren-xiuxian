"""Replay the 14 evidenced upstream questions without any Telegram connection.

Stems/answers and the two rare introductions are from Waaiging/fanrenxiuxian_auto
at 04d72e9026383d49ce529c88da2c2ecb8a9aca09. See the review document for the
individual question/judgment links. Generated option orders are deliberate
offline permutations, not a reconstruction of the overwritten 12294757 body.
"""
import json
import re
import tempfile
import unittest
from pathlib import Path

import test_question_reply_monitor as fixtures
import xuangu_quiz as xuangu
from question_reply_monitor import CANONICAL_CHAT_ID, question_kind


CASES = (
    ("考校", "乾蓝冰焰在原著中最初是封附在何种通天灵宝之外？", "虚天鼎"),
    ("考校", "按原著路线，乾蓝冰焰何时才适合开始从虚天鼎上缓慢炼出？", "元婴后"),
    ("考校", "玄骨妄图炼化乾蓝冰焰，是想将其修成什么魔焰？", "修罗圣火"),
    ("考校", "玄骨老魔最初真正惦记的，并不是虚天鼎本身，而是什么？", "鼎外乾蓝冰焰"),
    ("考校", "韩立在内殿对付玄骨时，最能克制魔修的手段是什么？", "辟邪神雷"),
    ("考校", "韩立能把虚天鼎从乾蓝冰焰池中拉出的关键倚仗是什么？", "血玉蜘蛛"),
    ("窥鼎", "原著路线里，乾蓝冰焰是何时才适合从虚天鼎上开始缓慢炼化？", "元婴后"),
    ("窥鼎", "按原著流程，虚天鼎与乾蓝冰焰的关系最贴切的说法是？", "先得虚天鼎，后把乾蓝自鼎上抽离"),
    ("窥鼎", "玄骨窥视虚天鼎时，真正惦记的并不是鼎本身，而是什么？", "鼎外乾蓝冰焰"),
    ("窥鼎", "韩立真正得到独立法宝【乾蓝冰焰】之前，首先拿到的是什么？", "虚天鼎"),
    ("夺焰", "下列哪一项更符合乾蓝冰焰后期化极所需的辅引思路？", "冰凤寒性与偏火残焰同炉"),
    ("夺焰", "乾蓝冰焰从虚天鼎上抽离后，在玩法上应被视作什么？", "独立法宝"),
    ("夺焰", "乾蓝冰焰后续化极时，要冲击蜕变成什么更凶的焰相？", "紫罗极火"),
    ("夺焰", "玄骨上人的化焰思路，核心在于先压住什么风险？", "鼎焰反噬"),
)
INTRO = {
    "考校": "神念直入脑海，一个苍老的声音向 @demo_player_01 提问：",
    "窥鼎": "那道魔念绕着你识海中的虚天鼎缓缓打转，向 @demo_player_01 阴恻恻地发问：",
    "夺焰": "一缕魔念直逼你识海中的乾蓝寒焰，玄骨上人的声音在 @demo_player_01 脑海中炸响：",
}
NOTICE = ("【天机异象 · 玄骨窥鼎】\n"
          "你所得的【虚天鼎】方一震鸣，鼎外寒意便引来一缕阴冷魔念。\n"
          "那是早已觊觎此焰的【玄骨上人】残念，它正隔空窥视修士 @hfsscxf 手中的鼎焰机缘...")
EXPIRED = "【玄骨窥鼎·题面失效】\n@hfsscxf 未能及时作答..."
TIMEOUT = ("【玄骨窥鼎·超时】\n@hfsscxf 面对玄骨上人的提问，竟迟迟无法作答，被视为不堪造就。\n"
           "玄骨上人略感失望，一道神念冲击让你损失了 1000 点修为。")


def configured_answers():
    runner = Path(__file__).with_name("scripts").joinpath("lib/run_xuangu_zuoda.sh").read_text()
    return re.search(r"TG_ANSWER_BANK_TEXT:-([^}]+)", runner)[1].split("|")


def question_text(case, slot=0, *, options=None, quoted=True):
    variant, stem, correct = case
    if options is None:
        options = [answer for answer in ("虚天鼎", "辟邪神雷", "血玉蜘蛛", "元婴后") if answer != correct][:3]
        options.insert(slot, correct)
    body = f"“{stem}”" if quoted else stem
    return (INTRO[variant] + "\n\n" + body + "\n\n" +
            "\n".join(f"{letter}. {answer}" for letter, answer in zip("ABCD", options)) +
            "\n\n小辈，你有 300秒 的时间，可直接点击下方按钮作答，也可回复本消息并使用 .作答 <选项> 给出你的答案。")


class XuanguQuestionBankTests(unittest.TestCase):
    def test_all_evidenced_questions_follow_every_option_shuffle(self):
        for case in CASES:
            for slot in range(4):
                with self.subTest(variant=case[0], question=case[1], slot=slot):
                    text = question_text(case, slot)
                    parsed = xuangu.parse_question(text)
                    self.assertIsNotNone(parsed)
                    self.assertEqual(parsed["variant"], case[0])
                    self.assertEqual(xuangu.resolve_answer(text, configured_answers(), {}), f".作答 {'ABCD'[slot]}")

    def test_similar_flame_questions_require_their_own_answer(self):
        options = ["南明离火", "修罗圣火", "紫罗极火", "九幽冥焰"]
        for index, letter in ((2, "B"), (12, "C")):
            with self.subTest(question=CASES[index][1]):
                text = question_text(CASES[index], options=options)
                self.assertEqual(xuangu.resolve_answer(text, configured_answers(), {}), ".作答 " + letter)

    def test_unknown_negated_ambiguous_and_wrong_variant_questions_are_not_guessed(self):
        case = CASES[7]
        original = question_text(case)
        for text in (
            original.replace(case[1], "哪一项与上述原著流程完全相反？"),
            original.replace("A. " + case[2], "A. 并不是" + case[2]),
            original.replace("B. 虚天鼎", "B. " + case[2]),
            original.replace(INTRO["窥鼎"], INTRO["考校"]),
        ):
            with self.subTest(text=text):
                self.assertIsNone(xuangu.resolve_answer(text, configured_answers(), {}))

    def test_duoyan_unquoted_stem_and_results_keep_the_correct_variant(self):
        text = question_text(CASES[13], slot=3, quoted=False)
        self.assertEqual(xuangu.parse_question(text)["variant"], "夺焰")
        self.assertEqual(xuangu.resolve_answer(text, configured_answers(), {}), ".作答 D")
        for spelling in ("夺焰", "奪焰"):
            parsed = xuangu.parse_result(f"【玄骨{spelling}·答对】\n@demo_player_01 的答案 D 完全正确！")
            self.assertEqual(parsed, {"variant": "夺焰", "username": "demo_player_01", "outcome": "correct", "option": "D"})

    def test_requested_chain_is_notice_expired_question_and_timeout(self):
        self.assertEqual(question_kind(NOTICE, xuangu.FLOW), "notice")
        self.assertEqual(question_kind(EXPIRED, xuangu.FLOW), "expired")
        self.assertEqual(question_kind(TIMEOUT, xuangu.FLOW), "answer_result")
        self.assertEqual(xuangu.parse_result(TIMEOUT)["variant"], "窥鼎")

    def test_answer_bank_text_and_file_merge_without_blank_or_equivalent_answers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bank.json"
            path.write_text(json.dumps({"answers": ["虚天鼎", " ", "元婴后"]}), encoding="utf-8")
            merged = xuangu.merge_answer_bank(xuangu.parse_answer_bank("虛天鼎|掌天瓶\n"),
                                              xuangu.load_answer_bank_file(path))
            missing = xuangu.load_answer_bank_file(Path(tmp) / "missing.json")
        self.assertEqual(merged, ["虛天鼎", "掌天瓶", "元婴后"])
        self.assertEqual(missing, [])


class XuanguRareMonitorTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.QuestionReplyTests.asyncSetUp
    asyncTearDown = fixtures.QuestionReplyTests.asyncTearDown
    monitor = fixtures.QuestionReplyTests.monitor
    message = fixtures.QuestionReplyTests.message
    observe = fixtures.QuestionReplyTests.observe

    def rare_monitor(self):
        monitor = self.monitor(bank=configured_answers())
        monitor.trusted.update({8980154525, 8944702077})
        return monitor

    async def test_all_kuiding_questions_reply_once_and_wait_for_matching_official_result(self):
        monitor = self.rare_monitor()
        for index, case in enumerate(CASES[6:10]):
            with self.subTest(question=case[1]):
                base = 12305000 + 100 * index
                self.now += 400
                notice = self.message(base, NOTICE.replace("@hfsscxf", "@demo_player_01"), sender=8980154525, age=9)
                self.observe(monitor, notice)
                self.assertEqual(len(self.client.sends), index)
                question = self.message(base+5, question_text(case, index), age=0)
                self.observe(monitor, question)
                key = monitor.key(CANONICAL_CHAT_ID, question.id)
                await monitor.dispatch(key)
                self.observe(monitor, question)
                await monitor.dispatch(key)
                self.assertEqual(len(self.client.sends), index+1)
                self.assertEqual(self.client.sends[-1], (CANONICAL_CHAT_ID, f".作答 {'ABCD'[index]}", question.id))
                self.assertEqual(monitor.rows[key]["status"], "awaiting_answer_result")
                self.assertEqual(monitor.rows[monitor.key(CANONICAL_CHAT_ID, base)]["status"], "question_received")
                path = self.root / "last_success.json"
                self.assertTrue(not path.exists() or json.loads(path.read_text())["event_message_id"] != question.id)
                result = self.message(base+20, f"【玄骨窥鼎·答对】\n@demo_player_01 的答案 {'ABCD'[index]} 完全正确！", sender=8944702077, age=0)
                self.observe(monitor, result)
                self.assertEqual(monitor.rows[key]["status"], "answer_confirmed")
                self.assertEqual(json.loads(path.read_text())["event_message_id"], question.id)

    async def test_duoyan_cannot_be_confirmed_by_a_kaojiao_or_kuiding_result(self):
        monitor = self.rare_monitor()
        question = self.message(12306000, question_text(CASES[12], 2))
        self.observe(monitor, question)
        key = monitor.key(CANONICAL_CHAT_ID, question.id)
        await monitor.dispatch(key)
        self.assertEqual(self.client.sends, [(CANONICAL_CHAT_ID, ".作答 C", question.id)])
        for index, variant in enumerate(("考校", "窥鼎", "夺焰")):
            self.observe(monitor, self.message(12306010+index, f"【玄骨{variant}·答对】\n@demo_player_01 的答案 C 完全正确！", age=0))
            self.assertEqual(monitor.rows[key]["status"], "answer_confirmed" if variant == "夺焰" else "awaiting_answer_result")

    async def test_kuiding_retries_across_restarts_until_an_old_copy_gets_official_result(self):
        for index, case in enumerate(CASES[6:10]):
            with self.subTest(question=case[1]):
                self.now += 400
                monitor = self.rare_monitor()
                question = self.message(12307000 + index, question_text(case, index), age=0)
                self.observe(monitor, question)
                key = monitor.key(CANONICAL_CHAT_ID, question.id)
                before = len(self.client.sends)
                await monitor.dispatch(key)
                first_id = monitor.rows[key]["sent_message_id"]
                deadline = monitor.deadline(monitor.rows[key])

                for attempt in range(2, 5):
                    self.now += 31
                    monitor = self.rare_monitor()
                    monitor.tick()
                    await monitor.dispatch(key)
                    self.assertEqual(len(self.client.sends), before + attempt)
                    self.assertEqual(self.client.sends[-1],
                                     (CANONICAL_CHAT_ID, f".作答 {'ABCD'[index]}", question.id))
                    self.assertEqual(monitor.rows[key]["status"], "awaiting_answer_result")
                    self.assertEqual(monitor.deadline(monitor.rows[key]), deadline)
                    if monitor.state_path.exists():
                        self.assertNotEqual(json.loads(monitor.state_path.read_text())["event_message_id"], question.id)

                result = self.message(first_id + 1000,
                    f"【玄骨窥鼎·答对】\n@demo_player_01 的答案 {'ABCD'[index]} 完全正确！",
                    sender=8944702077, age=0)
                result.reply_to.reply_to_msg_id = first_id
                self.observe(monitor, result)
                self.assertEqual(monitor.rows[key]["status"], "answer_confirmed")
                self.assertEqual(len(monitor.rows[key]["sent_message_ids"]), 4)
                confirmed = json.loads(monitor.state_path.read_text())
                self.assertEqual(confirmed["event_message_id"], question.id)
                self.assertEqual(confirmed["receipt_message_id"], result.id)

                self.now += 31
                monitor = self.rare_monitor()
                monitor.tick()
                await monitor.dispatch(key)
                self.assertEqual(len(self.client.sends), before + 4)
                self.assertEqual(monitor.rows[key]["status"], "answer_confirmed")

    async def test_requested_other_player_kuiding_does_not_trigger_an_answer(self):
        monitor = self.rare_monitor()
        for ident, text, sender in ((12294752, NOTICE, 8980154525), (12294757, EXPIRED, 8388633812), (12294812, TIMEOUT, 8944702077)):
            self.observe(monitor, self.message(ident, text, sender=sender))
        self.assertEqual(monitor.rows, {})
        self.assertEqual(monitor.answer_receipts, {})
        self.assertEqual(self.client.sends, [])


if __name__ == "__main__":
    unittest.main()
