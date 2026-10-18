"""Arrival headroom is known before the request; verdicts come from server time."""
import asyncio
import unittest
from types import SimpleNamespace

from qyz_reference.world_boss_features import WorldBossEntry, WorldBossMonitor
from qyz_timing import (VpsTimingMixin, arrival_budget, _ACTIVE_WINDOW,
                        ARRIVAL_ADVANCE_MAX_MS, ARRIVAL_EARLY_GUARD_MS,
                        ARRIVAL_CHARGE_GUARD_MS, DRIFT_MAX_STEP_MS)
from test_qyz_protocol import FakeClock


class Updated(VpsTimingMixin, WorldBossMonitor):
    pass


class Previous(Updated):
    def _hit_offset_ms(self, window):
        return WorldBossMonitor._hit_offset_ms(self, window)


class BudgetTests(unittest.TestCase):
    def budget(self, **changes):
        values = dict(center_ms=3000, perfect_ms=210, elapsed_ms=0, hold_ms=1150,
                      request_lead_ms=55, drift_ms=0, account_offset_ms=0)
        values.update(changes)
        return arrival_budget(**values)

    def test_early_window_gets_bounded_margin(self):
        self.assertEqual(self.budget()['advance_ms'], 60)

    def test_late_reveal_cannot_shorten_the_hold_to_buy_margin(self):
        self.assertEqual(self.budget(elapsed_ms=1740)['advance_ms'], 30)
        self.assertEqual(self.budget(elapsed_ms=1770)['advance_ms'], 0)
        self.assertEqual(self.budget(elapsed_ms=2000)['advance_ms'], 0)

    def test_narrow_windows_and_existing_lead_consume_early_headroom(self):
        self.assertEqual(self.budget(perfect_ms=100)['advance_ms'], 0)
        self.assertEqual(self.budget(drift_ms=78)['advance_ms'], 12)
        self.assertEqual(self.budget(drift_ms=120)['advance_ms'], 0)

    def test_presolve_does_not_advance_a_charge_before_battle_start(self):
        self.assertEqual(self.budget(center_ms=1000, elapsed_ms=-1000)['advance_ms'], 0)

    def test_geometry_reserves_one_feedback_step_and_charge_preparation(self):
        for drift in [-200, 0, 23, 60, 80, 200]:
            for elapsed in [0, 1600, 1740, 2000]:
                b = self.budget(drift_ms=drift, elapsed_ms=elapsed)
                advance = b['advance_ms']
                self.assertGreaterEqual(advance, 0)
                self.assertLessEqual(advance, ARRIVAL_ADVANCE_MAX_MS)
                if advance:
                    self.assertLessEqual(advance + max(0, drift) + DRIFT_MAX_STEP_MS + ARRIVAL_EARLY_GUARD_MS, 210)
                    self.assertGreaterEqual(3000 - 55 - drift - 1150 - advance - elapsed, ARRIVAL_CHARGE_GUARD_MS)

    def test_invalid_numbers_do_not_create_margin(self):
        for value in [None, True, float('nan'), float('inf'), '3000']:
            self.assertEqual(self.budget(center_ms=value)['advance_ms'], 0)


class CausalStrikeTests(unittest.IsolatedAsyncioTestCase):
    async def strike(self, cls, *, charge_queue=0, hit_queue=0, enter_at=0):
        clock = FakeClock()
        start = clock.now()
        clock.value += enter_at / 1000
        issued = None
        calls, requests = [], []

        async def request(origin, path, body, timeout):
            nonlocal issued
            calls.append(path.rsplit('/', 1)[-1])
            if path.endswith('/charge-start'):
                requests.append(('charge', (clock.now() - start) * 1000))
                await clock.sleep((55 + charge_queue) / 1000)
                issued = clock.now()
                await clock.sleep(.055)
                return {'ok': True, 'chargeTicket': 'fixture_ticket'}
            self.assertTrue(path.endswith('/hit'))
            requests.append(('hit', (clock.now() - start) * 1000))
            await clock.sleep((55 + hit_queue) / 1000)
            hold = (clock.now() - issued) * 1000
            delta = abs((clock.now() - start) * 1000 - 3000)
            await clock.sleep(.055)
            return {'ok': True, 'hit': {'perfect': 520 <= hold <= 1250 and delta <= 210,
                                       'holdMs': hold, 'deltaMs': delta, 'damageYi': 1}}

        m = cls(SimpleNamespace(client=None, config={}, state={}), 'tg9100000001',
                post_json=request, monotonic=clock.now, sleep=clock.sleep)
        entry = WorldBossEntry(message_id=1, chat_id=1, bot_username='fixture',
                               origin='https://asc.aiopenai.app', fingerprint='a' * 64, token='fixture')
        window = {'id': 'fixture', 'centerMs': 3000, 'hitMs': 620, 'perfectMs': 210}
        result = await m._hit_window(entry, 'fixture', 'fixture', 'fixture', start, window,
                                     window_index=1, request_lead_ms=55)
        self.assertEqual(calls, ['charge-start', 'hit'])
        self.assertIsNone(_ACTIVE_WINDOW.get())
        self.assertAlmostEqual(result['diagnostic']['hold_ms'], requests[1][1] - requests[0][1], delta=1)
        return result, requests, m

    async def test_moderate_queue_becomes_perfect_without_changing_hold(self):
        old, old_times, _ = await self.strike(Previous, charge_queue=240, hit_queue=240)
        new, new_times, _ = await self.strike(Updated, charge_queue=240, hit_queue=240)
        self.assertFalse(old['accepted_perfect'])
        self.assertTrue(new['accepted_perfect'])
        self.assertAlmostEqual(old['diagnostic']['server_hold_ms'], new['diagnostic']['server_hold_ms'], delta=1)
        for before, after in zip(old_times, new_times):
            self.assertAlmostEqual(before[1] - after[1], 60, delta=1)

    async def test_fast_path_keeps_perfect_and_reports_real_early_arrival(self):
        result, _, m = await self.strike(Updated)
        self.assertTrue(result['accepted_perfect'])
        self.assertAlmostEqual(result['diagnostic']['server_hit']['deltaMs'], 60, delta=1)
        self.assertEqual(result['diagnostic']['actual_elapsed_ms'], 2940)
        self.assertEqual(m._drift_lead_ms(), 0)  # intentional margin is not network drift

    async def test_late_reveal_keeps_the_original_schedule(self):
        old, old_times, _ = await self.strike(Previous, enter_at=2000)
        new, new_times, _ = await self.strike(Updated, enter_at=2000)
        self.assertEqual(new['diagnostic']['arrival_budget']['advance_ms'], 0)
        self.assertEqual(old_times, new_times)
        self.assertEqual(old['accepted_perfect'], new['accepted_perfect'])

    async def test_unrecoverable_tail_is_not_claimed_as_perfect(self):
        result, _, _ = await self.strike(Updated, hit_queue=500)
        self.assertFalse(result['accepted_perfect'])

    async def test_concurrent_windows_do_not_share_budgets(self):
        class Base:
            monotonic = staticmethod(lambda: 100)
            def _planned_hold_ms(self): return 1150
            def _drift_lead_ms(self): return 0
            def _hit_offset_ms(self, window): return 0
            async def _hit_window(self, entry, init, token, challenge, start, window, index, lead, charge):
                await asyncio.sleep(0)
                offset = self._hit_offset_ms(window)
                await asyncio.sleep(0)
                return {'diagnostic': {'offset': offset}}
        class Probe(VpsTimingMixin, Base):
            def _planned_hold_ms(self): return 1150
        m = Probe()
        early = {'id': 'early', 'centerMs': 3000, 'perfectMs': 210}
        late = {'id': 'late', 'centerMs': 1000, 'perfectMs': 210}
        a, b = await asyncio.gather(m._hit_window(None, '', '', '', 100, early, request_lead_ms=55),
                                    m._hit_window(None, '', '', '', 100, late, request_lead_ms=55))
        self.assertEqual(a['diagnostic']['offset'], -60)
        self.assertEqual(b['diagnostic']['offset'], 0)
        self.assertIsNone(_ACTIVE_WINDOW.get())


if __name__ == '__main__':
    unittest.main()
