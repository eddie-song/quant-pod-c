from __future__ import annotations

import pytest

from kalshi_as.calibration_log import build_calibration_record


def test_build_calibration_record_shape():
    r = build_calibration_record(
        market_ticker="KXTEST-YES",
        mid=0.50,
        model_bid=0.48,
        model_ask=0.53,
        A=2.0,
        k=1.5,
        gamma=0.05,
    )
    assert r["market_ticker"] == "KXTEST-YES"
    assert r["bid_distance_from_mid"] == pytest.approx(0.02)
    assert r["ask_distance_from_mid"] == pytest.approx(0.03)
    assert r["A"] == 2.0
