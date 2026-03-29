from __future__ import annotations

from collections import deque

from kalshi_ws.models import MarketTicker, Trade, _parse_float


# ── Sample messages (mirrors the schemas from the Kalshi WS docs) ────

SAMPLE_TICKER_MSG = {
    "market_ticker": "KXBTC-26APR01-T100000",
    "market_id": "abc-def-123",
    "yes_bid_dollars": "0.52",
    "yes_ask_dollars": "0.58",
    "price_dollars": "0.55",
    "volume_fp": "1234.00",
    "open_interest_fp": "5678.50",
    "dollar_volume": 62000,
    "dollar_open_interest": 31000,
    "ts": 1711746000,
    "time": "2026-03-29T21:00:00Z",
}

SAMPLE_TRADE_MSG = {
    "trade_id": "trade-001",
    "market_ticker": "KXBTC-26APR01-T100000",
    "yes_price_dollars": "0.55",
    "no_price_dollars": "0.45",
    "count_fp": "10.00",
    "taker_side": "yes",
    "ts": 1711746001,
}


# ── Tests ────────────────────────────────────────────────────────────

def test_parse_float_from_string():
    assert _parse_float("0.52") == 0.52
    assert _parse_float("1234.00") == 1234.0


def test_parse_float_from_number():
    assert _parse_float(42) == 42.0
    assert _parse_float(3.14) == 3.14


def test_parse_float_fallback():
    assert _parse_float(None) == 0.0
    assert _parse_float({}) == 0.0


def test_market_ticker_from_msg():
    mt = MarketTicker.from_msg(SAMPLE_TICKER_MSG)

    assert mt.market_ticker == "KXBTC-26APR01-T100000"
    assert mt.yes_bid == 0.52
    assert mt.yes_ask == 0.58
    assert mt.spread == round(0.58 - 0.52, 6)  # 0.06
    assert mt.last_price == 0.55
    assert mt.volume == 1234.0
    assert mt.open_interest == 5678.5
    assert mt.dollar_volume == 62000
    assert mt.dollar_open_interest == 31000
    assert mt.last_update_ts == 1711746000
    assert mt.update_count == 1


def test_market_ticker_spread_derivation():
    mt = MarketTicker.from_msg({"yes_bid_dollars": "0.30", "yes_ask_dollars": "0.70"})
    assert mt.spread == 0.4


def test_market_ticker_update_partial():
    mt = MarketTicker.from_msg(SAMPLE_TICKER_MSG)
    mt.update({"yes_bid_dollars": "0.60", "ts": 1711746005})

    assert mt.yes_bid == 0.60
    assert mt.yes_ask == 0.58  # unchanged
    assert mt.spread == round(0.58 - 0.60, 6)  # -0.02 (crossed book)
    assert mt.last_update_ts == 1711746005
    assert mt.update_count == 2


def test_trade_from_msg():
    trade = Trade.from_msg(SAMPLE_TRADE_MSG)

    assert trade.trade_id == "trade-001"
    assert trade.market_ticker == "KXBTC-26APR01-T100000"
    assert trade.yes_price == 0.55
    assert trade.no_price == 0.45
    assert trade.size == 10.0
    assert trade.taker_side == "yes"
    assert trade.ts == 1711746001


def test_deque_maxlen_respected():
    maxlen = 3
    buf: deque[Trade] = deque(maxlen=maxlen)

    for i in range(5):
        msg = dict(SAMPLE_TRADE_MSG)
        msg["trade_id"] = f"trade-{i}"
        buf.append(Trade.from_msg(msg))

    assert len(buf) == maxlen
    # Oldest trades should have been evicted
    assert buf[0].trade_id == "trade-2"
    assert buf[-1].trade_id == "trade-4"
