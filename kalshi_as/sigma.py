from __future__ import annotations

import math
from typing import Deque, Optional


def _log_returns(mids: list[float]) -> list[float]:
    out: list[float] = []
    for i in range(1, len(mids)):
        a, b = mids[i - 1], mids[i]
        if a <= 0 or b <= 0:
            continue
        out.append(math.log(b / a))
    return out


def _ewma_series(values: list[float], alpha: float) -> list[float]:
    if not values:
        return []
    alpha = min(max(alpha, 1e-3), 1.0)
    out = [values[0]]
    for value in values[1:]:
        out.append(alpha * value + (1.0 - alpha) * out[-1])
    return out


def _ewma_std(values: list[float], alpha: float) -> Optional[float]:
    if len(values) < 2:
        return None
    alpha = min(max(alpha, 1e-3), 1.0)
    mean = values[0]
    variance = 0.0
    for value in values[1:]:
        prev_mean = mean
        mean = alpha * value + (1.0 - alpha) * mean
        innovation = value - prev_mean
        variance = (1.0 - alpha) * (variance + alpha * innovation * innovation)
    return math.sqrt(max(variance, 0.0))


def estimate_sigma_per_sqrt_hour(
    mid_history: Deque[float],
    *,
    sample_interval_s: float,
    min_samples: int = 8,
    floor: float = 0.02,
    cap: float = 5.0,
    ewma_alpha: float = 0.25,
) -> Optional[float]:
    """Estimate per-√hour mid volatility from evenly spaced snapshots.

    Uses an EWMA-smoothed log-return standard deviation times √(samples per hour)
    so a single noisy tick does not dominate the estimate.
    Returns None if history is too short.
    """
    if len(mid_history) < min_samples:
        return None
    mids = [float(x) for x in mid_history if x > 0]
    if len(mids) < min_samples:
        return None
    smoothed_mids = _ewma_series(mids, ewma_alpha)
    rets = _log_returns(smoothed_mids)
    if len(rets) < min_samples - 1:
        return None
    sd = _ewma_std(rets, ewma_alpha)
    if sd is None:
        return None

    dt = max(sample_interval_s, 1e-3)
    per_hour = math.sqrt(3600.0 / dt)
    sigma = float(sd * per_hour)
    return min(max(sigma, floor), cap)
