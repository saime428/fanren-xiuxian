"""Seeded statistical checks and offline integration tests; no game requests."""
import math
from pathlib import Path
import random
import statistics
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from qyz_humanize import BrowserInput, Humanization
from qyz_http import HttpTransport
from qyz_protocol import ProtocolError, ProtocolRuntime
from qyz_verification import PreparedNativeTurnstileBrowser
from xiuxian_humanize import (
    DelayDistribution, TimingJitter, full_jitter, generate_mouse_path, play_mouse_path, sample_target,
)


class FakeClock:
    def __init__(self):
        self.value = 0.0
        self.waits = []

    def now(self):
        return self.value

    def sleep(self, duration):
        self.waits.append(duration)
        self.value += duration

    async def asleep(self, duration):
        self.sleep(duration)


class DistributionTests(unittest.TestCase):
    def test_hold_matches_gaussian_moments_without_boundary_piles(self):
        jitter = TimingJitter(rng=random.Random(4))
        values = [jitter.hold_duration() for _ in range(20000)]
        self.assertAlmostEqual(statistics.mean(values), .085, delta=.0005)
        self.assertAlmostEqual(statistics.stdev(values), .015, delta=.0005)
        self.assertTrue(all(.03 < x < .15 for x in values))

    def test_lognormal_uses_arithmetic_seconds_and_is_truncated(self):
        rng = random.Random(7)
        distribution = DelayDistribution(.4, .2, .02, 4, kind="lognormal")
        values = [distribution.sample(rng) for _ in range(12000)]
        self.assertAlmostEqual(statistics.mean(values), .4, delta=.01)
        self.assertAlmostEqual(statistics.stdev(values), .2, delta=.01)
        self.assertGreater(statistics.mean(values), statistics.median(values))
        self.assertTrue(all(.02 < x < 4 for x in values))

    def test_normal_interval_is_truncated_not_clipped(self):
        rng = random.Random(9)
        values = [DelayDistribution(.25, .2, .2, .3).sample(rng) for _ in range(4000)]
        self.assertTrue(all(.2 < x < .3 for x in values))
        self.assertAlmostEqual(statistics.mean(values), .25, delta=.002)

    def test_micro_break_counts_and_durations(self):
        timing = TimingJitter(rng=random.Random(41))
        previous = 0
        gaps, pauses = [], []
        for count in range(1, 5001):
            plan = timing.after_operation()
            self.assertTrue(.08 <= plan.interval <= .6)
            self.assertEqual(plan.total, plan.interval + plan.micro_break)
            if plan.micro_break:
                gaps.append(count - previous)
                previous = count
                pauses.append(plan.micro_break)
        self.assertGreater(len(gaps), 120)
        self.assertTrue(all(20 <= gap <= 40 for gap in gaps))
        self.assertGreater(len(set(gaps)), 8)
        self.assertTrue(all(2 <= pause <= 5 for pause in pauses))

    def test_invalid_values_fail_without_sleeping(self):
        for options in ({"stddev": -1}, {"mean": math.nan}, {"maximum": math.inf},
                        {"minimum": 2, "maximum": 1}, {"kind": "uniform"},
                        {"kind": "lognormal", "minimum": 0}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                DelayDistribution(**options)
        with self.assertRaises(ValueError):
            TimingJitter(break_every=(0, 5))
        self.assertEqual(DelayDistribution(.2, 0, .1, .3).sample(), .2)

    def test_full_jitter_exponential_cap_and_distribution(self):
        rng = random.Random(3)
        for attempt, ceiling in ((0, .5), (1, 1), (4, 8), (100000, 10)):
            values = [full_jitter(attempt, base=.5, cap=10, rng=rng) for _ in range(4000)]
            self.assertTrue(all(0 <= value <= ceiling for value in values))
            self.assertAlmostEqual(statistics.mean(values), ceiling / 2, delta=ceiling * .02)
            self.assertAlmostEqual(statistics.pvariance(values), ceiling ** 2 / 12, delta=ceiling ** 2 * .004)
        with self.assertRaises(ValueError):
            full_jitter(-1)

    def test_profiles_respect_existing_ceilings(self):
        profile = Humanization(random.Random(8))
        for ceiling in (.25, 5, 15, 40, 60):
            values = [profile.interval(ceiling) for _ in range(100)]
            self.assertTrue(all(ceiling * .75 <= x <= ceiling for x in values))
            self.assertGreater(len(set(values)), 90)
        self.assertTrue(all(.36 <= profile.window_poll() <= .39 for _ in range(100)))


class MouseTests(unittest.TestCase):
    def test_disk_truncated_gaussian_radial_cdf_and_symmetry(self):
        rng = random.Random(52)
        radius, sigma = 6, 2
        points = [sample_target((0, 0), radius, sigma=sigma, rng=rng) for _ in range(12000)]
        radii = [math.hypot(*point) for point in points]
        self.assertTrue(all(0 < value < radius for value in radii))
        a = radius ** 2 / (2 * sigma ** 2)
        expected_cdf = -math.expm1(-a / 4) / -math.expm1(-a)
        self.assertAlmostEqual(sum(r < radius / 2 for r in radii) / len(radii), expected_cdf, delta=.015)
        expected_variance = sigma ** 2 * (1 - (a + 1) * math.exp(-a)) / (1 - math.exp(-a))
        for axis in (0, 1):
            values = [point[axis] for point in points]
            self.assertAlmostEqual(statistics.mean(values), 0, delta=.06)
            self.assertAlmostEqual(statistics.pvariance(values), expected_variance, delta=.12)
        self.assertAlmostEqual(statistics.mean(x * y for x, y in points), 0, delta=.1)

    def test_curve_eases_along_arc_length_and_is_reproducible(self):
        options = dict(radius=0, control_sigma=70, jitter_sigma=0, duration=1, samples=101)
        path = generate_mouse_path((20, 20), (620, 400), rng=random.Random(5), **options)
        self.assertEqual(path, generate_mouse_path((20, 20), (620, 400), rng=random.Random(5), **options))
        self.assertNotEqual(path.controls, generate_mouse_path((20, 20), (620, 400), rng=random.Random(6), **options).controls)
        steps = [math.hypot(b.x - a.x, b.y - a.y) for a, b in zip(path.samples, path.samples[1:])]
        self.assertLess(statistics.mean(steps[:10]), statistics.mean(steps[45:55]) / 3)
        self.assertLess(statistics.mean(steps[-10:]), statistics.mean(steps[45:55]) / 3)
        self.assertEqual((path.samples[0].x, path.samples[0].y), (20, 20))
        self.assertEqual((path.samples[-1].x, path.samples[-1].y, path.samples[-1].time), (620, 400, 1))

    def test_noise_changes_interior_but_never_endpoint_or_start(self):
        options = dict(radius=4, control_sigma=5, rng=random.Random(6))
        smooth = generate_mouse_path((0, 0), (100, 100), jitter_sigma=0, **options)
        options["rng"] = random.Random(6)
        noisy = generate_mouse_path((0, 0), (100, 100), jitter_sigma=.4, **options)
        self.assertEqual(smooth.controls, noisy.controls)
        self.assertEqual(smooth.samples[0], noisy.samples[0])
        self.assertEqual(smooth.samples[-1], noisy.samples[-1])
        self.assertNotEqual(smooth.samples[30], noisy.samples[30])
        self.assertLess(math.hypot(noisy.samples[-1].x - 100, noisy.samples[-1].y - 100), 4)

    def test_replay_uses_absolute_times_despite_callback_duration(self):
        clock = FakeClock()
        path = generate_mouse_path((0, 0), (100, 100), duration=1, samples=21, rng=random.Random(1))
        sent_at = []
        def move(x, y):
            sent_at.append(clock.now())
            clock.value += .003
        play_mouse_path(path, move, sleep=clock.sleep, clock=clock.now)
        self.assertAlmostEqual(sent_at[-1], 1)
        self.assertAlmostEqual(clock.now(), 1.003)

    def test_bad_mouse_parameters_and_zero_radius(self):
        for options in ({"radius": -1}, {"duration": 0}, {"samples": 2}, {"jitter_sigma": math.nan}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                generate_mouse_path((0, 0), (10, 10), **options)
        self.assertEqual(sample_target((3, 4), 0), (3, 4))

    def test_prepared_browser_emits_one_real_gaussian_hold_at_randomized_endpoint(self):
        clock = FakeClock()
        sent = []
        def send(_browser, method, params=None):
            if method == "Input.dispatchMouseEvent":
                sent.append((clock.now(), dict(params)))
            return {}
        with tempfile.TemporaryDirectory() as directory:
            browser = PreparedNativeTurnstileBrowser(Path(directory))
            browser._human_input = BrowserInput(rng=random.Random(10), sleep=clock.sleep, clock=clock.now)
            with patch("qyz_verification.NativeTurnstileBrowser.command", send):
                browser.command("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 80, "y": 90})
                clock.sleep(.12)  # preserved reference delay, outside actual hold
                browser.command("Input.dispatchMouseEvent", {"type": "mousePressed", "button": "left", "buttons": 1, "x": 80, "y": 90})
                clock.sleep(.12)
                browser.command("Input.dispatchMouseEvent", {"type": "mouseReleased", "button": "left", "buttons": 0, "x": 80, "y": 90})
        moved = [event for _, event in sent if event["type"] == "mouseMoved"]
        presses = [(at, event) for at, event in sent if event["type"] == "mousePressed"]
        releases = [(at, event) for at, event in sent if event["type"] == "mouseReleased"]
        self.assertEqual(len(moved), 25)
        self.assertEqual((len(presses), len(releases)), (1, 1))
        self.assertTrue(.03 <= releases[0][0] - presses[0][0] <= .15)
        point = presses[0][1]
        self.assertLess(math.hypot(point["x"] - 80, point["y"] - 90), 3)
        for event in (moved[-1], releases[0][1]):
            self.assertEqual((event["x"], event["y"]), (point["x"], point["y"]))

    def test_interrupted_hold_still_releases(self):
        events = []
        def interrupted(_duration):
            raise KeyboardInterrupt
        adapter = BrowserInput(rng=random.Random(10), sleep=interrupted)
        with self.assertRaises(KeyboardInterrupt):
            adapter.dispatch({"type": "mousePressed", "button": "left", "x": 1, "y": 2},
                             lambda event: events.append(event["type"]))
        self.assertEqual(events, ["mousePressed", "mouseReleased"])


class AsyncIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_transport_randomizes_merged_headers_without_changing_payload_or_retrying(self):
        requests = []
        async def fixture(request):
            requests.append(request)
            return httpx.Response(200, json={"ok": True})
        body = {"token": "fixture", "initData": "fixture"}
        async with HttpTransport("tg9100000001", http_transport=httpx.MockTransport(fixture)) as api:
            for _ in range(12):
                await api.post_json("state", body)
        self.assertEqual(len(requests), 12)
        self.assertGreater(len({tuple(request.headers) for request in requests}), 1)
        self.assertTrue(all(dict(request.headers) == dict(requests[0].headers) for request in requests))
        self.assertTrue(all(request.content == requests[0].content for request in requests))

    async def test_runtime_retry_respects_action_deadline_and_keeps_request_identical(self):
        clock = FakeClock()
        error = ProtocolError("transport_outcome_unknown", phase="hit", uncertain=True)
        api = type("Api", (), {"post_json": AsyncMock(side_effect=error)})()
        runtime = ProtocolRuntime(account="tg9100000001", username="fixture", transport=api,
                                  monotonic=clock.now, sleep=clock.asleep)
        runtime.humanization = Humanization(random.Random(12))
        runtime.combat.started_at = 0
        runtime.combat.windows["w"] = {"centerMs": 400, "hitMs": 100, "perfectMs": 50}
        body = {"windowId": "w", "chargeTicket": "fixture"}
        with self.assertRaises(ProtocolError):
            await runtime._post_until_reply("hit", body, .1)
        self.assertLess(clock.now(), .58)
        self.assertGreater(api.post_json.await_count, 1)
        self.assertTrue(all(call.args[1] is body for call in api.post_json.await_args_list))
        self.assertTrue(all(.26 <= value <= 10 for value in clock.waits))


if __name__ == "__main__":
    unittest.main()
