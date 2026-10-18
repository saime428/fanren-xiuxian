"""Dynamic window timing follows server reveals and arrived feedback only."""
import asyncio
import json
import unittest
from types import SimpleNamespace

from test_qyz_protocol import FakeClock
from qyz_protocol import ProtocolError, ProtocolRuntime, WebViewAuth
from qyz_reference.compat import MiniAppBeastError
from qyz_reference.world_boss_features import WorldBossEntry, WorldBossMonitor


def fixture_entry():
    return WorldBossEntry(1, 1, "fixture", "https://asc.aiopenai.app", "a" * 64, "fixture")


class IrregularBattleFixture:
    """Hidden reveal schedule; verdicts use server time and minted ticket age."""
    account = "tg9100000001"
    centers = (2200, 5140, 14900, 18240, 26010, 30620, 34990, 41240,
               45110, 53180, 58010, 61740, 68130, 73720, 78510, 86070)

    def __init__(self, clock, reveal_ahead_ms=1250):
        self.clock, self.reveal_ahead_ms = clock, reveal_ahead_ms
        self.started_at = None
        self.windows = [{"id": f"irregular-{i}", "centerMs": center, "hitMs": 620, "perfectMs": 210}
                        for i, center in enumerate(self.centers, 1)]
        self.revealed, self.tickets, self.releases, self.hits = set(), {}, {}, {}
        self.window_starts, self.calls = [], []

    async def post_json(self, endpoint, body, *, timeout=8):
        self.calls.append(endpoint)
        sent = self.clock.now()
        if endpoint == "window":
            self.window_starts.append(sent)
        await self.clock.sleep(.150)
        if endpoint == "start":
            data = {"ok": True, "sessionToken": "fixture-session", "boss": {"roomStatus": "battle", "actionLimit": 1},
                    "player": {"maxHp": 100}, "challenge": {"challengeId": "fixture-challenge", "windowCount": 16,
                    "windows": [], "durationMs": 100000}}
        elif endpoint == "begin":
            self.started_at = self.clock.now() + .500
            data = {"ok": True, "startsInMs": 500}
        elif endpoint == "window":
            after = body["afterWindowId"]
            index = next((i + 1 for i, w in enumerate(self.windows) if w["id"] == after), 0)
            window = self.windows[index] if index < 16 else None
            if window and (self.clock.now() - self.started_at) * 1000 < window["centerMs"] - self.reveal_ahead_ms:
                window = None
            if window:
                self.revealed.add(window["id"])
            data = {"ok": True, "window": window, "windowCount": 16, "done": len(self.revealed) == 16}
        elif endpoint == "charge-start":
            wid = body["windowId"]
            assert wid in self.revealed and wid not in self.tickets
            self.tickets[wid] = self.clock.now()
            data = {"ok": True, "chargeTicket": "fixture-ticket-" + wid}
        elif endpoint == "hit":
            wid = body["windowId"]
            assert wid in self.tickets and wid not in self.releases
            self.releases[wid] = sent
            window = next(w for w in self.windows if w["id"] == wid)
            hold = (self.clock.now() - self.tickets[wid]) * 1000
            delta = abs((self.clock.now() - self.started_at) * 1000 - window["centerMs"])
            self.hits[wid] = {"windowId": wid, "perfect": 520 <= hold <= 1250 and delta <= window["perfectMs"],
                              "holdMs": hold, "deltaMs": delta, "damageYi": 1}
            data = {"ok": True, "hit": self.hits[wid]}
        elif endpoint == "finish":
            data = {"ok": True, "result": {"grade": "甲等", "score": 100, "player_hp": 100}}
        else:
            raise AssertionError(endpoint)
        await self.clock.sleep(.150)
        return data


class DynamicWindowTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_twenty_second_stall_keeps_the_cursor_until_server_done(self):
        clock = FakeClock()
        cursors = []

        async def request(_origin, _path, body, _timeout):
            cursors.append(body["afterWindowId"])
            index = len(cursors)
            return {
                "ok": True,
                "window": {
                    "id": f"stall-{index}",
                    "centerMs": index * 30000,
                    "hitMs": 620,
                    "perfectMs": 210,
                },
                "windowCount": 2,
                "done": index == 2,
            }

        monitor = WorldBossMonitor(
            SimpleNamespace(client=None, config={}, state={}),
            "tg9100000001",
            post_json=request,
            monotonic=clock.now,
            sleep=clock.sleep,
        )
        original_sleep = monitor.sleep

        async def stalled_sleep(seconds):
            if len(cursors) == 1:
                clock.value += 21
            else:
                await original_sleep(seconds)

        monitor.sleep = stalled_sleep
        queue, reveal = asyncio.Queue(), []
        try:
            await monitor._reveal_windows(
                fixture_entry(), "fixture", "fixture", "fixture",
                clock.now(), queue, reveal, 2,
            )
        finally:
            await monitor.stop()

        self.assertEqual(cursors, ["", "stall-1"])
        self.assertEqual(
            [row["window_id"] for row in reveal if row["status"] == "revealed"],
            ["stall-1", "stall-2"],
        )
        self.assertTrue(any(row["status"] == "stalled" for row in reveal))
        self.assertEqual(reveal[-1]["reason"], "server_done")

    async def test_window_poll_cadence_does_not_add_round_trip_to_browser_interval(self):
        for rtt in (.05, .30, .52):
            with self.subTest(rtt=rtt):
                clock = FakeClock()
                starts, active = [], 0
                peak = 0

                async def request(_origin, _path, _body, _timeout):
                    nonlocal active, peak
                    starts.append(clock.now())
                    active += 1
                    peak = max(peak, active)
                    await clock.sleep(rtt)
                    active -= 1
                    window = ({"id": "last", "centerMs": 5000, "hitMs": 620, "perfectMs": 210}
                              if len(starts) == 3 else None)
                    return {"ok": True, "window": window, "done": window is not None, "windowCount": 1}

                monitor = WorldBossMonitor(SimpleNamespace(client=None, config={}, state={}), "tg9100000001",
                                           post_json=request, monotonic=clock.now, sleep=clock.sleep)
                queue, reveal = asyncio.Queue(), []
                await monitor._reveal_windows(fixture_entry(), "fixture", "fixture", "fixture",
                                              clock.now(), queue, reveal, 1)
                self.assertEqual(peak, 1)
                self.assertEqual(len([row for row in reveal if row["status"] == "revealed"]), 1)
                self.assertEqual(len(starts), 3)
                for before, after in zip(starts, starts[1:]):
                    self.assertAlmostEqual(after - before, max(.36, rtt), places=6)

    async def _rescheduled_hit(self, feedback_at):
        clock = FakeClock()
        start = clock.now()
        actual = {}

        async def request(_origin, path, _body, _timeout):
            if path.endswith("/charge-start"):
                actual["charge_ms"] = round((clock.now() - start) * 1000)
                await clock.sleep(.2)
                return {"ok": True, "chargeTicket": "fixture-ticket"}
            actual["hit_ms"] = round((clock.now() - start) * 1000)
            await clock.sleep(.2)
            return {"ok": True, "hit": {"perfect": True, "damageYi": 1}}

        monitor = WorldBossMonitor(SimpleNamespace(client=None, config={}, state={}), "tg9100000001",
                                   post_json=request, monotonic=clock.now, sleep=clock.sleep)

        async def feedback():
            await clock.sleep(feedback_at)
            # A prior window's delayed reply arrives while this window waits.
            # Only the +200 candidate fits this measured request interval.
            monitor._record_drift(200, center_ms=2000, sent_elapsed_ms=2000,
                                  request_completed_elapsed_ms=2250, request_lead_ms=100)

        window = {"id": "pending-window", "centerMs": 4000, "hitMs": 620, "perfectMs": 210}
        _, result = await asyncio.gather(feedback(), monitor._hit_window(
            fixture_entry(), "fixture", "fixture", "fixture", start, window, 2, 100))
        self.assertTrue(result["ok"])
        return actual, result

    async def test_new_feedback_advances_a_charge_that_is_still_waiting(self):
        actual, _ = await self._rescheduled_hit(2.6)
        self.assertEqual(actual["charge_ms"], 2840)

    async def test_new_feedback_reschedules_release_during_the_hold(self):
        actual, result = await self._rescheduled_hit(3.5)
        self.assertEqual(actual["charge_ms"], 2900)
        self.assertEqual(actual["hit_ms"], 3840)
        self.assertEqual(result["action"]["holdMs"], actual["hit_ms"] - actual["charge_ms"])

    async def test_window_errors_follow_request_start_cadence(self):
        for code, expected in (("boss_window_not_ready", .36), ("rate_limited", .36)):
            clock = FakeClock()
            starts = []

            async def request(_origin, _path, _body, _timeout):
                starts.append(clock.now())
                await clock.sleep(.3)
                if len(starts) == 1:
                    raise MiniAppBeastError(code, 429 if code == "rate_limited" else 400)
                return {"ok": True, "windowCount": 1, "done": True,
                        "window": {"id": "one", "centerMs": 3000, "hitMs": 620, "perfectMs": 210}}

            monitor = WorldBossMonitor(SimpleNamespace(client=None, config={}, state={}), "tg9100000001",
                                       post_json=request, monotonic=clock.now, sleep=clock.sleep)
            await monitor._reveal_windows(fixture_entry(), "fixture", "fixture", "fixture",
                                          clock.now(), asyncio.Queue(), [], 1)
            self.assertAlmostEqual(starts[1] - starts[0], expected)

    async def test_irregular_16_rounds_have_real_ticket_hold_and_distinct_live_states(self):
        clock = FakeClock()
        api = IrregularBattleFixture(clock)
        trace = []
        runtime = ProtocolRuntime(account=api.account, username="fixture", transport=api,
                                  monotonic=clock.now, sleep=clock.sleep, trace=trace.append)
        result = await runtime.run(WebViewAuth(api.account, "fixture-init", "qyz_fixture", "a" * 64))
        self.assertTrue(result["finish_confirmed"])
        self.assertEqual(result["status"], "settled_perfect")
        self.assertEqual((result["accepted_hits"], result["server_perfects"]), (16, 16))
        self.assertEqual(api.calls.count("charge-start"), 16)
        self.assertEqual(api.calls.count("hit"), 16)
        states = result["window_states"]
        self.assertEqual([w["index"] for w in states], list(range(1, 17)))
        self.assertEqual([w["center_ms"] for w in states], list(api.centers))
        for row in states:
            self.assertEqual(row["phase"], "confirmed_perfect")
            self.assertLess(row["charge_sent_ms"], row["charge_confirmed_ms"])
            self.assertLess(row["charge_confirmed_ms"], row["release_ms"])
            self.assertLess(row["release_ms"], row["reply_ms"])
            self.assertAlmostEqual(row["local_hold_ms"], row["release_ms"] - row["charge_sent_ms"], delta=1)
            self.assertTrue(520 <= row["server_hold_ms"] <= 1250)
            self.assertLessEqual(row["server_delta_ms"], 210)
            phases = [v["phase"] for v in row["transitions"]]
            for phase in ("waiting_charge", "charge_pending", "charging", "awaiting_hit_reply", "confirmed_perfect"):
                self.assertIn(phase, phases)
        for row in trace:
            state = row.get("window_state", {})
            if state.get("phase") in {"waiting_charge", "charge_pending", "charging", "awaiting_hit_reply"}:
                self.assertIsNone(state["server_perfect"])
        text = json.dumps(states)
        self.assertNotIn("fixture-ticket", text)
        self.assertNotIn("fixture-init", text)
        self.assertTrue(all(b - a >= .36 - 1e-8 for a, b in zip(api.window_starts, api.window_starts[1:])))

    async def test_missing_official_hit_stays_unconfirmed_despite_successful_finish(self):
        clock = FakeClock()
        api = IrregularBattleFixture(clock)
        request = api.post_json

        async def lose_first_hit(endpoint, body, **options):
            data = await request(endpoint, body, **options)
            if endpoint == "hit" and body["windowId"] == api.windows[0]["id"]:
                return {"ok": True}
            return data

        api.post_json = lose_first_hit
        runtime = ProtocolRuntime(account=api.account, username="fixture", transport=api,
                                  monotonic=clock.now, sleep=clock.sleep)
        result = await runtime.run(WebViewAuth(api.account, "fixture-init", "qyz_fixture", "a" * 64))
        self.assertTrue(result["finish_confirmed"])
        self.assertEqual(result["status"], "settled_partial")
        self.assertEqual(result["accepted_hits"], 15)
        self.assertEqual(result["window_states"][0]["phase"], "released_unconfirmed")
        self.assertIsNone(result["window_states"][0]["server_perfect"])

    async def test_charge_rejection_cannot_be_overwritten_by_a_release_plan(self):
        clock = FakeClock()
        api = IrregularBattleFixture(clock)
        request = api.post_json

        async def reject_charge(endpoint, body, **options):
            if endpoint == "charge-start" and body["windowId"] == api.windows[0]["id"]:
                raise ProtocolError("boss_charge_ticket_invalid", http_status=400)
            return await request(endpoint, body, **options)

        api.post_json = reject_charge
        runtime = ProtocolRuntime(account=api.account, username="fixture", transport=api,
                                  monotonic=clock.now, sleep=clock.sleep)
        result = await runtime.run(WebViewAuth(api.account, "fixture-init", "qyz_fixture", "a" * 64))
        state = result["window_states"][0]
        self.assertEqual(state["phase"], "rejected")
        self.assertEqual(state["error"], "boss_charge_ticket_invalid")
        self.assertNotIn("release_ms", state)
        self.assertIsNone(state["server_perfect"])

    async def test_cancelling_a_timing_wait_drains_its_children(self):
        waiting = asyncio.Event()
        cancelled = asyncio.Event()

        async def sleep(_delay):
            waiting.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        monitor = WorldBossMonitor(SimpleNamespace(client=None, config={}, state={}), "tg9100000001",
                                   monotonic=lambda: 100, sleep=sleep)
        previous = set(asyncio.all_tasks())
        task = asyncio.create_task(monitor._wait_for_timing_target(lambda: 103))
        await waiting.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cancelled.is_set())
        self.assertEqual(set(asyncio.all_tasks()) - previous, set())
