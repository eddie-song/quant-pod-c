from __future__ import annotations

import math
import statistics
from collections import deque
from typing import Deque, Optional


def _log_returns(mids: list[float]) -> list[float]:
    out: list[float] = []
    for i in range(1, len(mids)):
        a, b = mids[i - 1], mids[i]
        if a <= 0 or b <= 0:
            continue
        out.append(math.log(b / a))
    return out


def estimate_sigma_per_sqrt_hour(
    mid_history: Deque[float],
    *,
    sample_interval_s: float,
    min_samples: int = 8,
    floor: float = 0.02,
    cap: float = 5.0,
) -> Optional[float]:
    """Rough σ scaling: per √hour volatility of the mid from evenly spaced snapshots.

    Uses sample standard deviation of log returns times √(samples per hour).
    Returns None if history is too short.
    """
    if len(mid_history) < min_samples:
        return None
    mids = [float(x) for x in mid_history if x > 0]
    if len(mids) < min_samples:
        return None
    rets = _log_returns(mids)
    if len(rets) < min_samples - 1:
        return None
    try:
        sd = statistics.stdev(rets)
    except statistics.StatisticsError:
        return None

    dt = max(sample_interval_s, 1e-3)
    per_hour = math.sqrt(3600.0 / dt)
    sigma = float(sd * per_hour)
    return min(max(sigma, floor), cap)
