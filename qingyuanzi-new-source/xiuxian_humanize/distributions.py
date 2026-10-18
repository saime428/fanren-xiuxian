"""Bounded random delays, in seconds; rejection sampling avoids boundary spikes."""

from dataclasses import dataclass
import math
import random
from statistics import NormalDist
from typing import Literal


def finite(value: float, name: str, *, minimum: float | None = None) -> None:
    if not math.isfinite(value) or (minimum is not None and value < minimum):
        raise ValueError(f"{name} must be finite and >= {minimum}")


def truncated_normal(
    rng: random.Random, mean: float, stddev: float, lower: float, upper: float
) -> float:
    """Sample a conditional Gaussian, never clamp an outlier onto a boundary."""
    for name, value in (("mean", mean), ("lower", lower), ("upper", upper)):
        finite(value, name)
    finite(stddev, "stddev", minimum=0)
    if lower > upper:
        raise ValueError("lower must be <= upper")
    if stddev == 0:
        if not lower <= mean <= upper:
            raise ValueError("zero-variance mean must lie inside the bounds")
        return mean
    if lower == upper:
        return lower
    for _ in range(128):
        value = rng.gauss(mean, stddev)
        if lower <= value <= upper:
            return value
    # Narrow intervals should not cause unbounded rejection loops.
    normal = NormalDist(mean, stddev)
    lo, hi = normal.cdf(lower), normal.cdf(upper)
    if not 0 <= lo < hi <= 1:
        raise ValueError("truncation interval has negligible representable probability")
    probability = lo + (hi - lo) * rng.random()
    probability = max(math.nextafter(0.0, 1.0), min(math.nextafter(1.0, 0.0), probability))
    value = normal.inv_cdf(probability)
    if not lower <= value <= upper:
        raise ValueError("truncation interval is too narrow for floating-point sampling")
    return value


@dataclass(frozen=True)
class DelayDistribution:
    """Mean/stddev describe the underlying distribution BEFORE truncation.

    For lognormal, these are arithmetic seconds, not log-space parameters.
    """

    mean: float = 0.25
    stddev: float = 0.06
    minimum: float = 0.08
    maximum: float = 0.6
    kind: Literal["normal", "lognormal"] = "normal"

    def __post_init__(self) -> None:
        for name in ("mean", "stddev", "minimum", "maximum"):
            finite(getattr(self, name), name, minimum=0)
        if self.minimum > self.maximum:
            raise ValueError("minimum must be <= maximum")
        if self.kind not in ("normal", "lognormal"):
            raise ValueError("kind must be 'normal' or 'lognormal'")
        if self.stddev == 0 and not self.minimum <= self.mean <= self.maximum:
            raise ValueError("zero-variance mean must lie inside the bounds")
        if self.kind == "lognormal" and (self.mean <= 0 or self.minimum <= 0):
            raise ValueError("lognormal mean and minimum must be positive")

    def sample(self, rng: random.Random | None = None) -> float:
        rng = rng if rng is not None else random.Random()
        if self.kind == "normal":
            return truncated_normal(rng, self.mean, self.stddev, self.minimum, self.maximum)
        if self.stddev == 0 or self.minimum == self.maximum:
            return self.mean if self.stddev == 0 else self.minimum
        sigma_squared = math.log1p((self.stddev / self.mean) ** 2)
        mu = math.log(self.mean) - sigma_squared / 2
        return math.exp(truncated_normal(
            rng, mu, math.sqrt(sigma_squared), math.log(self.minimum), math.log(self.maximum)
        ))
