"""Operation pacing, key/button hold time, and occasional micro-breaks."""

from dataclasses import dataclass
import random
import threading
import time
from typing import Callable

from .distributions import DelayDistribution, truncated_normal


@dataclass(frozen=True)
class WaitPlan:
    interval: float
    micro_break: float = 0.0

    @property
    def total(self) -> float:
        return self.interval + self.micro_break


class TimingJitter:
    """Call after_operation exactly once for each completed UI operation.

    Each instance owns its counter. A lock protects the counter and RNG when
    used by several threads. An externally supplied RNG should not be shared
    with unrelated concurrent callers.
    """

    def __init__(
        self, *, interval: DelayDistribution = DelayDistribution(),
        hold: DelayDistribution = DelayDistribution(0.085, 0.015, 0.03, 0.15),
        micro_break: DelayDistribution = DelayDistribution(3.5, 0.65, 2.0, 5.0),
        break_every: tuple[int, int] = (20, 40), rng: random.Random | None = None,
    ) -> None:
        low, high = break_every
        if any(isinstance(n, bool) or not isinstance(n, int) for n in break_every):
            raise ValueError("break_every bounds must be integers")
        if not 1 <= low <= high:
            raise ValueError("break_every must satisfy 1 <= low <= high")
        self.interval = interval
        self.hold = hold
        self.micro_break = micro_break
        self.break_every = break_every
        self._rng = rng if rng is not None else random.Random()
        self._lock = threading.Lock()
        self._remaining = self._next_break_count()

    def _next_break_count(self) -> int:
        low, high = self.break_every
        if low == high:
            return low
        # Rounded truncated Gaussian, including both integer endpoints.
        return round(truncated_normal(
            self._rng, (low + high) / 2, (high - low) / 6, low - 0.499, high + 0.499
        ))

    def hold_duration(self) -> float:
        with self._lock:
            return self.hold.sample(self._rng)

    def after_operation(self) -> WaitPlan:
        with self._lock:
            interval = self.interval.sample(self._rng)
            self._remaining -= 1
            pause = 0.0
            if self._remaining == 0:
                pause = self.micro_break.sample(self._rng)
                self._remaining = self._next_break_count()
            return WaitPlan(interval, pause)

    def wait_after_operation(self, *, sleep: Callable[[float], object] = time.sleep) -> WaitPlan:
        plan = self.after_operation()
        sleep(plan.total)
        return plan

    def press_and_release(
        self, press: Callable[[], object], release: Callable[[], object], *,
        sleep: Callable[[float], object] = time.sleep,
    ) -> float:
        """Always attempt release if hold sleep raises after a successful press."""
        duration = self.hold_duration()
        press()
        try:
            sleep(duration)
        finally:
            release()
        return duration
