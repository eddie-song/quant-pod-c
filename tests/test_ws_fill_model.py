from __future__ import annotations

from kalshi_ws.models import Fill


def test_fill_from_msg_parses_basic_fields():
    msg = {
        "market_ticker": "T",
        "side": "yes",
        "action": "buy",
        "count_fp": "2.00",
        "yes_price_dollars": "0.40",
        "no_price_dollars": "0.60",
        "ts": 123,
        "order_id": "OID",
        "client_order_id": "as:T:buy:x",
    }
    f = Fill.from_msg(msg)
    assert f.market_ticker == "T"
    assert f.side == "yes"
    assert f.action == "buy"
    assert f.count == 2.0
    assert f.yes_price == 0.40
    assert f.order_id == "OID"

