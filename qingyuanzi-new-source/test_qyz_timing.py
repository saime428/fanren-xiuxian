"""Causal timing fixtures: verdicts use server arrival/ticket age, never client claims."""
import unittest
from types import SimpleNamespace

from test_qyz_protocol import FakeClock
from qyz_reference.compat import MiniAppBeastError
from qyz_reference.world_boss_features import WorldBossEntry, WorldBossMonitor


class TicketTimingTests(unittest.IsolatedAsyncioTestCase):
    async def strike(self, *, charge_outbound_ms, charge_return_ms=144, hit_outbound_ms=144,
                     request_lead_ms=144, drift_ms=0, hold_skew_ms=0, charge_wake_ms=0):
        clock = FakeClock()
        battle_start = clock.now()
        window = {"id": "fixture_window", "centerMs": 3000, "hitMs": 620, "perfectMs": 210}
        ticket_at = None
        sent = []

        async def request(origin, path, body, timeout):
            nonlocal ticket_at
            if path.endswith("/charge-start"):
                await clock.sleep(charge_outbound_ms / 1000)
                ticket_at = clock.now()
                await clock.sleep(charge_return_ms / 1000)
                return {"ok": True, "chargeTicket": "fixture_ticket"}
            self.assertTrue(path.endswith("/hit"))
            sent.append(dict(body))
            await clock.sleep(hit_outbound_ms / 1000)
            hold = (clock.now() - ticket_at) * 1000
            delta = abs((clock.now() - battle_start) * 1000 - window["centerMs"])
            await clock.sleep(.144)
            if delta > window["hitMs"]:
                raise MiniAppBeastError("boss_hit_outside_window", 409)
            return {"ok": True, "hit": {"perfect": 520 <= hold <= 1250 and delta <= 210,
                                        "holdMs": hold, "deltaMs": delta, "damageYi": 1}}

        actor = SimpleNamespace(client=None, config={}, state={})
        monitor = WorldBossMonitor(actor, "tg9100000003", post_json=request,
                                   monotonic=clock.now, sleep=clock.sleep)
        monitor._hold_skew_ms = hold_skew_ms
        monitor._drift_ms = drift_ms
        monitor._drift_samples = int(bool(drift_ms))
        sleep_until = monitor._sleep_until
        woke = False

        async def wake(target):
            nonlocal woke
            result = await sleep_until(target)
            if not woke:
                clock.value += charge_wake_ms / 1000
                woke = True
            return result

        monitor._sleep_until = wake
        entry = WorldBossEntry(message_id=1, chat_id=1, bot_username="fixture_bot",
                               origin="https://asc.aiopenai.app", fingerprint="a" * 64, token="fixture")
        result = await monitor._hit_window(entry, "fixture", "fixture", "fixture", battle_start,
                                           window, window_index=5, request_lead_ms=request_lead_ms)
        self.assertEqual(len(sent), 1)
        return result, sent[0]

    async def test_slow_charge_uses_remaining_perfect_tolerance_to_reach_minimum_hold(self):
        # 797 ms charge RTT, as observed for demo_player_03 window 5. The fixture
        # puts the extra delay before ticket creation; the runner sees only its response.
        result, sent = await self.strike(charge_outbound_ms=653)
        self.assertTrue(result["accepted_perfect"], result["diagnostic"])
        self.assertGreater(result["diagnostic"]["sent_elapsed_ms"], 2856)
        self.assertLessEqual(result["diagnostic"]["server_hit"]["deltaMs"], 210)
        inference = result["diagnostic"]["arrival_inference"]
        self.assertEqual(inference["drift_after_ms"], inference["drift_before_ms"])
        self.assertEqual(sent["holdMs"], result["diagnostic"]["sent_elapsed_ms"]
                         - result["diagnostic"]["charge"]["requested_elapsed_ms"])

    async def test_available_slack_is_used_when_the_full_hold_margin_cannot_fit(self):
        # 2026-09-13 demo_player_02 window 6: a late wake and 866 ms charge
        # response leave a young ticket. The old all-or-nothing guard fired
        # immediately although another 170 ms still fit inside the window.
        result, sent = await self.strike(charge_outbound_ms=621, charge_return_ms=245,
            hit_outbound_ms=160, drift_ms=37, charge_wake_ms=112)
        self.assertTrue(result["accepted_perfect"], result["diagnostic"])
        self.assertFalse(result["diagnostic"]["hold_guard"]["feasible"])
        self.assertGreater(result["diagnostic"]["hold_guard"]["delay_ms"], 0)
        self.assertEqual(sent["holdMs"], result["diagnostic"]["sent_elapsed_ms"]
                         - result["diagnostic"]["charge"]["requested_elapsed_ms"])

    async def test_hold_rescue_keeps_margin_for_asymmetric_transit(self):
        # One feasible reconstruction of demo_player_06 window 4's 507.6 ms server
        # hold. Inputs come from send/response intervals; the future hit
        # verdict is computed by this fixture, never supplied to the runner.
        result, _ = await self.strike(charge_outbound_ms=548.5, charge_return_ms=156.5,
            hit_outbound_ms=170.1, request_lead_ms=184, drift_ms=31,
            hold_skew_ms=31, charge_wake_ms=135)
        self.assertTrue(result["accepted_perfect"], result["diagnostic"])
        self.assertGreaterEqual(result["diagnostic"]["server_hit"]["holdMs"], 520)

    async def test_long_return_does_not_overcharge_an_early_issued_ticket(self):
        result, _ = await self.strike(charge_outbound_ms=30, charge_return_ms=900)
        self.assertTrue(result["accepted_perfect"], result["diagnostic"])
        self.assertLessEqual(result["diagnostic"]["server_hit"]["holdMs"], 1250)

    async def test_unrecoverable_charge_does_not_fabricate_hold_or_retry(self):
        # 1039 ms charge RTT, as observed for demo_player_03 window 12. There is
        # insufficient room for both legal server hold and perfect arrival.
        result, sent = await self.strike(charge_outbound_ms=895)
        self.assertFalse(result["accepted_perfect"])
        self.assertLess(result["diagnostic"]["server_hit"]["holdMs"], 520)
        self.assertEqual(sent["holdMs"], result["diagnostic"]["sent_elapsed_ms"]
                         - result["diagnostic"]["charge"]["requested_elapsed_ms"])
        self.assertFalse(result["diagnostic"]["hold_guard"]["feasible"])

    async def test_slow_response_after_ticket_creation_stays_within_hold_ceiling(self):
        result, _ = await self.strike(charge_outbound_ms=144, charge_return_ms=653)
        self.assertTrue(result["accepted_perfect"], result["diagnostic"])
        self.assertLessEqual(result["diagnostic"]["server_hit"]["holdMs"], 1250)

    async def test_warm_transport_needs_no_hold_delay_and_reports_real_scheduler_lateness(self):
        result, _ = await self.strike(charge_outbound_ms=144)
        diagnostic = result["diagnostic"]
        self.assertTrue(result["accepted_perfect"])
        self.assertEqual(diagnostic["sent_elapsed_ms"], 2856)
        self.assertEqual(diagnostic["wake_lateness_ms"], 0)
        self.assertEqual(diagnostic["wake_lateness_ms"], diagnostic["sent_elapsed_ms"] - diagnostic["target_ms"])

    async def test_late_hit_preserves_rejection_without_replay_or_local_success(self):
        # 1066 ms hit RTT reproduces a cold-connection delay before server arrival.
        result, _ = await self.strike(charge_outbound_ms=144, hit_outbound_ms=922)
        self.assertFalse(result["ok"])
        self.assertFalse(result["accepted_perfect"])
        self.assertEqual(result["error"], "boss_hit_outside_window")
        self.assertEqual(len(result["diagnostic"]["request"]["attempts"]), 1)
