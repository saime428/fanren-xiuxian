"""共历心劫 group protocol tests, texts captured live in the canonical topic 2026-09-20/21."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pavilion_loop.heart_trial import HeartTrialCommander, classify_trial, run_heart_trial

CARD = "**1. 你的道心侍妾: 【妍丽】** (状态: 随行中)\n\n**情缘值**: 545\n\n---\n\n**2. 你的道心侍妾: 【文思月】** (状态: 居于藏娇阁)"
# Real anchor + in-place edits. The first round is Chinese (第一轮); the edits
# after each .稳 switch to Arabic numerals (第2轮 / 第3轮).
ANCHOR = ("【坠魔心劫·第一轮】\n你与侍妾【妍丽】步入幻境，前方魔念化形拦路。\n"
          "请回复本消息 .稳 / .狠 / .骗 进行抉择（共3轮）。")
ANCHOR_BUFF = ANCHOR + "\n【天机前兆】已生效：本次心劫入场消耗降低，首轮评分+1。"
EDIT1 = ("【坠魔心劫·第1轮已定】\n你稳守灵台，不贪快功，魔影首轮试探未能动你分毫。\n\n"
         "【坠魔心劫·第2轮】\n幻境再变，请继续回复 .稳 / .狠 / .骗。")
EDIT2 = ("【坠魔心劫·第2轮已定】\n你按韩立式谨慎节奏步步为营，侍妾神念与你渐趋同频。\n\n"
         "【坠魔心劫·第3轮】\n幻境再变，请继续回复 .稳 / .狠 / .骗。")
SETTLED = ("【坠魔心劫·结算】\n三轮抉择：稳 / 稳 / 稳\n你以守代攻，借势封魔，终在险境中稳稳落子。\n"
           "你以韩立式谨慎贯穿全局，收益最大。\n\n修为结算：+643\n情缘结算：+7\n心魔值结算：-5（当前 0）")
SAILING = "侍妾正在远航途中，暂不可共历心劫。"
COOLDOWN = "心劫余波未散，请在 3小时54分钟27秒 后再试。"
ANCHOR_LOST = "心劫锚点已散，需重新引动天劫。"
IN_PROGRESS = "你已有一场心劫抉择正在进行，请先完成当前抉择。"
OFFICIAL = 8980154525
ACCOUNT = 9100000001


class ClassifyTests(unittest.TestCase):
    def test_rounds_use_chinese_and_arabic_numerals(self):
        self.assertEqual(classify_trial(ANCHOR), {"status": "round", "round": 1})
        self.assertEqual(classify_trial(ANCHOR_BUFF), {"status": "round", "round": 1})
        # The regression: after the first .稳 the label is Arabic (第2轮 / 第3轮).
        self.assertEqual(classify_trial(EDIT1), {"status": "round", "round": 2})
        self.assertEqual(classify_trial(EDIT2), {"status": "round", "round": 3})

    def test_settlement_and_refusals(self):
        self.assertEqual(classify_trial(SETTLED),
                         {"status": "settled", "cultivation": 643, "affection": 7, "demon": -5, "choices": "稳 / 稳 / 稳"})
        self.assertEqual(classify_trial(SAILING)["status"], "sailing_blocked")
        self.assertEqual(classify_trial(COOLDOWN), {"status": "cooldown", "retry_minutes": 235})
        self.assertEqual(classify_trial(ANCHOR_LOST)["status"], "anchor_lost")
        self.assertEqual(classify_trial(IN_PROGRESS)["status"], "in_progress")


class FakeGroupClient:
    """Scripted topic: replies to our messages, edits the anchor in place after each .稳."""
    def __init__(self, *, trial_reply=ANCHOR, edits=(EDIT1, EDIT2, SETTLED)):
        self.trial_reply, self.edits = trial_reply, list(edits)
        self.messages: dict[int, SimpleNamespace] = {}
        self.sent = []
        self.next_id = 100

    def _add(self, text, *, sender_id, reply_to):
        self.next_id += 1
        msg = SimpleNamespace(id=self.next_id, sender_id=sender_id, fwd_from=None, message=text,
                              reply_to=SimpleNamespace(reply_to_msg_id=reply_to), reply_to_msg_id=reply_to)
        self.messages[msg.id] = msg
        return msg

    async def get_me(self):
        return SimpleNamespace(id=ACCOUNT)

    async def send_message(self, chat, text, reply_to=None):
        mine = self._add(text, sender_id=ACCOUNT, reply_to=reply_to)
        self.sent.append((text, reply_to))
        if text == ".我的侍妾":
            self._add(CARD, sender_id=OFFICIAL, reply_to=mine.id)
        elif text == ".共历心劫":
            self.anchor = self._add(self.trial_reply, sender_id=OFFICIAL, reply_to=mine.id)
        elif text == ".稳":
            anchor_id = getattr(getattr(self, "anchor", None), "id", None)
            if reply_to == anchor_id and self.edits:
                self.anchor.message = self.edits.pop(0)
            elif reply_to != anchor_id:
                self._add(ANCHOR_LOST, sender_id=OFFICIAL, reply_to=mine.id)
        return mine

    def iter_messages(self, chat, limit=40, min_id=0, from_user=None):
        rows = [m for m in self.messages.values() if m.id > min_id]
        if from_user is not None:
            rows = [m for m in rows if m.sender_id == ACCOUNT]
        async def gen():
            for m in sorted(rows, key=lambda m: m.id, reverse=True):
                yield m
        return gen()

    async def get_messages(self, chat, ids=None):
        if isinstance(ids, (list, tuple)):
            return [self.messages.get(i) for i in ids]
        return self.messages.get(ids)


class ResumeClient(FakeGroupClient):
    """A prior half-finished trial (anchor at round 2) blocks new trials until settled."""
    def __init__(self):
        super().__init__()
        self.settled_stale = False
        self.stale_edits = [EDIT2, SETTLED]
        # Seed the account's earlier .共历心劫 (id 500) and its still-open anchor (id 501 = round 2).
        self.messages[500] = SimpleNamespace(id=500, sender_id=ACCOUNT, fwd_from=None, message=".共历心劫",
                                             reply_to=SimpleNamespace(reply_to_msg_id=400), reply_to_msg_id=400)
        self.messages[501] = SimpleNamespace(id=501, sender_id=OFFICIAL, fwd_from=None, message=EDIT1,
                                             reply_to=SimpleNamespace(reply_to_msg_id=500), reply_to_msg_id=500)
        self.stale_anchor_id = 501
        self.next_id = 600

    async def send_message(self, chat, text, reply_to=None):
        mine = self._add(text, sender_id=ACCOUNT, reply_to=reply_to)
        self.sent.append((text, reply_to))
        if text == ".我的侍妾":
            self._add(CARD, sender_id=OFFICIAL, reply_to=mine.id)
        elif text == ".共历心劫":
            if not self.settled_stale:
                self._add(IN_PROGRESS, sender_id=OFFICIAL, reply_to=mine.id)
            else:
                self.anchor = self._add(ANCHOR, sender_id=OFFICIAL, reply_to=mine.id)
        elif text == ".稳":
            if reply_to == self.stale_anchor_id and not self.settled_stale:
                nxt = self.stale_edits.pop(0)
                self.messages[501].message = nxt
                if "结算" in nxt:
                    self.settled_stale = True
            elif getattr(self, "anchor", None) and reply_to == self.anchor.id and self.edits:
                self.anchor.message = self.edits.pop(0)
        return mine


class FakeClock:
    def __init__(self):
        self.value = 0.0
    def now(self):
        return self.value
    async def sleep(self, seconds):
        self.value += float(seconds)


class RunTests(unittest.IsolatedAsyncioTestCase):
    async def run_trial(self, client, name="妍丽"):
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as tmp:
            commander = HeartTrialCommander(client, state_dir=Path(tmp), sleep=clock.sleep, monotonic=clock.now,
                                           clock=clock.now)
            return await run_heart_trial(commander, companion_name=name)

    async def test_three_stable_rounds_reply_to_the_anchor_and_settle(self):
        client = FakeGroupClient()
        result = await self.run_trial(client)
        self.assertEqual(result["status"], "settled")
        self.assertEqual((result["cultivation"], result["affection"], result["rounds"]), (643, 7, 3))
        texts = [t for t, _ in client.sent]
        self.assertEqual(texts, [".我的侍妾", ".共历心劫", ".稳", ".稳", ".稳"])
        # .共历心劫 replies to the status card; every .稳 replies to the same anchor.
        self.assertEqual(client.sent[1][1], 102)
        self.assertEqual({r for _, r in client.sent[2:]}, {client.anchor.id})

    async def test_buffed_anchor_also_completes_three_rounds(self):
        client = FakeGroupClient(trial_reply=ANCHOR_BUFF)
        result = await self.run_trial(client)
        self.assertEqual(result["status"], "settled")
        self.assertEqual([t for t, _ in client.sent].count(".稳"), 3)

    async def test_refusals_are_reported_without_any_stable_reply(self):
        for text, status in ((SAILING, "sailing_blocked"), (COOLDOWN, "cooldown")):
            client = FakeGroupClient(trial_reply=text)
            result = await self.run_trial(client)
            self.assertEqual(result["status"], status)
            self.assertNotIn(".稳", [t for t, _ in client.sent])

    async def test_wrong_active_companion_never_starts_a_trial(self):
        client = FakeGroupClient()
        result = await self.run_trial(client, name="文思月")
        self.assertEqual(result["status"], "companion_mismatch")
        self.assertEqual([t for t, _ in client.sent], [".我的侍妾"])

    async def test_in_progress_resumes_the_open_anchor_then_runs_the_new_trial(self):
        client = ResumeClient()
        result = await self.run_trial(client)
        self.assertEqual(result["status"], "settled")
        # Two .稳 finish the stale trial (round 2 -> 3 -> settle), three finish the fresh one.
        self.assertEqual([t for t, _ in client.sent].count(".稳"), 5)
        self.assertIn("resumed_settlement", result)
        self.assertEqual(result["resumed_settlement"]["cultivation"], 643)
        self.assertTrue(client.settled_stale)


if __name__ == "__main__":
    unittest.main()


class DeadAnchorClient(FakeGroupClient):
    """The bot never edits; it answers the .稳 with 锚点已散 instead."""
    async def send_message(self, chat, text, reply_to=None):
        mine = self._add(text, sender_id=ACCOUNT, reply_to=reply_to)
        self.sent.append((text, reply_to))
        if text == ".我的侍妾":
            self._add(CARD, sender_id=OFFICIAL, reply_to=mine.id)
        elif text == ".共历心劫":
            self.anchor = self._add(self.trial_reply, sender_id=OFFICIAL, reply_to=mine.id)
        elif text == ".稳":
            self._add(ANCHOR_LOST, sender_id=OFFICIAL, reply_to=mine.id)
        return mine


class DeadAnchorTests(unittest.IsolatedAsyncioTestCase):
    async def test_bot_refusal_after_a_choice_stops_the_chain_immediately(self):
        client = DeadAnchorClient()
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as tmp:
            commander = HeartTrialCommander(client, state_dir=Path(tmp), sleep=clock.sleep, monotonic=clock.now,
                                           clock=clock.now)
            result = await run_heart_trial(commander, companion_name="妍丽")
        self.assertEqual(result["status"], "anchor_lost")
        self.assertEqual([t for t, _ in client.sent].count(".稳"), 1)


class QyzYieldTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_battle_window_is_checked_before_every_group_message(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from miniapp_shared.dwelling import MiniAppError
        client = FakeGroupClient()
        before = datetime(2026, 9, 21, 13, 35, 50, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as tmp:
            # The tick started just before 13:36; the window opens while the chain runs.
            commander = HeartTrialCommander(client, state_dir=Path(tmp), sleep=clock.sleep, monotonic=clock.now,
                                            clock=lambda: before + (60 if client.sent else 0))
            with self.assertRaises(MiniAppError) as caught:
                await run_heart_trial(commander, companion_name="妍丽")
        self.assertEqual("yielding_to_qyz", caught.exception.code)
        self.assertEqual([".我的侍妾"], [t for t, _ in client.sent])
