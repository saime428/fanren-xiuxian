"""Cubic Bézier trajectories with arc-length easing and Gaussian micro-jitter."""

from bisect import bisect_left
from dataclasses import dataclass
import math
import random
import time
from typing import Callable

from .distributions import finite

Point = tuple[float, float]


@dataclass(frozen=True)
class MouseSample:
    time: float
    x: float
    y: float


@dataclass(frozen=True)
class MousePath:
    samples: tuple[MouseSample, ...]
    controls: tuple[Point, Point, Point, Point]


def sample_target(
    center: Point, radius: float, *, sigma: float | None = None,
    rng: random.Random | None = None,
) -> Point:
    """Isotropic 2D Gaussian conditioned on lying in a disk.

    Its radius has a truncated Rayleigh distribution. Inverse sampling avoids
    clipping (which would pile up samples on the circumference).
    """
    finite(center[0], "center.x")
    finite(center[1], "center.y")
    finite(radius, "radius", minimum=0)
    sigma = radius / 3 if sigma is None else sigma
    finite(sigma, "sigma", minimum=0)
    if radius == 0 or sigma == 0:
        return center
    rng = rng if rng is not None else random.Random()
    ratio = radius / sigma
    mass = -math.expm1(-0.5 * ratio * ratio)
    if mass == 0:
        # Limiting distribution when sigma dwarfs the radius.
        distance = radius * math.sqrt(rng.random())
    else:
        distance = sigma * math.sqrt(-2 * math.log1p(-rng.random() * mass))
    angle = rng.random() * math.tau
    return center[0] + distance * math.cos(angle), center[1] + distance * math.sin(angle)


def _bezier(controls: tuple[Point, Point, Point, Point], t: float) -> Point:
    u = 1 - t
    weights = (u ** 3, 3 * u * u * t, 3 * u * t * t, t ** 3)
    return tuple(sum(weight * point[axis] for weight, point in zip(weights, controls))
                 for axis in (0, 1))


def generate_mouse_path(
    start: Point, target: Point, *, radius: float = 3.0,
    target_sigma: float | None = None, duration: float = 0.6, samples: int = 61,
    control_sigma: float | None = None, jitter_sigma: float = 0.35,
    rng: random.Random | None = None,
) -> MousePath:
    """Return timestamped samples. Times and duration are seconds.

    Equal time steps follow smoothstep progress along approximate arc length.
    Independent Gaussian sample noise fades to zero at both endpoints. Easing
    describes the underlying trajectory; noise adds local speed variation.
    """
    for name, value in (("start.x", start[0]), ("start.y", start[1]),
                        ("target.x", target[0]), ("target.y", target[1])):
        finite(value, name)
    finite(duration, "duration", minimum=0)
    finite(jitter_sigma, "jitter_sigma", minimum=0)
    if duration == 0:
        raise ValueError("duration must be positive")
    if isinstance(samples, bool) or not isinstance(samples, int) or samples < 3:
        raise ValueError("samples must be an integer >= 3")
    rng = rng if rng is not None else random.Random()
    endpoint = sample_target(target, radius, sigma=target_sigma, rng=rng)
    distance = math.dist(start, endpoint)
    control_sigma = distance * 0.12 if control_sigma is None else control_sigma
    finite(control_sigma, "control_sigma", minimum=0)
    controls = (start, *(tuple(
        start[axis] + fraction * (endpoint[axis] - start[axis]) + rng.gauss(0, control_sigma)
        for axis in (0, 1)
    ) for fraction in (1 / 3, 2 / 3)), endpoint)
    # Reparameterize by arc length so random controls do not dictate speed.
    resolution = max(512, samples * 4)
    lookup = [_bezier(controls, i / resolution) for i in range(resolution + 1)]
    lengths = [0.0]
    for previous, current in zip(lookup, lookup[1:]):
        lengths.append(lengths[-1] + math.dist(previous, current))
    result = []
    for i in range(samples):
        phase = i / (samples - 1)
        eased = phase * phase * (3 - 2 * phase)
        if i == 0:
            x, y = start
        elif i == samples - 1:
            x, y = endpoint
        else:
            wanted = eased * lengths[-1]
            index = min(resolution, max(1, bisect_left(lengths, wanted)))
            span = lengths[index] - lengths[index - 1]
            fraction = (wanted - lengths[index - 1]) / span if span else 0.0
            t = (index - 1 + fraction) / resolution
            x, y = _bezier(controls, t)
            envelope = math.sin(math.pi * phase) ** 2
            x += rng.gauss(0, jitter_sigma) * envelope
            y += rng.gauss(0, jitter_sigma) * envelope
        result.append(MouseSample(phase * duration, x, y))
    return MousePath(tuple(result), controls)


def play_mouse_path(
    path: MousePath, move_to: Callable[[float, float], object], *,
    sleep: Callable[[float], object] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Replay using absolute deadlines, avoiding accumulated callback drift."""
    started = clock()
    for sample in path.samples:
        remaining = started + sample.time - clock()
        if remaining > 0:
            sleep(remaining)
        move_to(sample.x, sample.y)
