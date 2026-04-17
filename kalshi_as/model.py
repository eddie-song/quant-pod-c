from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class ASConfig:
    """Parameters for reduced-form Avellaneda–Stoikov quoting."""

    gamma: float  # risk aversion (> 0)
    k: float  # order-arrival intensity shape (> 0); larger k → tighter theoretical spread
    tau_hours: float  # remaining horizon (T − t) in hours (> 0)
    A: float = 1.0  # calibration parameter placeholder (stored/logged; formula unchanged for now)
    tick: float = 0.01  # Kalshi YES price tick in dollars


@dataclass(frozen=True)
class AvellanedaStoikovQuotes:
    mid: float
    reservation: float
    half_spread: float
    bid: float
    ask: float


def reservation_price(mid: float, inventory_yes: float, gamma: float, sigma: float, tau_hours: float) -> float:
    """Inventory skew: long YES (inventory_yes > 0) lowers the reservation price."""
    return float(mid - inventory_yes * gamma * (sigma**2) * tau_hours)


def optimal_half_spread(gamma: float, k: float, sigma: float, tau_hours: float) -> float:
    """Symmetric half-spread around the reservation price (standard AS reduced form)."""
    if gamma <= 0 or k <= 0:
        raise ValueError("gamma and k must be positive")
    intensity_term = (1.0 / gamma) * math.log1p(gamma / k)
    risk_term = 0.5 * gamma * (sigma**2) * tau_hours
    return float(intensity_term + risk_term)


def _round_to_tick(x: float, tick: float) -> float:
    if tick <= 0:
        return x
    return round(x / tick) * tick


def compute_quotes(
    mid: float,
    *,
    inventory_yes: float,
    sigma: float,
    config: ASConfig,
    tau_hours: float | None = None,
) -> AvellanedaStoikovQuotes:
    """Bid/ask around reservation with symmetric half-spread; clamp to [tick, 1 - tick]."""
    if not (0 < mid < 1):
        raise ValueError("mid must be in (0, 1) for Kalshi YES prices")
    if sigma <= 0:
        raise ValueError("sigma must be positive")
    tau = float(config.tau_hours if tau_hours is None else tau_hours)
    if tau <= 0:
        raise ValueError("tau_hours must be positive")

    v = reservation_price(mid, inventory_yes, config.gamma, sigma, tau)
    h = optimal_half_spread(config.gamma, config.k, sigma, tau)
    bid_raw = v - h
    ask_raw = v + h

    t = config.tick
    lo, hi = t, 1.0 - t
    bid = min(max(_round_to_tick(bid_raw, t), lo), hi)
    ask = min(max(_round_to_tick(ask_raw, t), lo), hi)
    if bid >= ask:
        # Degenerate after clamping: collapse to one tick inside the raw band if possible
        mid_tick = _round_to_tick(v, t)
        bid = min(max(mid_tick - t, lo), hi - t)
        ask = min(max(mid_tick + t, lo + t), hi)

    return AvellanedaStoikovQuotes(
        mid=mid,
        reservation=v,
        half_spread=h,
        bid=bid,
        ask=ask,
    )
