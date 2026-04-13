from __future__ import annotations

import statistics
import math
from collections import deque

from kalshi_as.sigma import estimate_sigma_per_sqrt_hour


def test_estimate_sigma_none_until_enough_points():
    d: deque[float] = deque([0.5, 0.51], maxlen=80)
    assert estimate_sigma_per_sqrt_hour(d, sample_interval_s=5.0, min_samples=12) is None


def test_estimate_sigma_returns_float_when_warmed():
    mids = [0.50, 0.51, 0.50, 0.515, 0.505, 0.52, 0.51, 0.525, 0.515, 0.53]
    d: deque[float] = deque(mids, maxlen=80)
    sigma = estimate_sigma_per_sqrt_hour(
        d, sample_interval_s=5.0, min_samples=8, floor=0.001, cap=10.0
    )
    assert sigma is not None
    assert sigma > 0


def test_estimate_sigma_ewma_softens_single_noisy_tick():
    mids = [0.5, 0.5004, 0.4998, 0.5002, 0.4999, 0.5001, 0.5, 0.525]
    d: deque[float] = deque(mids, maxlen=80)

    sigma = estimate_sigma_per_sqrt_hour(
        d, sample_interval_s=5.0, min_samples=8, floor=0.0, cap=10.0, ewma_alpha=0.2
    )

    rets = [math.log(mids[i] / mids[i - 1]) for i in range(1, len(mids))]
    unsmoothed = statistics.stdev(rets) * math.sqrt(3600.0 / 5.0)

    assert sigma is not None
    assert sigma < unsmoothed


def test_estimate_sigma_respects_floor_and_cap():
    flat: deque[float] = deque([0.5] * 10, maxlen=80)
    low_sigma = estimate_sigma_per_sqrt_hour(
        flat, sample_interval_s=5.0, min_samples=8, floor=0.05, cap=10.0
    )
    assert low_sigma == 0.05

    jumpy: deque[float] = deque([0.2, 0.8] * 6, maxlen=80)
    high_sigma = estimate_sigma_per_sqrt_hour(
        jumpy, sample_interval_s=5.0, min_samples=8, floor=0.0, cap=0.3
    )
    assert high_sigma == 0.3
