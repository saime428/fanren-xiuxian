"""Retry/polling primitives for spreading client load over time."""

from collections.abc import Mapping
import math
import random

from .distributions import finite


def full_jitter(
    attempt: int, *, base: float = 0.5, cap: float = 30.0,
    rng: random.Random | None = None,
) -> float:
    """U(0, min(cap, base * 2**attempt)); attempt=0 is the first retry."""
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0:
        raise ValueError("attempt must be a nonnegative integer")
    finite(base, "base", minimum=0)
    finite(cap, "cap", minimum=0)
    if base == 0 or cap == 0:
        return 0.0
    try:
        ceiling = min(cap, math.ldexp(base, attempt))
    except OverflowError:
        ceiling = cap
    rng = rng if rng is not None else random.Random()
    return rng.random() * ceiling


def shuffle_headers(
    headers: Mapping[str, str], *, rng: random.Random | None = None
) -> dict[str, str]:
    """Copy and permute insertion order without modifying names or values.

    Actual wire order remains the HTTP transport's responsibility.
    """
    rng = rng if rng is not None else random.Random()
    items = list(headers.items())
    rng.shuffle(items)
    return dict(items)
