"""Offline tests built from official replies captured in the group log on 2026-09-20."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from sect_daily.protocol import (
    choose_support_action, known_true_report, parse_checkin, parse_panel, parse_publish,
    parse_report_box, parse_support,
)
from sect_daily.runner import ACCOUNT_MINUTE_OFFSETS, admitted, run_account
from sect_daily.state import read_state, write_state

CHECKIN_OK = "点卯成功！你获得了 **10** 点宗门贡献。\n你已连续点卯 **3** 天。\n**达成3日不断，额外奖励 5 点贡献！**"
CHECKIN_DONE = "今日已点卯，道友明日再来吧。"
PANEL = (
    "**【慕兰烽烟】**\n慕兰法士越过草原南下，圣灯压阵，天南边境烽火连营。\n\n**【今日军议】**\n"
    "- 天象：**阵脚微裂**，边境大阵有暗伤，护阵所得会更扎实。\n"
    "- 军议密令：**固守边境法阵**，押中可得额外军功和收益。\n"
    "- 险棋窗口：**斥候探草原**，风险上升；若成，可额外立功。\n"
    "- 粮道通畅：**夜袭法士营**，灵石收益提高。\n\n"
    "修士：@sample_peer_17\n今日状态：**未支援**\n累计边境军功：**502**\n连续支援：**36** 天\n"
    "押中密令：**16** 次\n险棋得手：**19** 次\n最近支援：**2026-09-19**\n\n**可用行动**\n"
    "- `.支援慕兰 斥候`：稳健探路，偏基础材料。\n"
)
PANEL_DONE = PANEL.replace("今日状态：**未支援**", "今日状态：**已支援**")
BOX = (
    "**【慕兰谍影】**\n日期：**2026-09-20**\n状态：**待公开**\n\n**军报匣**\n"
    "1. 圣灯已熄，只需正面冲阵便可夺灯。（未辨）\n"
    "2. 有小股法士借草沟绕行，似在寻找黄龙山外阵缺口。（未辨）\n"
    "3. 慕兰主力已退三百里，草原前线今日几乎无兵。（未辨）\n\n"
    "建议流程：先 `.辨报 <编号>`，再 `.公开军报 <编号>`。"
)
BOX_NO_TRUE = BOX.replace("有小股法士借草沟绕行，似在寻找黄龙山外阵缺口。", "南营无人防守，所有法士都在主帐议事。")
BOX_EMPTY = "**【慕兰谍影】**\n今日尚未搜集军报。\n\n命令：\n- `.搜集军报`：取得三条真假混杂的前线消息。"
PUBLISH_TRUE = ("**【慕兰谍影·真报】**\n前线采信了你的军报：有小股法士借草沟绕行，似在寻找黄龙山外阵缺口。\n\n"
                "获得边境军功 **+2**。\n今日支援【斥候探草原】时，将获得情报助力。")
PUBLISH_FALSE = "**【慕兰谍影·假报】**\n这条军报被证明是慕兰法士放出的诱饵：圣灯已熄，只需正面冲阵便可夺灯。\n\n今日支援慕兰时，前线警戒会额外提高。"
PUBLISH_ALREADY = "今日已有军报公开，前线不会重复采信。"
SUPPORT_OK = ("**【慕兰烽烟 · 斥候探草原】小胜**\n你伏在草浪中看清法士旗号，带回一份可用的前线图记。\n\n"
              "- 你选择了今日【险棋窗口】，风险上升至 **13%**。\n- 你公开过的真军报正在发挥作用，本次风险降低。\n"
              "- 险棋得手，前线记你一笔突进功。\n\n获得修为 **+297**\n获得灵石 **+212**\n获得材料 **养魂木 x1**\n\n"
              "边境军功 **+5**，累计 **498**\n连续支援 **64** 天")
SUPPORT_DONE = "**【慕兰烽烟】**\n你今日已支援过边境战线（1/1）。请待明日上海时间刷新。"


class ProtocolTests(unittest.TestCase):
    def test_checkin_replies(self):
        self.assertEqual(parse_checkin(CHECKIN_OK), {"status": "checked_in", "contribution": 10, "streak_days": 3, "stipend": None})
        self.assertEqual(parse_checkin(CHECKIN_DONE)["status"], "already_checked_in")
        self.assertEqual(parse_checkin("**你的本命元婴** 等级 49")["status"], "unknown")

    def test_panel_council_and_state(self):
        panel = parse_panel(PANEL)
        self.assertEqual(panel["status"], "not_supported")
        self.assertEqual(panel["council"], {"天象": "阵脚微裂", "军议密令": "固守边境法阵", "险棋窗口": "斥候探草原", "粮道通畅": "夜袭法士营"})
        self.assertEqual((panel["cultivator"], panel["merit"], panel["streak"]), ("sample_peer_17", "502", "36 天"))
        self.assertEqual(parse_panel(PANEL_DONE)["status"], "supported")

    def test_report_box_and_true_report(self):
        box = parse_report_box(BOX)
        self.assertEqual(box["status"], "pending")
        self.assertEqual([r["number"] for r in box["reports"]], [1, 2, 3])
        self.assertEqual(known_true_report(box["reports"]), {"number": 2, "text": "有小股法士借草沟绕行，似在寻找黄龙山外阵缺口", "route": "斥候探草原"})
        self.assertIsNone(known_true_report(parse_report_box(BOX_NO_TRUE)["reports"]))
        self.assertEqual(parse_report_box(BOX_EMPTY)["status"], "not_collected")

    def test_publish_and_support_replies(self):
        self.assertEqual(parse_publish(PUBLISH_TRUE), {"status": "true_report", "merit": 2, "route": "斥候探草原"})
        self.assertEqual(parse_publish(PUBLISH_FALSE)["status"], "false_report")
        self.assertEqual(parse_publish(PUBLISH_ALREADY)["status"], "already_published")
        self.assertEqual(parse_publish("此情报不会重复采信，但并非军报公开回执")["status"], "unknown")
        support = parse_support(SUPPORT_OK)
        self.assertEqual((support["status"], support["route"], support["outcome"]), ("supported", "斥候探草原", "小胜"))
        self.assertEqual((support["merit_gain"], support["merit_total"], support["streak_days"]), (5, 498, 64))
        self.assertTrue(support["intel_bonus"] and support["risky_success"])
        self.assertEqual(parse_support(SUPPORT_DONE)["status"], "already_supported")

    def test_support_choice_follows_operator_priority(self):
        council = parse_panel(PANEL)["council"]
        # Published true report wins even over the 军议密令.
        self.assertEqual(choose_support_action(council, "斥候探草原")["action"], "斥候")
        # Without a published report: 军议密令 > 粮道通畅 > 天象.
        self.assertEqual(choose_support_action(council)["action"], "护阵")
        self.assertEqual(choose_support_action({"粮道通畅": "夜袭法士营"})["action"], "奇袭")
        # The 险棋窗口 route is avoided unless a true report backs it.
        self.assertEqual(choose_support_action({"军议密令": "斥候探草原", "险棋窗口": "斥候探草原", "粮道通畅": "夜袭法士营"})["action"], "奇袭")
        self.assertEqual(choose_support_action({})["action"], "护阵")

    def test_admission_is_01_00_shanghai_with_three_minute_offsets(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        def ts(h, m):
            return datetime(2026, 9, 21, h, m, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        self.assertFalse(admitted("tg9100000001", ts(0, 59)))
        self.assertTrue(admitted("tg9100000001", ts(1, 0)))
        self.assertFalse(admitted("tg9100000006", ts(1, 14)))
        self.assertTrue(admitted("tg9100000006", ts(1, 15)))
        self.assertEqual(ACCOUNT_MINUTE_OFFSETS["tg9100000006"], 15)


class FakeClient:
    """Minimal Telethon stand-in: replies come from a script keyed by command text."""
    OFFICIAL = 8918867302

    def __init__(self, script, *, username="demo_player_01", user_id=9100000001):
        self.script, self.username, self.user_id = dict(script), username, user_id
        self.sent, self.replies, self.next_id = [], [], 1000

    async def get_me(self):
        return SimpleNamespace(id=self.user_id, username=self.username)

    async def send_message(self, chat, text, reply_to=None):
        self.next_id += 1
        self.sent.append(text)
        message = SimpleNamespace(id=self.next_id)
        reply_text = self.script.get(text) if text in self.script else self.script.get(text.split(" ")[0])
        if reply_text is not None:
            self.next_id += 1
            self.replies.append(SimpleNamespace(id=self.next_id, sender_id=self.OFFICIAL, fwd_from=None,
                                                message=reply_text, reply_to=SimpleNamespace(reply_to_msg_id=message.id),
                                                reply_to_msg_id=message.id))
        return message

    def iter_messages(self, chat, limit=40, min_id=0, reverse=False):
        rows = [row for row in self.replies if row.id > min_id]
        ordered = rows if reverse else list(reversed(rows))
        async def gen():
            for row in ordered[:limit]:
                yield row
        return gen()


class FakeBridge:
    def __init__(self, client):
        self.client = client
    async def __aenter__(self):
        return self
    async def __aexit__(self, *_):
        return None


class FakeClock:
    def __init__(self):
        self.value = 0.0
    def now(self):
        return self.value
    async def sleep(self, seconds):
        self.value += float(seconds)


async def no_sleep(_seconds):
    return None


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def run_day(self, root, client, *, force=True, now=1_000_000.0):
        clock = FakeClock()
        return await run_account(root, "tg9100000001", day_key="2026-09-21", now=now,
                                 bridge_factory=lambda _root, _account: FakeBridge(client), force=force,
                                 sleep=clock.sleep, monotonic=clock.now)

    async def test_full_day_publishes_true_report_and_supports_its_route(self):
        client = FakeClient({".宗门点卯": CHECKIN_OK, ".边境军功": PANEL, ".搜集军报": BOX,
                             ".公开军报 2": PUBLISH_TRUE, ".支援慕兰 斥候": SUPPORT_OK})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = await self.run_day(root, client)
            self.assertEqual(client.sent, [".宗门点卯", ".边境军功", ".搜集军报", ".公开军报 2", ".支援慕兰 斥候"])
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["support"]["route"], "斥候探草原")
            self.assertEqual(result["checkin"]["streak_days"], 3)
            self.assertEqual(read_state(root, "tg9100000001")["status"], "completed")
            # A second run the same day sends nothing.
            again = await self.run_day(root, client, force=False, now=2_000_000.0)
            self.assertEqual(again["status"], "daily_already_complete")
            self.assertEqual(len(client.sent), 5)

    async def test_no_true_report_skips_publish_and_uses_council_order(self):
        client = FakeClient({".宗门点卯": CHECKIN_DONE, ".边境军功": PANEL, ".搜集军报": BOX_NO_TRUE,
                             ".支援慕兰 护阵": SUPPORT_OK.replace("斥候探草原", "固守边境法阵")})
        with tempfile.TemporaryDirectory() as tmp:
            result = await self.run_day(Path(tmp), client)
            self.assertEqual(client.sent, [".宗门点卯", ".边境军功", ".搜集军报", ".支援慕兰 护阵"])
            self.assertEqual(result["steps"]["publish"], {"status": "skipped", "reason": "no_known_true_report",
                                                          "reports": result["steps"]["collect"]["reports"]})
            self.assertEqual(result["status"], "completed")

    async def test_panel_already_supported_sends_nothing_further(self):
        client = FakeClient({".宗门点卯": CHECKIN_DONE, ".边境军功": PANEL_DONE})
        with tempfile.TemporaryDirectory() as tmp:
            result = await self.run_day(Path(tmp), client)
            self.assertEqual(client.sent, [".宗门点卯", ".边境军功"])
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["steps"]["support"]["status"], "skipped")

    async def test_missing_reply_keeps_step_pending_and_resumes_without_resending_done_steps(self):
        client = FakeClient({".宗门点卯": CHECKIN_OK, ".边境军功": PANEL, ".搜集军报": BOX, ".公开军报 2": PUBLISH_TRUE})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = await self.run_day(root, client)
            self.assertEqual(first["status"], "partial")
            self.assertEqual(first["unresolved"], ["support"])
            self.assertEqual(first["next_due_at"], 1_000_000.0 + 15 * 60)
            client.script[".支援慕兰 斥候"] = SUPPORT_OK
            clock = FakeClock()
            second = await run_account(root, "tg9100000001", day_key="2026-09-21", now=1_000_000.0 + 16 * 60,
                                       bridge_factory=lambda _r, _a: FakeBridge(client), sleep=clock.sleep,
                                       monotonic=clock.now)
            self.assertEqual(second["status"], "completed")
            self.assertEqual(client.sent.count(".宗门点卯"), 1)
            self.assertEqual(client.sent.count(".支援慕兰 斥候"), 2)

    async def test_lost_reply_after_a_send_is_recovered_by_lookup_without_resending(self):
        """2026-09-22 @demo_player_04: the reply scan died after .公开军报 3 was sent; 39 resends followed."""
        client = FakeClient({".宗门点卯": CHECKIN_OK, ".边境军功": PANEL, ".搜集军报": BOX,
                             ".公开军报 2": PUBLISH_TRUE, ".支援慕兰 斥候": SUPPORT_OK})
        original = client.iter_messages
        armed = {"value": True}

        def flaky(chat, **kwargs):
            if armed["value"] and any(text.startswith(".公开军报") for text in client.sent):
                armed["value"] = False
                raise ValueError("Request was unsuccessful 2 time(s)")
            return original(chat, **kwargs)

        client.iter_messages = flaky
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = await self.run_day(root, client)
            self.assertEqual(first["status"], "account_failed")
            publish = read_state(root, "tg9100000001")["steps"]["publish"]
            self.assertEqual((publish["status"], publish["send_count"]), ("unconfirmed", 1))
            self.assertTrue(publish["sent_message_id"])
            self.assertTrue(publish["error"].startswith("reply_wait_failed:ValueError"))
            clock = FakeClock()
            second = await run_account(root, "tg9100000001", day_key="2026-09-21", now=1_000_000.0 + 16 * 60,
                                       bridge_factory=lambda _r, _a: FakeBridge(client), sleep=clock.sleep,
                                       monotonic=clock.now)
            self.assertEqual(client.sent.count(".公开军报 2"), 1)
            self.assertEqual(second["steps"]["publish"]["status"], "true_report")
            self.assertEqual(second["steps"]["publish"]["send_count"], 1)
            self.assertEqual(second["status"], "completed")
            self.assertEqual(second["support"]["route"], "斥候探草原")

    async def test_a_step_is_sent_at_most_twice_per_day_when_no_reply_ever_arrives(self):
        client = FakeClient({".宗门点卯": CHECKIN_OK, ".边境军功": PANEL, ".搜集军报": BOX})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = await self.run_day(root, client)
            self.assertEqual((first["status"], first["unresolved"]), ("partial", ["publish", "support"]))
            self.assertEqual(client.sent.count(".公开军报 2"), 1)
            for attempt in (1, 2, 3):
                clock = FakeClock()
                again = await run_account(root, "tg9100000001", day_key="2026-09-21",
                                          now=1_000_000.0 + attempt * 16 * 60,
                                          bridge_factory=lambda _r, _a: FakeBridge(client), sleep=clock.sleep,
                                          monotonic=clock.now)
                self.assertEqual(again["status"], "partial")
            self.assertEqual(client.sent.count(".公开军报 2"), 2)
            # Without a confirmed true report the council picks 护阵; it is capped too.
            self.assertEqual(client.sent.count(".支援慕兰 护阵"), 2)
            publish = read_state(root, "tg9100000001")["steps"]["publish"]
            self.assertEqual((publish["status"], publish["error"], publish["send_count"]),
                             ("unconfirmed", "resend_limit_reached", 2))

    async def test_a_send_that_returns_no_message_id_still_counts_toward_the_cap(self):
        client = FakeClient({".宗门点卯": CHECKIN_OK, ".边境军功": PANEL, ".搜集军报": BOX})
        original = client.send_message

        async def no_id(chat, text, reply_to=None):
            if text.startswith(".公开军报"):
                client.sent.append(text)
                return SimpleNamespace(id=0)
            return await original(chat, text, reply_to=reply_to)

        client.send_message = no_id
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            await self.run_day(root, client)
            for attempt in (1, 2, 3):
                clock = FakeClock()
                await run_account(root, "tg9100000001", day_key="2026-09-21", now=1_000_000.0 + attempt * 16 * 60,
                                  bridge_factory=lambda _r, _a: FakeBridge(client), sleep=clock.sleep,
                                  monotonic=clock.now)
            self.assertEqual(client.sent.count(".公开军报 2"), 2)
            publish = read_state(root, "tg9100000001")["steps"]["publish"]
            self.assertEqual((publish["error"], publish["send_count"]), ("resend_limit_reached", 2))

    async def test_a_send_that_raises_still_counts_toward_the_cap(self):
        client = FakeClient({".宗门点卯": CHECKIN_OK, ".边境军功": PANEL, ".搜集军报": BOX})
        original = client.send_message

        async def boom(chat, text, reply_to=None):
            if text.startswith(".公开军报"):
                client.sent.append(text)
                raise ValueError("Request was unsuccessful 2 time(s)")
            return await original(chat, text, reply_to=reply_to)

        client.send_message = boom
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = await self.run_day(root, client)
            self.assertEqual(first["status"], "account_failed")
            for attempt in (1, 2, 3):
                clock = FakeClock()
                await run_account(root, "tg9100000001", day_key="2026-09-21", now=1_000_000.0 + attempt * 16 * 60,
                                  bridge_factory=lambda _r, _a: FakeBridge(client), sleep=clock.sleep,
                                  monotonic=clock.now)
            self.assertEqual(client.sent.count(".公开军报 2"), 2)
            self.assertEqual(read_state(root, "tg9100000001")["steps"]["publish"]["send_count"], 2)

    async def test_cached_already_published_reply_is_reconciled_without_resend(self):
        client = FakeClient({})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_state(root, "tg9100000001", {
                "account": "tg9100000001",
                "day": "2026-09-21",
                "status": "partial",
                "next_due_at": 0,
                "steps": {
                    "checkin": {"status": "already_checked_in"},
                    "panel": {"status": "not_supported", "council": {}},
                    "collect": {"status": "pending", "reports": [
                        {"number": 2, "text": "有小股法士借草沟绕行，似在寻找黄龙山外阵缺口"}
                    ]},
                    "publish": {"status": "unknown", "reply_text": PUBLISH_ALREADY,
                                "sent_message_id": 12452058, "reply_message_id": 12452060},
                    "support": {"status": "already_supported"},
                },
            })
            clock = FakeClock()
            result = await run_account(
                root,
                "tg9100000001",
                day_key="2026-09-21",
                now=1_000_000.0,
                bridge_factory=lambda _r, _a: FakeBridge(client),
                sleep=clock.sleep,
                monotonic=clock.now,
            )
            self.assertEqual("completed", result["status"])
            self.assertEqual("already_published", result["steps"]["publish"]["status"])
            self.assertEqual([], client.sent)
            persisted = read_state(root, "tg9100000001")
            self.assertEqual("completed", persisted["status"])
            self.assertEqual(12452058, persisted["steps"]["publish"]["sent_message_id"])
            self.assertEqual(12452060, persisted["steps"]["publish"]["reply_message_id"])

            def forbidden_bridge(*_args):
                raise AssertionError("completed day must not open Telegram")

            again = await run_account(
                root,
                "tg9100000001",
                day_key="2026-09-21",
                now=1_000_001.0,
                bridge_factory=forbidden_bridge,
                sleep=clock.sleep,
                monotonic=clock.now,
            )
            self.assertEqual("daily_already_complete", again["status"])
            self.assertEqual([], client.sent)

    async def test_identity_mismatch_never_sends(self):
        client = FakeClient({".宗门点卯": CHECKIN_OK}, username="sample_peer_12_else")
        with tempfile.TemporaryDirectory() as tmp:
            result = await self.run_day(Path(tmp), client)
            self.assertEqual(result["status"], "account_failed")
            self.assertEqual(client.sent, [])


class ReplyPollingTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_missing_reply_is_read_with_growing_gaps_until_the_deadline(self):
        from sect_daily.runner import GroupCommander
        client = FakeClient({})
        reads = []
        original = client.iter_messages

        def counted(chat, **kwargs):
            reads.append(clock.value)
            self.assertEqual((1001, 60, True), (kwargs["min_id"], kwargs["limit"], kwargs["reverse"]))
            return original(chat, **kwargs)

        client.iter_messages = counted
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as tmp:
            commander = GroupCommander(client, state_dir=Path(tmp), sleep=clock.sleep, monotonic=clock.now)
            self.assertIsNone(await commander.wait_reply(1001, timeout=40))
        # 2, 3, 5 then 8 s apart, the last read exactly at the deadline (was every 3 s: 15 reads).
        self.assertEqual([0, 2, 5, 10, 18, 26, 34, 40], reads)


class NotificationTests(unittest.TestCase):
    def test_summary_lists_each_account(self):
        from miniapp_shared.summaries import sect_notification
        summary = {"day": "2026-09-21", "accounts": [
            {"account": "tg9100000001", "status": "completed",
             "checkin": {"status": "checked_in", "contribution": 10, "streak_days": 3},
             "steps": {"publish": {"status": "true_report", "merit": 2}},
             "support": {"status": "supported", "route": "斥候探草原", "outcome": "小胜", "merit_gain": 5,
                         "merit_total": 498, "streak_days": 64, "matched_order": False}},
            {"account": "tg9100000002", "status": "partial", "error": "no_official_reply"}]}
        self.assertIsNone(sect_notification(summary, now_ts=1789920000.0))  # 00:00, not day end
        key, text = sect_notification({**summary, "accounts": summary["accounts"][:1]}, now_ts=1789920000.0)
        self.assertEqual(key, "2026-09-21:complete")
        self.assertIn("@demo_player_01: 点卯+10贡献 连续3天，真报+2军功，支援斥候探草原 小胜 军功+5（累计498，连续64天）", text)


if __name__ == "__main__":
    unittest.main()
