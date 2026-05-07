from __future__ import annotations

from kalshi_as.strategy_loop import _ws_is_stale
from kalshi_ws.models import MarketTicker


def test_ws_is_stale_when_no_updates():
    assert _ws_is_stale({}, now_s=100.0, stale_s=10.0) is True


def test_ws_is_not_stale_when_recent_update():
    mt = MarketTicker(market_ticker="T", yes_bid=0.4, yes_ask=0.6, last_update_ts=95, update_count=1)
    assert _ws_is_stale({"T": mt}, now_s=100.0, stale_s=10.0) is False


def test_ws_is_stale_when_old_update():
    mt = MarketTicker(market_ticker="T", yes_bid=0.4, yes_ask=0.6, last_update_ts=50, update_count=1)
    assert _ws_is_stale({"T": mt}, now_s=100.0, stale_s=10.0) is True

