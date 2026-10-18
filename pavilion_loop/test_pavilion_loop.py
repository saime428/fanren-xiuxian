"""Offline tests built from 天机阁 replies captured in the MiniApp on 2026-09-20/21."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pavilion_loop.protocol import choose_route, classify, companions_from_payload, parse_iso, parse_minutes
from pavilion_loop.runner import run_account

RETURNED = ("**【乱星海远航·归】**\n侍妾【绿袖】已自 **均衡** 航线归来，向你呈上收获：\n- 修为 **+440**\n- 灵石 **+109**\n"
            "- 二级妖丹 x1\n- 阴魂丝 x3\n- 侍妾额外为你蓄灵 **3** 点\n此行顺遂，侍妾对你更添信重，情缘增加 **6** 点。")
DIVINED = "**【天机代卜链】**\n侍妾【绿袖】焚香推演，为你接引一缕天机。\n得卦【避劫星芒】：下一次闭关将获奇遇+8%，走火率-2%。\n本次消耗：**180修为**。"
SEEK_MISS = ("**【入梦寻图】**\n本次梦兆锁定：**【虚天残图】** 线路。\n你与侍妾【绿袖】共入迷梦，终只见海雾翻腾，未觅得【虚天残图】碎片。\n"
             "本次掉落率：**22%**（进度衰减 -12%，当前 虚天残图 2/4）。")
SEEK_HIT = ("**【入梦寻图】**\n本次梦兆锁定：**【虚天残图】** 线路。\n你与侍妾【凌玉灵】共梦乱星海，获得 **【虚天残图】** 残纹 **南渊残纹**（新残纹）。\n"
            "当前进度：**1/4**。\n可用 `.残图` 查看详情，集齐后使用 `.拼图` 触发对应副本机缘。")
PROTECTED = "**【坠魔心劫】**\n侍妾【霓裳】替你先承一缕魔劫，护持已成。\n下次你参与坠魔谷时，所在队伍将获得：魔染护持 **18**、封印起始 **+12**、士气起始 **+20**。\n本次成功率：**84%**。"
WEAK = "**【坠魔心劫】**\n侍妾【温天心】强行承劫未竟全功，仅留下残痕护持。\n本次成功率：**70%**。\n本次为红尘道侣路径，未消耗情缘。"
ZHUIMO_CD = "心劫余波未散，请在 **11小时52分钟45秒** 后再试。"
SAIL = "**【乱星海远航·启】**\n你命侍妾【绿袖】沿 **均衡** 航线远行。\n消耗：**360修为**、**45灵石**。\n预计归航时间：**8小时** 后。\n可用 `.远航状态` 查看进度，归航后使用 `.远航归来` 结算。"
SAILING_BLOCK = "侍妾正在远航途中，暂无法焚香代卜。"
VOYAGE_CD = "远航尚未结束，请在 **1小时58分钟52秒** 后再来接引。"
PUZZLE_INCOMPLETE = "虚天残图仍缺：**南渊残纹、东离残纹**。\n苍坤残图仍缺：**禁门残纹**。\n请继续使用 `.入梦寻图`。"
RECALLED = "你已将【南宫婉·月影】召至身边，随你一同历练。情缘、神通与冷却均保持不变。"
AFFECTION_LOW = "侍妾心神未定，此航线至少需要 **70** 情缘值。"
PROTECTION_ACTIVE = "坠魔心劫护持尚在，无需重复施展（剩余 **7小时36分钟50秒**）。"
SAIL_SAFE = SAIL.replace("均衡", "稳妥").replace("8小时", "4小时").replace("360修为", "200修为").replace("45灵石", "20灵石")


def payload(companions):
    return {"dwelling": {"companions": companions}}


def comp(name, *, active, last_seek=None, last_div=None, last_zhuimo=None, voyage=None, xutian=None):
    raw = {"affection": 100}
    if last_seek: raw["last_dream_map_seek_time"] = last_seek
    if last_div: raw["last_divination_chain_time"] = last_div
    if last_zhuimo: raw["last_zhuimo_trial_time"] = last_zhuimo
    if voyage: raw["voyage"] = voyage
    if xutian: raw["xutian_fragment_bag"] = xutian
    return {"name": name, "active": active, "status": "随行中" if active else "居于藏娇阁", "raw": raw}


class ProtocolTests(unittest.TestCase):
    def test_reply_classification(self):
        self.assertEqual(classify(".远航归来", RETURNED), {"status": "returned", "cultivation": 440, "spirit_stones": 109,
                                                        "affection_delta": 6, "items": ["二级妖丹 x1", "阴魂丝 x3"]})
        self.assertEqual(classify(".远航归来", VOYAGE_CD), {"status": "cooldown", "retry_minutes": 119})
        self.assertEqual(classify(".天机代卜", DIVINED), {"status": "divined", "buff": "避劫星芒"})
        self.assertEqual(classify(".天机代卜", SAILING_BLOCK)["status"], "sailing_blocked")
        self.assertEqual(classify(".入梦寻图", SEEK_MISS), {"status": "missed", "chart": "", "fragment": "", "new": False, "progress": None, "rate": 22})
        self.assertEqual(classify(".入梦寻图", SEEK_HIT), {"status": "found", "chart": "虚天残图", "fragment": "南渊残纹", "new": True, "progress": 1, "rate": None})
        self.assertEqual(classify(".坠魔心劫", PROTECTED), {"status": "protected", "rate": 84})
        self.assertEqual(classify(".坠魔心劫", WEAK), {"status": "weak_protection", "rate": 70})
        self.assertEqual(classify(".坠魔心劫", ZHUIMO_CD), {"status": "cooldown", "retry_minutes": 713})
        self.assertEqual(classify(".侍妾远航 均衡", SAIL), {"status": "sailing", "hours": 8})
        self.assertEqual(classify(".拼图", PUZZLE_INCOMPLETE)["status"], "incomplete")
        self.assertEqual(classify(".侍妾远航 均衡", AFFECTION_LOW), {"status": "affection_too_low", "required_affection": 70})
        self.assertEqual(classify(".坠魔心劫", PROTECTION_ACTIVE), {"status": "protection_active", "retry_minutes": 457})
        self.assertEqual(classify(".召回侍妾 南宫婉·月影", RECALLED)["status"], "recalled")
        self.assertEqual(parse_minutes("136分钟"), 136)

    def test_companion_timers_and_route(self):
        rows = companions_from_payload(payload([
            comp("霓裳", active=True, last_seek="2026-09-20T10:50:39+00:00", last_div="2026-09-20T01:22:29+00:00",
                 voyage={"end_time": "2026-09-20T18:50:46+00:00", "route": "均衡", "status": "sailing"},
                 xutian={"xutian_chart_east": 1, "xutian_chart_north": 1, "xutian_chart_south": 2, "xutian_chart_west": 1}),
            comp("南宫婉·月影", active=False),
        ]), now_ts=0)
        first = rows[0]
        self.assertTrue(first["sailing"])
        self.assertEqual(first["voyage_end_ts"], parse_iso("2026-09-20T18:50:46+00:00"))
        self.assertEqual(first["dream_map_ready_ts"], parse_iso("2026-09-20T10:50:39+00:00") + 480 * 60)
        self.assertEqual(first["divination_ready_ts"], parse_iso("2026-09-20T01:22:29+00:00") + 720 * 60)
        self.assertEqual(first["fragments"], {"虚天": 4, "苍坤": 0})
        self.assertTrue(first["puzzle_ready"])
        self.assertEqual(choose_route(rows[1]), "月殿寻痕")
        self.assertEqual(choose_route(first), "均衡")
        self.assertEqual(choose_route(first, {"霓裳": "冒险"}), "冒险")
        # A fresh companion below the 均衡 floor sails 稳妥 until 情缘 reaches 70.
        self.assertEqual(choose_route({"name": "妍丽", "affection": 7}), "")  # below the 30 needed even for 稳妥
        self.assertEqual(choose_route({"name": "妍丽", "affection": 30}), "稳妥")
        self.assertEqual(choose_route({"name": "妍丽", "affection": 70}), "均衡")


class FakeTransport:
    """Scripted 天机阁: replies by command; tracks the active companion and timers."""
    def __init__(self, companions, replies):
        self.companions, self.replies, self.calls = companions, dict(replies), []

    async def __call__(self, path, body, *, mutation):
        command = body["command"]
        self.calls.append(command)
        text = self.replies.get(command, self.replies.get(command.split(" ")[0], "天机阁无回响。"))
        if command.startswith(".召回侍妾 "):
            target = command.split(" ", 1)[1]
            for c in self.companions:
                c["active"] = c["name"] == target
                c["status"] = "随行中" if c["active"] else "居于藏娇阁"
            text = RECALLED.replace("南宫婉·月影", target)
        active = next(c for c in self.companions if c["active"])
        if command == ".远航归来" and "归】" in text:
            active["raw"].pop("voyage", None)
        if command.startswith(".侍妾远航") and "启】" in text:
            active["raw"]["voyage"] = {"end_time": "2026-09-21T08:00:00+00:00", "route": "均衡", "status": "sailing"}
        if command == ".天机代卜" and "代卜链" in text:
            active["raw"]["last_divination_chain_time"] = "2026-09-21T00:00:00+00:00"
        return {"ok": True, "actionResult": {"ok": True, "rawMessage": text}, "dwelling": {"companions": self.companions}}


class FakeBridge:
    def __init__(self, transport):
        self.client = SimpleNamespace(get_me=self._me)
        self._entry_protocol = SimpleNamespace(_pinned_cave_entry=self._entry, _cave_webview=self._webview)
    async def _me(self):
        return SimpleNamespace(username="demo_player_01", id=9100000001)
    async def _entry(self, _client):
        return {"token": "df_fixture"}
    async def _webview(self, _client, _entry):
        return "fixture_init"
    async def __aenter__(self):
        return self
    async def __aexit__(self, *_):
        return None


async def no_sleep(_s):
    return None


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def run_loop(self, transport, *, now, force=True, root=None):
        if root is None:
            self._tmp = tempfile.TemporaryDirectory()
            root = Path(self._tmp.name)
        import miniapp_shared.dwelling as dwelling
        original = dwelling.open_dwelling
        import pavilion_loop.runner as runner
        original_heart = runner.HeartTrialCommander
        runner.HeartTrialCommander = lambda *a, **k: None  # runner tests cover the in-app chain only
        async def fake_open(bridge, t):
            return SimpleNamespace(start_payload={"dwelling": {"companions": transport.companions}},
                                   base_payload=lambda player_id=None: {"token": "df_fixture", "initData": "fixture_init"},
                                   identities=(), account="tg9100000001")
        runner.open_dwelling = fake_open
        try:
            r = Path(root)
            return await run_account(r, "tg9100000001", now=now, bridge_factory=lambda _r, _a: FakeBridge(transport),
                                     transport=transport, force=force, sleep=no_sleep), r
        finally:
            runner.open_dwelling = original
            runner.HeartTrialCommander = original_heart

    async def test_full_cycle_runs_both_companions_and_restores_the_active_one(self):
        companions = [
            comp("红莲", active=True, last_seek="2026-09-20T10:53:08+00:00", last_div="2026-09-20T01:29:37+00:00",
                 voyage={"end_time": "2026-09-20T18:00:00+00:00", "route": "均衡", "status": "sailing"}),
            comp("南宫婉·月影", active=False, last_seek="2026-09-20T10:52:22+00:00", last_div="2026-09-20T01:30:39+00:00",
                 last_zhuimo="2026-09-19T03:06:33+00:00"),
        ]
        replies = {".远航归来": RETURNED, ".天机代卜": DIVINED, ".入梦寻图": SEEK_MISS, ".坠魔心劫": WEAK, ".侍妾远航": SAIL}
        transport = FakeTransport(companions, replies)
        now = parse_iso("2026-09-20T21:00:00+00:00")  # 寻图 (10:53Z + 8h) and 代卜 (01:29Z + 12h) due, voyage ended 18:00Z
        result, _ = await self.run_loop(transport, now=now)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(transport.calls, [
            ".远航归来", ".天机代卜", ".入梦寻图", ".坠魔心劫", ".侍妾远航 均衡",
            ".召回侍妾 南宫婉·月影", ".天机代卜", ".入梦寻图", ".坠魔心劫", ".侍妾远航 月殿寻痕",
            ".召回侍妾 红莲"])
        self.assertEqual([i["status"] for i in result["identities"]], ["cycled", "cycled"])
        self.assertEqual(result["active_restored"], "红莲")
        # Both companions sail for 8 h; the tick re-checks after at most 4 h anyway.
        self.assertAlmostEqual(result["next_due_at"], now + 4 * 3600, delta=1)

    async def test_sailing_companion_is_only_settled_when_the_voyage_has_ended(self):
        companions = [comp("绿袖", active=True, voyage={"end_time": "2026-09-21T00:44:58+00:00", "route": "均衡", "status": "sailing"}),
                      comp("咸鱼", active=False, voyage={"end_time": "2026-09-21T00:44:58+00:00", "route": "均衡", "status": "sailing"})]
        transport = FakeTransport(companions, {})
        now = parse_iso("2026-09-20T23:00:00+00:00")
        result, root = await self.run_loop(transport, now=now)
        self.assertEqual(transport.calls, [".召回侍妾 咸鱼", ".召回侍妾 绿袖"])
        self.assertEqual([i["status"] for i in result["identities"]], ["sailing", "sailing"])
        self.assertAlmostEqual(result["next_due_at"], parse_iso("2026-09-21T00:44:58+00:00") + 20, delta=1)
        # A scheduled wait never opens a session.
        later, _ = await self.run_loop(transport, now=now + 600, force=False, root=root)
        self.assertEqual(later["status"], "scheduled_wait")

    async def test_cooldown_refusal_schedules_the_server_wait(self):
        companions = [comp("红莲", active=True, last_zhuimo="2026-09-20T16:42:11+00:00"),
                      comp("南宫婉·月影", active=False, last_seek="2026-09-21T00:00:00+00:00",
                           last_div="2026-09-21T00:00:00+00:00", last_zhuimo="2026-09-21T00:00:00+00:00")]
        # local timer says 坠魔 is due for 温天心 but the server still refuses
        replies = {".天机代卜": DIVINED, ".入梦寻图": SEEK_HIT, ".坠魔心劫": ZHUIMO_CD, ".侍妾远航": SAIL}
        transport = FakeTransport(companions, replies)
        result, _ = await self.run_loop(transport, now=parse_iso("2026-09-21T06:00:00+00:00"))
        first = result["identities"][0]
        zhuimo = next(a for a in first["actions"] if a["command"] == ".坠魔心劫")
        self.assertEqual((zhuimo["status"], zhuimo["retry_minutes"]), ("cooldown", 713))
        self.assertEqual(first["status"], "cycled")
        self.assertNotIn(".拼图", transport.calls)


class AffectionFloorTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_floor_refusal_falls_back_to_the_safe_route_once(self):
        # affection looks fine locally (100) but the server still refuses 均衡.
        companions = [comp("红莲", active=True, last_seek="2026-09-21T00:00:00+00:00", last_div="2026-09-21T00:00:00+00:00",
                           last_zhuimo="2026-09-21T00:00:00+00:00"),
                      comp("温天心", active=False, voyage={"end_time": "2026-09-21T02:47:03+00:00", "route": "均衡", "status": "sailing"})]
        transport = FakeTransport(companions, {".侍妾远航 均衡": AFFECTION_LOW, ".侍妾远航 稳妥": SAIL_SAFE})
        result, _ = await RunnerTests.run_loop(RunnerTests(), transport, now=parse_iso("2026-09-21T01:00:00+00:00"))
        self.assertEqual(transport.calls, [".侍妾远航 均衡", ".侍妾远航 稳妥", ".召回侍妾 温天心", ".召回侍妾 红莲"])
        first = result["identities"][0]
        self.assertEqual((first["status"], first["route"]), ("cycled", "稳妥"))


class PinnedRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_demo_player_01_nangongwan_always_sails_the_moon_route(self):
        from pavilion_loop.state import ROUTE_OVERRIDES
        self.assertEqual(ROUTE_OVERRIDES["tg9100000001"]["南宫婉·月影"], "月殿寻痕")
        self.assertEqual(choose_route({"name": "南宫婉·月影", "affection": 0}, ROUTE_OVERRIDES["tg9100000001"]), "月殿寻痕")
        companions = [comp("南宫婉·月影", active=True, last_seek="2026-09-21T00:00:00+00:00",
                           last_div="2026-09-21T00:00:00+00:00", last_zhuimo="2026-09-21T00:00:00+00:00"),
                      comp("温天心", active=False, voyage={"end_time": "2026-09-21T09:00:00+00:00", "route": "稳妥", "status": "sailing"})]
        # Even a floor refusal must not turn 月殿寻痕 into 稳妥 for the pinned companion.
        transport = FakeTransport(companions, {".侍妾远航 月殿寻痕": AFFECTION_LOW})
        result, _ = await RunnerTests.run_loop(RunnerTests(), transport, now=parse_iso("2026-09-21T01:00:00+00:00"))
        self.assertEqual([c for c in transport.calls if c.startswith(".侍妾远航")], [".侍妾远航 月殿寻痕"])
        self.assertEqual(result["identities"][0]["route"], "月殿寻痕")


class OperatorNoSailTests(unittest.IsolatedAsyncioTestCase):
    async def test_demo_player_01_wentianxin_resumes_voyages_after_the_other_due_steps(self):
        from pavilion_loop.state import NO_SAIL, sailing_disabled
        self.assertNotIn("温天心", NO_SAIL.get("tg9100000001", frozenset()))
        self.assertFalse(sailing_disabled("tg9100000001", "温天心"))
        self.assertFalse(sailing_disabled("tg9100000002", "温天心"))  # other accounts' 温天心 still sail
        companions = [comp("温天心", active=True, last_seek="2026-09-20T00:00:00+00:00", last_div="2026-09-20T00:00:00+00:00",
                           last_zhuimo="2026-09-20T00:00:00+00:00"),
                      comp("南宫婉·月影", active=False, voyage={"end_time": "2026-09-21T09:00:00+00:00", "route": "月殿寻痕", "status": "sailing"})]
        transport = FakeTransport(companions, {".天机代卜": DIVINED, ".入梦寻图": SEEK_MISS, ".坠魔心劫": WEAK, ".侍妾远航": SAIL})
        result, _ = await RunnerTests.run_loop(RunnerTests(), transport, now=parse_iso("2026-09-21T01:00:00+00:00"))
        self.assertEqual([c for c in transport.calls if c.startswith(".侍妾远航")], [".侍妾远航 均衡"])
        self.assertEqual(transport.calls[:3], [".天机代卜", ".入梦寻图", ".坠魔心劫"])
        first = result["identities"][0]
        self.assertEqual((first["identity"], first["status"]), ("温天心", "cycled"))
        self.assertEqual(first["route"], "均衡")
        self.assertGreater(first["next_due_at"], parse_iso("2026-09-21T01:00:00+00:00"))

    async def test_a_voyage_already_under_way_is_still_settled_and_then_not_renewed(self):
        from unittest.mock import patch
        companions = [comp("温天心", active=True, last_seek="2026-09-20T00:00:00+00:00", last_div="2026-09-20T00:00:00+00:00",
                           last_zhuimo="2026-09-20T00:00:00+00:00",
                           voyage={"end_time": "2026-09-21T00:30:00+00:00", "route": "稳妥", "status": "sailing"}),
                      comp("南宫婉·月影", active=False, voyage={"end_time": "2026-09-21T09:00:00+00:00", "route": "月殿寻痕", "status": "sailing"})]
        transport = FakeTransport(companions, {".远航归来": RETURNED, ".天机代卜": DIVINED, ".入梦寻图": SEEK_MISS, ".坠魔心劫": WEAK})
        with patch.dict("pavilion_loop.state.NO_SAIL", {"tg9100000001": frozenset({"温天心"})}):
            result, _ = await RunnerTests.run_loop(RunnerTests(), transport, now=parse_iso("2026-09-21T01:00:00+00:00"))
        self.assertEqual(transport.calls[0], ".远航归来")
        self.assertEqual(next(a["status"] for a in result["identities"][0]["actions"] if a["command"] == ".远航归来"), "returned")
        self.assertEqual([c for c in transport.calls if c.startswith(".侍妾远航")], [])
        self.assertEqual(result["identities"][0]["status"], "voyage_disabled_by_operator")


class NoSailTests(unittest.IsolatedAsyncioTestCase):
    async def test_companion_below_thirty_affection_does_not_try_to_sail(self):
        companions = [comp("红莲", active=True, last_seek="2026-09-21T00:00:00+00:00", last_div="2026-09-21T00:00:00+00:00",
                           last_zhuimo="2026-09-21T00:00:00+00:00"),
                      comp("温天心", active=False, voyage={"end_time": "2026-09-21T02:47:03+00:00", "route": "均衡", "status": "sailing"})]
        companions[0]["raw"]["affection"] = 21
        transport = FakeTransport(companions, {})
        result, _ = await RunnerTests.run_loop(RunnerTests(), transport, now=parse_iso("2026-09-21T01:00:00+00:00"))
        self.assertEqual([c for c in transport.calls if c.startswith(".侍妾远航")], [])
        self.assertEqual(result["identities"][0]["status"], "affection_too_low_to_sail")


class HeartTrialBackoffTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_trials_back_off_reset_on_settlement_and_stop_at_the_daily_cap(self):
        from unittest.mock import patch
        import pavilion_loop.runner as runner
        # 红莲 cannot sail (情缘 21) and every in-app timer is fresh: only 共历心劫 is due on each tick.
        companions = [comp("红莲", active=True, last_seek="2026-09-21T00:00:00+00:00", last_div="2026-09-21T00:00:00+00:00",
                           last_zhuimo="2026-09-21T00:00:00+00:00"),
                      comp("温天心", active=False, voyage={"end_time": "2026-09-22T00:00:00+00:00", "route": "均衡", "status": "sailing"})]
        companions[0]["raw"]["affection"] = 21
        transport = FakeTransport(companions, {})
        outcomes = iter(["unconfirmed", "unconfirmed", "settled"] + ["unconfirmed"] * 40)
        start = now = parse_iso("2026-09-21T01:00:00+00:00")  # 09:00 Asia/Shanghai; ten hours stay on one day
        started = []

        async def fake_trial(_commander, *, companion_name):
            started.append((companion_name, int(now - start) // 60))
            return {"status": next(outcomes)}

        async def fake_open(_bridge, _transport):
            return SimpleNamespace(start_payload={"dwelling": {"companions": transport.companions}},
                                   base_payload=lambda player_id=None: {"token": "df_fixture", "initData": "fixture_init"},
                                   identities=(), account="tg9100000001")

        with tempfile.TemporaryDirectory() as tmp, patch.object(runner, "run_heart_trial", fake_trial),                 patch.object(runner, "open_dwelling", fake_open),                 patch.object(runner, "HeartTrialCommander", lambda *a, **k: object()):
            for step in range(40):  # a forced tick every 15 minutes for ten hours
                now = start + step * 15 * 60
                await run_account(Path(tmp), "tg9100000001", now=now, transport=transport, force=True,
                                  bridge_factory=lambda _r, _a: FakeBridge(transport), sleep=no_sleep)
        # 15 then 30 min of backoff; the settled trial resets it; then 15/30 min again and the
        # sixth start of the day is the last one.
        self.assertEqual([("红莲", minute) for minute in (0, 15, 45, 60, 75, 105)], started)


class NotificationTests(unittest.TestCase):
    def test_batch_summary(self):
        from miniapp_shared.summaries import pavilion_notification
        summary = {"accounts": [{"account": "tg9100000004", "status": "completed", "updated_at": 1.0, "identities": [
            {"identity": "绿袖", "actions": [
                {"command": ".远航归来", "status": "returned", "cultivation": 440, "spirit_stones": 109},
                {"command": ".天机代卜", "status": "divined", "buff": "避劫星芒"},
                {"command": ".入梦寻图", "status": "missed"},
                {"command": ".侍妾远航 均衡", "status": "sailing", "hours": 8}]}]},
            {"account": "tg9100000001", "status": "scheduled_wait", "identities": []}]}
        key, text = pavilion_notification(summary, now_ts=1789920000.0)
        self.assertTrue(key.startswith("2026-09-21:"))
        self.assertIn("@demo_player_04 绿袖: 归航+440修为/+109灵石，代卜(避劫星芒)，寻图✗，远航(均衡 8h)", text)
        self.assertIsNone(pavilion_notification({"accounts": [{"account": "tg9100000001", "status": "scheduled_wait", "identities": []}]}, now_ts=1.0))
        # An idle tick that only echoes last cycle's actions must not re-send.
        idle = {"accounts": [{**summary["accounts"][0], "status": "scheduled_wait"}]}
        self.assertIsNone(pavilion_notification(idle, now_ts=1789920000.0))


if __name__ == "__main__":
    unittest.main()
