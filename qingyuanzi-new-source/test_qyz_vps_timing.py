"""Causal timing tests: network delay produces verdicts before feedback sees them."""
import unittest
from types import SimpleNamespace

from qyz_reference.world_boss_features import WorldBossMonitor
from qyz_timing import VpsTimingMixin, DRIFT_MAX_STEP_MS, TARGET_SERVER_HOLD_MS
from qyz_protocol import ProtocolRuntime, WebViewAuth
from test_qyz_protocol import FakeClock, BattleFixture


class VpsMonitor(VpsTimingMixin, WorldBossMonitor):
    pass


def monitor(cls=VpsMonitor):
    return cls(SimpleNamespace(client=None, config={}, state={}), 'tg9100000001')


def trace(ms, attempts=1):
    return {'total_duration_ms': ms, 'attempts': [{'ok': True, 'duration_ms': ms}] * attempts}


class FeedbackTests(unittest.TestCase):
    def test_all_thirteen_observed_reversals_are_corrected_or_rejected(self):
        # Numeric one-step counterexamples from 2026-10-04. Each case uses the
        # original decision point; this is NOT a claim about a rerun's verdict.
        cases = [(61.5, 889, 997, 53, 0.0, 59.965, 77.645, -1),
         (104.1, 834, 961, 53, 0.0, 112.935, 128.975, -1),
         (322, 919, 1425, 53, 0, 28.863, -0.915, 1),
         (139.5, 926, 1213, 58, 0.0, 17.467, 0.047, 1),
         (50, 988, 1213, 54, 0, 74.37, 67.059, 1),
         (564.8, 884, 1682, 54, 0.0, 61.941, 60.175, 1),
         (631.2, 888, 1741, 52, 0.0, 60.175, 58.954, 1),
         (324.5, 926, 1415, 50, 0.0, 23.778, -0.845, 1),
         (36.6, 912, 1022, 50, 0.0, 38.828, 66.6, -1),
         (76.5, 866, 986, 49, 0.0, 85.67, 99.389, -1),
         (210.2, 730, 863, 54, 0.0, 217.304, 244.663, -1),
         (114.1, 702, 969, 54, 0.0, 244.663, 263.814, -1),
         (162.1, 924, 1291, 53, 0.0, 23.685, 2.72, 1)]
        for delta, sent, completed, lead, offset, before, old_after, sign in cases:
            with self.subTest(delta=delta, before=before):
                self.assertLess(sign * (old_after - before), 0)
                m = monitor()
                m._drift_ms, m._drift_samples = before, 1
                result = m._record_drift(delta, 1000, sent, completed,
                                          request_lead_ms=lead, account_offset_ms=offset)
                self.assertIsNotNone(result)
                self.assertGreaterEqual(sign * (m._drift_ms - before), 0)

    def test_real_early_receipt_cannot_increase_an_already_excessive_lead(self):
        m = monitor()
        m._drift_ms, m._drift_samples = 217.304, 3
        m._drift_history = [395.6, 400, 308.5]
        m._drift_weight_history = [(x, 1) for x in m._drift_history]
        result = m._record_drift(210.2, 37818, 37548, 37682, request_lead_ms=54)
        self.assertEqual(result['direction'], 'early')
        self.assertLess(m._drift_ms, 217.304)
        self.assertLessEqual(217.304 - m._drift_ms, DRIFT_MAX_STEP_MS)

    def test_delayed_request_does_not_become_persistent_clock_drift(self):
        m = monitor()
        result = m._record_drift(600, 1000, 945, 1655, request_lead_ms=55)
        self.assertEqual(result['feedback_reason'], 'request_tail_latency')
        self.assertEqual(m._drift_samples, 0)
        self.assertEqual(m._drift_lead_ms(), 0)

    def test_local_wake_delay_is_not_learned_as_network_delay(self):
        m = monitor()
        result = m._record_drift(150, 1000, 1095, 1205, request_lead_ms=55)
        self.assertEqual(result['feedback_reason'], 'local_schedule_delay')
        self.assertFalse(result['update_applied'])

    def test_hold_rescue_is_removed_from_network_error(self):
        m = monitor()
        result = m._record_drift(200, 1000, 1145, 1255, request_lead_ms=55, account_offset_ms=200)
        self.assertEqual(result['feedback_reason'], 'on_target')
        self.assertEqual(m._drift_lead_ms(), 0)

    def test_ambiguous_arrival_never_invents_a_direction(self):
        m = monitor()
        result = m._record_drift(10, 1000, 945, 1055, request_lead_ms=55)
        self.assertFalse(result['update_applied'])
        self.assertEqual(result['direction'], 'ambiguous')

    def test_direction_reversal_clears_stale_residual_history(self):
        m = monitor()
        m._vps_feedback_sign = 1
        m._vps_feedback_history = [300, 300, 300]
        m._drift_ms, m._drift_samples = 200, 3
        result = m._record_drift(200, 1000, 745, 855, request_lead_ms=55)
        self.assertEqual(result['history_size'], 1)
        self.assertLess(m._drift_ms, 200)

    def test_spikes_then_recovery_do_not_poison_later_windows(self):
        # The same network conditions drive both controllers. Every decision
        # only sees completed earlier requests; future delays are unknown.
        queue_ms = [0, 0, 500, 550, 450, 0, 0, 0, 0, 0, 0, 0]
        results = []
        for cls in [WorldBossMonitor, VpsMonitor]:
            m = monitor(cls)
            perfects = 0
            for i, queue in enumerate(queue_ms):
                center = 3000 + i * 5000
                sent = center - 55 - m._drift_lead_ms()
                arrival = sent + 55 + queue
                completed = arrival + 55
                delta = abs(arrival - center)
                perfects += delta <= 210
                m._record_drift(delta, center, sent, completed, request_lead_ms=55)
            results.append(perfects)
        self.assertEqual(results[1], len(queue_ms) - 3)
        self.assertGreater(results[1], results[0])

    def test_legacy_and_zero_rtt_compatibility_remains(self):
        for args, options in [((100,), {}), ((100, 1000, 1000, 1000), {})]:
            old, new = monitor(WorldBossMonitor), monitor()
            old._record_drift(*args, **options)
            new._record_drift(*args, **options)
            self.assertEqual(new._drift_lead_ms(), old._drift_lead_ms())


class HoldFeedbackTests(unittest.TestCase):
    def test_fast_comparable_requests_are_still_learned(self):
        self.assertTrue(VpsMonitor._hold_feedback_quality(trace(110), trace(125), 55)['eligible'])

    def test_vps_queue_spikes_are_not_persistent_hold_skew(self):
        for charge, hit in [(110, 242), (110, 260), (869, 114), (110, 390)]:
            with self.subTest(charge=charge, hit=hit):
                self.assertFalse(VpsMonitor._hold_feedback_quality(trace(charge), trace(hit), 55)['eligible'])

    def test_limits_scale_with_a_slower_but_stable_connection(self):
        self.assertTrue(VpsMonitor._hold_feedback_quality(trace(305), trace(315), 155)['eligible'])

    def test_retried_requests_are_not_learned(self):
        self.assertFalse(VpsMonitor._hold_feedback_quality(trace(110, 2), trace(110), 55)['eligible'])

    def test_target_and_skew_are_clamped_together(self):
        m = monitor()
        self.assertEqual(m._planned_hold_ms(), TARGET_SERVER_HOLD_MS)
        m._hold_skew_ms = 700
        self.assertEqual(m._planned_hold_ms(), 560)
        m._hold_skew_ms = -700
        self.assertEqual(m._planned_hold_ms(), 1210)


class RuntimeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_production_adapter_selects_vps_timing_and_preserves_requests(self):
        clock = FakeClock()
        api = BattleFixture(clock, count=3)
        runtime = ProtocolRuntime(account=api.account, username='fixture', transport=api,
                                  monotonic=clock.now, sleep=clock.sleep)
        result = await runtime.run(WebViewAuth(api.account, 'fixture', 'fixture', 'a' * 64))
        self.assertIsInstance(runtime._monitor, VpsTimingMixin)
        self.assertTrue(result['finish_confirmed'])
        self.assertEqual(api.calls.count('charge-start'), 3)
        self.assertEqual(api.calls.count('hit'), 3)
        self.assertEqual(api.calls.count('finish'), 1)


if __name__ == '__main__':
    unittest.main()
