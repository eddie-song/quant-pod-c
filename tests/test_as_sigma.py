from __future__ import annotations

from collections import deque

from kalshi_as.sigma import estimate_sigma_per_sqrt_hour


def test_estimate_sigma_none_until_enough_points():
    d: deque[float] = deque([0.5, 0.51], maxlen=80)
    assert estimate_sigma_per_sqrt_hour(d, sample_interval_s=5.0, min_samples=12) is None


def test_estimate_sigma_returns_float_when_warmed():
    # Upward drift: positive log returns → non-zero stdev
    mids = [0.50 + 0.01 * i for i in range(20)]
    d: deque[float] = deque(mids, maxlen=80)
    sigma = estimate_sigma_per_sqrt_hour(
        d, sample_interval_s=5.0, min_samples=8, floor=0.001, cap=10.0
    )
    assert sigma is not None
    assert sigma > 0
