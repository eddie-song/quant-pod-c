from __future__ import annotations

import pytest

from kalshi_as.model import ASConfig, compute_quotes, optimal_half_spread, reservation_price


def test_reservation_long_yes_lowers_price():
    mid = 0.5
    v0 = reservation_price(mid, 0.0, gamma=0.1, sigma=0.2, tau_hours=1.0)
    v_long = reservation_price(mid, 10.0, gamma=0.1, sigma=0.2, tau_hours=1.0)
    assert v_long < v0


def test_half_spread_positive():
    h = optimal_half_spread(gamma=0.1, k=1.5, sigma=0.15, tau_hours=2.0)
    assert h > 0


def test_quotes_symmetric_around_reservation_no_inventory():
    # Keep half-spread small enough that quotes are not pinned to tick bounds.
    cfg = ASConfig(gamma=0.05, k=4.0, tau_hours=0.5, tick=0.01)
    q = compute_quotes(0.55, inventory_yes=0.0, sigma=0.08, config=cfg)
    assert abs((q.bid + q.ask) / 2.0 - q.reservation) < 0.02  # rounding can shift slightly


def test_quotes_invalid_mid():
    cfg = ASConfig(gamma=0.05, k=1.5, tau_hours=3.0, tick=0.01)
    with pytest.raises(ValueError):
        compute_quotes(1.0, inventory_yes=0.0, sigma=0.2, config=cfg)
