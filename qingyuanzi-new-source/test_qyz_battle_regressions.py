"""September 14 regressions, using causal offline servers and no game requests."""
import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from qyz_protocol import ProtocolError, ProtocolRuntime, WebViewAuth
from qyz_reference.world_boss_features import WorldBossMonitor
from qyz_timing import AdmissionClock
from test_qyz_protocol import BattleFixture, CallbackFixtureBroker, FakeClock
from test_qyz_dynamic_windows import IrregularBattleFixture, fixture_entry


class AdmissionClockTests(unittest.TestCase):
    def test_requires_repeated_recent_same_account_evidence(self):
        now = [100.0]
        clock = AdmissionClock(lambda: now[0])
        def observe(value):
            clock.observe_start({"attempts": [{"ok": True, "duration_ms": value}]})
        observe(300)
        trace = {"clock_rtt_ms": 737, "clock_rtt_source": "successful_begin"}
        clock.refine_begin(trace)
        self.assertEqual(trace["clock_rtt_ms"], 737)
        for value in (301, 850, 299, 308):
            observe(value)
        clock.refine_begin(trace)
        # The network floor (not the median) is the reference: server-side work
        # such as room polling only ever inflates a same-account /start.
        self.assertEqual(trace["clock_rtt_ms"], 299)
        self.assertEqual(trace["network_reference"]["floor_ms"], 299)
        self.assertEqual(trace["network_reference"]["stable_samples"], 4)
        # The estimate is neither global nor persisted into another account/event.
        other = {"clock_rtt_ms": 737}
        AdmissionClock(lambda: now[0]).refine_begin(other)
        self.assertEqual(other["clock_rtt_ms"], 737)
        # Turnstile waits run 100-150 s; same-event samples stay usable for 300 s.
        now[0] += 150
        recent = {"clock_rtt_ms": 737}
        clock.refine_begin(recent)
        self.assertEqual(recent["clock_rtt_ms"], 299)
        now[0] += 151
        stale = {"clock_rtt_ms": 737}
        clock.refine_begin(stale)
        self.assertEqual(stale["clock_rtt_ms"], 737)

    def test_jittery_start_samples_still_provide_the_floor(self):
        """2026-09-22 @demo_player_04: /start 817/1092/338/2753/1507 ms, /begin probe 2483 ms."""
        clock = AdmissionClock(lambda: 100.0)
        for value in (817, 1092, 338, 2753, 1507):
            clock.observe_start({"attempts": [{"ok": True, "duration_ms": value}]})
        trace = {"clock_rtt_ms": 2483, "clock_rtt_source": "pre_verification_begin"}
        clock.refine_begin(trace)
        self.assertEqual(trace["clock_rtt_ms"], 338)
        self.assertEqual(trace["clock_rtt_source"], "recent_account_start")
        self.assertEqual(trace["network_reference"]["stable_samples"], 1)
        # A probe close to the floor is still the audited reference.
        near = {"clock_rtt_ms": 420}
        clock.refine_begin(near)
        self.assertEqual(near["clock_rtt_ms"], 420)

    def test_normal_begin_variation_and_malformed_samples_do_not_change_timing(self):
        clock = AdmissionClock(lambda: 100.0)
        for value in (float('nan'), True, -1, '300'):
            clock.observe_start({"attempts": [{"ok": True, "duration_ms": value}]})
        clock.observe_start({"attempts": [{"ok": False, "duration_ms": 50}]})
        self.assertEqual(len(clock.samples), 0)
        for value in (300, 310, 308, 305, 320):
            clock.observe_start({"attempts": [{"ok": True, "duration_ms": value}]})
        for baseline in (274, 318, 368, 411):
            trace = {"clock_rtt_ms": baseline}
            clock.refine_begin(trace)
            self.assertEqual(trace["clock_rtt_ms"], baseline)


class SlowBeginFixture(IrregularBattleFixture):
    """A cold probe and Siteverify work are slower than the warm action path."""
    account = "tg9100000002"

    def __init__(self, clock):
        super().__init__(clock, reveal_ahead_ms=2200)
        self.start_count = 0
        self.begin_payloads = []

    async def post_json(self, endpoint, body, *, timeout=8):
        if endpoint == "start":
            self.start_count += 1
            data = await super().post_json(endpoint, body, timeout=timeout)
            if self.start_count < 6:
                data.pop("challenge")
                data["boss"].update(roomStatus="joining", joinRemainingSeconds=5)
            return data
        if endpoint == "begin":
            self.calls.append(endpoint)
            self.begin_payloads.append(dict(body))
            if not body.get("turnstileToken"):
                await self.clock.sleep(.852)
                raise ProtocolError("turnstile_failed", phase="begin", http_status=403)
            # startsInMs is generated AFTER 437 ms of server verification work.
            await self.clock.sleep(.150 + .437)
            self.started_at = self.clock.now() + 1.499
            await self.clock.sleep(.150)
            return {"ok": True, "startsInMs": 1499}
        return await super().post_json(endpoint, body, timeout=timeout)


class StalledRevealFixture(IrregularBattleFixture):
    def __init__(self, clock):
        super().__init__(clock, reveal_ahead_ms=2200)
        self.stalled = False
        self.timed_out = False
        self.window_timeouts = []

    async def post_json(self, endpoint, body, *, timeout=8):
        if endpoint == "window":
            self.window_timeouts.append(timeout)
            elapsed = (self.clock.now() - self.started_at) * 1000
            if not self.stalled and elapsed >= self.centers[4] - 2600:
                self.stalled = True
                # A read cancellation is safe; the server alone owns its reveal
                # schedule, and the next request uses the same afterWindowId.
                await self.clock.sleep(min(timeout, 3.1))
                if timeout < 3.1:
                    self.timed_out = True
                    raise ProtocolError("read_transport_failed", phase="window")
        return await super().post_json(endpoint, body, timeout=timeout)


class BattleRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def run_fixture(self, api, clock, *, broker=None):
        runtime = ProtocolRuntime(account=api.account, username="fixture", transport=api,
                                  monotonic=clock.now, sleep=clock.sleep, broker=broker)
        result = await runtime.run(WebViewAuth(api.account, "fixture", "qyz_fixture", "a" * 64))
        return runtime, result

    async def test_cold_begin_does_not_shift_all_sixteen_windows_early(self):
        clock = FakeClock()
        api = SlowBeginFixture(clock)
        broker = CallbackFixtureBroker()
        runtime, result = await self.run_fixture(api, clock, broker=broker)
        self.assertEqual(result["accepted_hits"], 16)
        self.assertEqual(result["server_perfects"], 16)
        self.assertAlmostEqual(runtime.combat.started_at, api.started_at, places=3)
        sync = result["reference_outcome"]["diagnostics"]["clock_sync"]
        self.assertEqual(sync["request"]["successful_attempt_ms"], 737)
        self.assertEqual(sync["round_trip_ms"], 300)
        self.assertEqual(sync["request"]["clock_rtt_source"], "recent_account_start")
        self.assertEqual(len(api.begin_payloads), 2)
        self.assertEqual(len(broker.generated), 1)
        self.assertFalse(broker._tokens)

    async def test_stalled_window_read_yields_to_reveal_loop_before_window_is_lost(self):
        clock = FakeClock()
        api = StalledRevealFixture(clock)
        _, result = await self.run_fixture(api, clock)
        self.assertTrue(api.stalled)
        self.assertTrue(api.timed_out)
        self.assertLessEqual(max(api.window_timeouts), 1)
        self.assertEqual(result["accepted_hits"], 16)
        self.assertEqual(result["server_perfects"], 16)
        self.assertEqual(len(api.tickets), len(api.releases))

    async def test_finish_waits_for_real_minimum_duration_before_submitting(self):
        clock = FakeClock()
        api = BattleFixture(clock, count=1)
        original = api.post_json
        finish_durations = []

        async def request(endpoint, body, **options):
            data = await original(endpoint, body, **options)
            if endpoint == "start":
                data["challenge"].update(minDurationMs=75000, maxDurationMs=312000)
            elif endpoint == "finish":
                duration = body["bossProof"]["durationMs"]
                finish_durations.append(duration)
                if duration < 75000:
                    raise ProtocolError("boss_duration_too_short", phase="finish", http_status=400)
            return data

        api.post_json = request
        _, result = await self.run_fixture(api, clock)
        self.assertTrue(result["finish_confirmed"])
        self.assertEqual(len(finish_durations), 1)
        self.assertGreaterEqual(finish_durations[0], 75000)

    async def test_absolute_sleep_deadline_does_not_repeat_an_event_loop_stall(self):
        async def run(stall):
            clock = FakeClock()
            monitor = WorldBossMonitor(
                SimpleNamespace(client=None, config={}, state={}),
                "tg9100000001",
                monotonic=clock.now,
            )
            monitor._boss_defeat_marker = Path("/tmp/qyz-fixture-stop.json")
            loop = asyncio.get_running_loop()
            debug = loop.get_debug()
            loop.set_debug(False)
            stop = asyncio.Event()
            target = clock.now() + .822

            def block_loop():
                clock.value += stall

            async def wait():
                loop.call_soon(block_loop)
                return await monitor._sleep_until(target)

            try:
                with patch.object(loop, "time", clock.now), \
                        patch.object(monitor, "_boss_stop_requested", return_value=False), \
                        patch.object(monitor, "_wait_for_boss_stop", new=stop.wait):
                    task = asyncio.create_task(wait())
                    try:
                        for _ in range(12):
                            await asyncio.sleep(0)
                        clock.value = max(clock.value, target)
                        for _ in range(12):
                            await asyncio.sleep(0)
                        self.assertTrue(task.done(), "The absolute deadline already passed")
                        self.assertTrue(task.result())
                    finally:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
            finally:
                loop.set_debug(debug)
            await monitor.stop()

        for stall in (.421, 1.875):
            with self.subTest(stall_seconds=stall):
                await run(stall)

    async def test_slow_charge_feedback_does_not_bias_the_next_hold(self):
        clock = FakeClock()
        clock.value = 101.217

        async def request(_origin, path, payload, _timeout):
            sequence = int(payload["windowId"])
            if path.endswith("/charge-start"):
                await clock.sleep(.981 if sequence == 1 else .190)
                return {"ok": True, "chargeTicket": "fixture-ticket"}
            await clock.sleep(.192)
            return {"ok": True, "hit": {
                "damageYi": 1,
                "perfect": sequence > 1,
                "deltaMs": 19.8,
                "holdMs": 201.1 if sequence == 1 else payload["holdMs"] + .3,
            }}

        monitor = WorldBossMonitor(
            SimpleNamespace(client=None, config={}, state={}),
            "tg9100000001",
            post_json=request,
            monotonic=clock.now,
            sleep=clock.sleep,
        )
        try:
            first = await monitor._hit_window(
                fixture_entry(), "fixture", "fixture", "fixture", 100,
                {"id": "1", "centerMs": 2308, "hitMs": 620, "perfectMs": 210}, 1, 95,
            )
            self.assertFalse(first["accepted_perfect"])
            self.assertEqual(monitor._planned_hold_ms(), 1000)
            self.assertEqual(first["diagnostic"]["hold_feedback"]["reason"], "slow_charge_request")
            second = await monitor._hit_window(
                fixture_entry(), "fixture", "fixture", "fixture", 100,
                {"id": "2", "centerMs": 7513, "hitMs": 620, "perfectMs": 210}, 2, 95,
            )
            self.assertEqual(second["diagnostic"]["planned_hold_ms"], 1000)
        finally:
            await monitor.stop()

    async def test_read_failure_returns_to_owner_without_sixty_second_inline_retries(self):
        clock = FakeClock()
        api = BattleFixture(clock)
        calls = []

        async def fail(endpoint, body, **options):
            calls.append(endpoint)
            raise ProtocolError("read_transport_failed", phase=endpoint)

        api.post_json = fail
        runtime = ProtocolRuntime(account=api.account, username="fixture", transport=api,
                                  monotonic=clock.now, sleep=clock.sleep)
        with self.assertRaisesRegex(ProtocolError, "read_transport_failed"):
            await runtime._post_until_reply("window", {}, 1)
        self.assertEqual(calls, ["window"])

    async def test_official_damage_reduction_is_not_an_unqualified_perfect_settlement(self):
        clock = FakeClock()
        api = BattleFixture(clock, count=16)
        original = api.post_json

        async def request(endpoint, body, **options):
            data = await original(endpoint, body, **options)
            if endpoint == "hit":
                data["hit"]["automationDamageMultiplier"] = .18
                data["hit"]["automationReview"] = True
            if endpoint == "finish":
                data["result"]["realtime_damage_yi"] = 123
                data["result"]["automation_review"] = True
                data["result"]["automation_risk"] = {
                    "damageMultiplier": .18, "review": True, "flags": ["fixture official flag"]}
            return data

        api.post_json = request
        _, result = await self.run_fixture(api, clock)
        self.assertEqual(result["accepted_hits"], 16)
        self.assertEqual(result["server_perfects"], 16)
        self.assertTrue(result["finish_confirmed"])
        self.assertEqual(result["status"], "settled_damage_reduced")
        self.assertEqual(result["server_damage_multiplier_min"], .18)
        self.assertEqual(result["server_result"]["realtime_damage_yi"], 123)
        self.assertEqual(result["server_result"]["automation_risk"]["damageMultiplier"], .18)


if __name__ == "__main__":
    unittest.main()


class FailedBattleEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_finish_keeps_confirmed_hits_and_the_server_code(self):
        clock = FakeClock()
        api = BattleFixture(clock, count=3)
        original = api.post_json

        async def request(endpoint, body, **options):
            if endpoint == "finish":
                raise ProtocolError("boss_client_clock_mismatch", phase="finish", http_status=400)
            return await original(endpoint, body, **options)

        api.post_json = request
        runtime = ProtocolRuntime(account=api.account, username="fixture", transport=api,
                                  monotonic=clock.now, sleep=clock.sleep)
        with self.assertRaises(ProtocolError) as ctx:
            await runtime.run(WebViewAuth(api.account, "fixture", "qyz_fixture", "a" * 64))
        error = ctx.exception
        self.assertEqual(error.code, "boss_client_clock_mismatch")
        self.assertEqual((error.details["accepted_hits"], error.details["server_perfects"], error.details["expected_windows"]),
                         (3, 3, 3))
        self.assertTrue(error.details["begin_confirmed"])
        self.assertEqual(len(error.details["receipts"]), 3)
        self.assertFalse(error.details["local_dead"])


class WarmFloorLeadTests(unittest.TestCase):
    def test_lead_is_capped_at_half_the_warm_action_floor(self):
        from qyz_timing import effective_request_lead
        rows = [{"pool": "action", "http_status": 200, "duration_ms": d, "connection_reused": True}
                for d in (363, 395, 412)]
        # 09-20 @demo_player_05: probe 811 ms gave lead 406 while warm hits took 363-412 ms.
        lead, evidence = effective_request_lead(406, rows)
        self.assertEqual(lead, 182)
        self.assertTrue(evidence["applied"])
        self.assertEqual(evidence["floor_rtt_ms"], 363)
        # A lead already inside the cap is untouched.
        self.assertEqual(effective_request_lead(160, rows)[0], 160)

    def test_cold_probe_and_read_pool_samples_never_lower_the_lead(self):
        from qyz_timing import effective_request_lead
        rows = [{"pool": "action", "http_status": 403, "duration_ms": 811, "connection_reused": True},
                {"pool": "action", "http_status": 200, "duration_ms": 1332, "connection_reused": False},
                {"pool": "read", "http_status": 200, "duration_ms": 300, "connection_reused": True},
                {"pool": "action", "http_status": 200, "duration_ms": 380, "connection_reused": True},
                {"pool": "action", "http_status": 200, "duration_ms": float("nan"), "connection_reused": True}]
        lead, evidence = effective_request_lead(406, rows)
        self.assertEqual((lead, evidence["applied"], evidence["decision"]), (406, False, "insufficient_warm_samples"))


class ColdReadPoolFixture(IrregularBattleFixture):
    """Every read-pool poll dies inside its TLS handshake until a wider budget is allowed."""
    def __init__(self, clock):
        super().__init__(clock, reveal_ahead_ms=2200)
        self.request_diagnostics = []
        self.read_pool_cold_failures = 0
        self.window_timeouts = []
        self.warmed = 0

    async def warm_read_pool(self, auth, *, connections=2, timeout=4):
        self.warmed += 1
        return connections

    async def post_json(self, endpoint, body, *, timeout=8):
        if endpoint == "window":
            self.window_timeouts.append(timeout)
            if self.warmed == 0 and timeout < 1.2:
                await self.clock.sleep(timeout)
                self.read_pool_cold_failures += 1
                raise ProtocolError("read_transport_failed", phase="window")
        return await super().post_json(endpoint, body, timeout=timeout)


class ColdReadPoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_reveal_loop_warms_the_read_pool_before_its_first_poll(self):
        clock = FakeClock()
        api = ColdReadPoolFixture(clock)
        runtime = ProtocolRuntime(account=api.account, username="fixture", transport=api,
                                  monotonic=clock.now, sleep=clock.sleep)
        result = await runtime.run(WebViewAuth(api.account, "fixture", "qyz_fixture", "a" * 64))
        self.assertGreaterEqual(api.warmed, 1)
        self.assertEqual(result["accepted_hits"], 16)
        self.assertEqual(result["server_perfects"], 16)

    async def test_cold_failure_widens_only_the_next_poll(self):
        clock = FakeClock()
        api = IrregularBattleFixture(clock, reveal_ahead_ms=2200)
        api.read_pool_cold_failures = 1
        timeouts = []
        original = api.post_json
        async def request(endpoint, body, **options):
            if endpoint == "window":
                timeouts.append(options.get("timeout"))
                api.read_pool_cold_failures = 0
            return await original(endpoint, body, **options)
        api.post_json = request
        runtime = ProtocolRuntime(account=api.account, username="fixture", transport=api,
                                  monotonic=clock.now, sleep=clock.sleep)
        result = await runtime.run(WebViewAuth(api.account, "fixture", "qyz_fixture", "a" * 64))
        self.assertEqual(timeouts[0], 2)
        self.assertTrue(all(t == 1 for t in timeouts[1:]))
        self.assertEqual(result["accepted_hits"], 16)


class StalledReadSocketFixture(IrregularBattleFixture):
    """One poll dies on its socket while the transport's other read socket is warm."""
    def __init__(self, clock, *, other_socket_warm=True):
        super().__init__(clock, reveal_ahead_ms=2200)
        self.other_socket_warm = other_socket_warm
        self.read_pool_cold_failures = 0
        self.cold = 0
        self.failed = None
        self.window_timeouts = []
        self.warm_calls = []

    def read_socket_warm(self):
        return self.cold == 0 or self.other_socket_warm

    def cold_read_sockets(self):
        return self.cold

    async def warm_read_pool(self, auth, *, connections=2, timeout=4, cold_only=False):
        self.warm_calls.append((cold_only, timeout))
        if cold_only:
            # A slow handshake that still completes, off the poll path.
            await self.clock.sleep(1.4)
        opened, self.cold = (self.cold if cold_only else 2), 0
        return opened

    async def post_json(self, endpoint, body, *, timeout=8):
        if endpoint == "window":
            self.window_timeouts.append(timeout)
            elapsed = (self.clock.now() - self.started_at) * 1000
            if not self.failed and elapsed >= self.centers[4] - 2600:
                self.failed = len(self.window_timeouts) - 1
                await self.clock.sleep(timeout)
                self.cold, self.read_pool_cold_failures = 1, 1
                raise ProtocolError("read_transport_failed", phase="window")
        return await super().post_json(endpoint, body, timeout=timeout)


class SlowHandshakeBeginFixture(SlowBeginFixture):
    """2026-09-27: each read-socket handshake took 1.5-1.8 s and window 1 came early."""
    centers = (1600,) + IrregularBattleFixture.centers[1:]

    def __init__(self, clock, *, handshake_seconds=1.6):
        super().__init__(clock)
        self.handshake_seconds = handshake_seconds
        self.warm_started, self.warm_finished, self.begin_sent = [], [], []

    async def warm_read_pool(self, auth, *, connections=2, timeout=4, cold_only=False):
        self.warm_started.append(self.clock.now())
        await self.clock.sleep(self.handshake_seconds)
        self.warm_finished.append(self.clock.now())
        return connections

    async def post_json(self, endpoint, body, *, timeout=8):
        if endpoint == "begin":
            self.begin_sent.append(self.clock.now())
        return await super().post_json(endpoint, body, timeout=timeout)


class ReadPoolBeforeBeginTests(unittest.IsolatedAsyncioTestCase):
    async def run_fixture(self, api, clock):
        runtime = ProtocolRuntime(account=api.account, username="fixture", transport=api,
                                  monotonic=clock.now, sleep=clock.sleep, broker=CallbackFixtureBroker())
        return await runtime.run(WebViewAuth(api.account, "fixture", "qyz_fixture", "a" * 64))

    async def test_read_sockets_open_before_begin_so_window_one_is_revealed_in_time(self):
        clock = FakeClock()
        api = SlowHandshakeBeginFixture(clock)
        result = await self.run_fixture(api, clock)
        # One warm-up, finished before the clock probe left: no handshake
        # shares the network with either /begin.
        self.assertEqual(1, len(api.warm_started))
        self.assertLessEqual(api.warm_finished[0], api.begin_sent[0])
        # Opened after /begin, the first poll left 0.25 s into the battle and
        # window 1's charge went out late with a ~900 ms hold.
        self.assertLess(api.window_starts[0], api.started_at)
        self.assertGreaterEqual(api.hits["irregular-1"]["holdMs"], 1100)
        self.assertEqual((result["accepted_hits"], result["server_perfects"]), (16, 16))

    async def test_a_hung_warm_up_delays_begin_by_the_bound_only(self):
        from qyz_timing import READ_POOL_WARM_WAIT_SECONDS
        clock = FakeClock()
        api = SlowHandshakeBeginFixture(clock, handshake_seconds=600)
        result = await self.run_fixture(api, clock)
        self.assertEqual(1, len(api.warm_started))
        self.assertAlmostEqual(api.begin_sent[0] - api.warm_started[0], READ_POOL_WARM_WAIT_SECONDS, places=3)
        # The reveal loop waits one more bound at most, then polls anyway.
        self.assertLessEqual(api.window_starts[0] - api.warm_started[0], 2 * READ_POOL_WARM_WAIT_SECONDS + 3)
        self.assertGreaterEqual(result["accepted_hits"], 14)
        self.assertEqual([], api.warm_finished)  # still hung, cancelled at battle end


class ReadSocketFailoverTests(unittest.IsolatedAsyncioTestCase):
    async def run_fixture(self, api, clock):
        runtime = ProtocolRuntime(account=api.account, username="fixture", transport=api,
                                  monotonic=clock.now, sleep=clock.sleep)
        result = await runtime.run(WebViewAuth(api.account, "fixture", "qyz_fixture", "a" * 64))
        return runtime, result

    async def test_failed_poll_keeps_the_one_second_budget_while_a_warm_socket_remains(self):
        clock = FakeClock()
        api = StalledReadSocketFixture(clock)
        runtime, result = await self.run_fixture(api, clock)
        self.assertIsNotNone(api.failed)
        # The next poll uses the warm socket, so it needs no cold-handshake budget.
        self.assertTrue(all(timeout == 1 for timeout in api.window_timeouts))
        self.assertIn((True, 4), api.warm_calls)
        self.assertEqual(api.cold_read_sockets(), 0)
        self.assertTrue(runtime._read_rewarm.done())
        self.assertEqual((result["accepted_hits"], result["server_perfects"]), (16, 16))

    async def test_cold_budget_applies_only_when_no_warm_socket_is_left(self):
        clock = FakeClock()
        api = StalledReadSocketFixture(clock, other_socket_warm=False)
        _runtime, result = await self.run_fixture(api, clock)
        timeouts = api.window_timeouts
        self.assertTrue(all(timeout == 1 for timeout in timeouts[:api.failed + 1]))
        self.assertEqual(timeouts[api.failed + 1], 2)
        # Once the re-warm restores a socket, polls return to the 1-second budget.
        self.assertEqual(timeouts[-1], 1)
        self.assertEqual(result["accepted_hits"], 16)

    async def test_battle_end_cancels_a_pending_rewarm(self):
        clock = FakeClock()
        api = StalledReadSocketFixture(clock)
        never = asyncio.Event()
        async def stuck_warm(auth, *, connections=2, timeout=4, cold_only=False):
            api.warm_calls.append((cold_only, timeout))
            if cold_only:
                await never.wait()
            return 2
        api.warm_read_pool = stuck_warm
        runtime, result = await self.run_fixture(api, clock)
        self.assertIn((True, 4), api.warm_calls)
        self.assertTrue(runtime._read_rewarm.cancelled())
        self.assertEqual(result["accepted_hits"], 16)
