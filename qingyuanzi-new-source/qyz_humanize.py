"""Qingyuanzi jitter profiles and the native browser's input adapter.

Only waiting/polling cadence is randomized. Server battle timestamps, measured
charge holds, request budgets, authentication and account ownership stay intact.
"""
from __future__ import annotations

import random
import time

from xiuxian_humanize import (
    DelayDistribution, TimingJitter, full_jitter, generate_mouse_path, play_mouse_path,
)

HUMANIZATION_VERSION = "qyz-humanization-v1"


class Humanization:
    """One instance per account/runtime/browser; never a fleet-wide seed."""

    def __init__(self, rng=None):
        self.rng = rng if rng is not None else random.Random()
        self.operations = TimingJitter(rng=self.rng)

    def interval(self, ceiling):
        # Keep health/admission/keep-alive waits within their existing ceiling.
        return DelayDistribution(ceiling * .9, ceiling * .06, ceiling * .75, ceiling).sample(self.rng)

    def window_poll(self):
        # No faster than the official 360 ms start-to-start polling interval.
        return DelayDistribution(.375, .007, .36, .39).sample(self.rng)

    def retry_delay(self, attempt, *, base=.25, cap=5):
        return full_jitter(attempt, base=base, cap=max(0, cap - .1), rng=self.rng) + (
            DelayDistribution(.05, .015, .01, .1).sample(self.rng)
        )


class BrowserInput:
    """Execute each reference click as an atomic down/hold/up gesture.

    The reference sends move, press, release with fixed sleeps in between. The
    local adapter completes the gesture on press and consumes its paired release,
    so the REAL down/up duration comes from our distribution. Reference sleeps
    remain outside the held-button interval; the protected source is unchanged.
    """

    def __init__(self, *, rng=None, sleep=time.sleep, clock=time.monotonic):
        self.profile = Humanization(rng)
        self.sleep, self.clock = sleep, clock
        self.position = (0.0, 0.0)
        self._moved = False
        self._paired_release = False
        self._release_result = None

    def dispatch(self, params, send):
        kind = params.get("type")
        if kind == "mouseMoved":
            self._paired_release = False
            duration = DelayDistribution(.45, .08, .25, .7).sample(self.profile.rng)
            path = generate_mouse_path(
                self.position, (float(params["x"]), float(params["y"])), radius=3,
                duration=duration, samples=25, jitter_sigma=.2, rng=self.profile.rng,
            )

            def move(x, y):
                result = send({**params, "x": x, "y": y})
                self.position = (x, y)
                return result

            play_mouse_path(path, move, sleep=self.sleep, clock=self.clock)
            self._moved = True
            return {}
        if kind == "mousePressed" and params.get("button") == "left":
            if not self._moved:
                self.position = (float(params["x"]), float(params["y"]))
            point = {"x": self.position[0], "y": self.position[1]}
            self._moved = False
            self._paired_release = False

            def release():
                self._release_result = send({**params, **point, "type": "mouseReleased", "buttons": 0})
                self._paired_release = True

            self.profile.operations.press_and_release(
                lambda: send({**params, **point}), release, sleep=self.sleep,
            )
            self.profile.operations.wait_after_operation(sleep=self.sleep)
            return {}
        if kind == "mouseReleased" and params.get("button") == "left" and self._paired_release:
            self._paired_release = False
            return self._release_result
        return send(params)
