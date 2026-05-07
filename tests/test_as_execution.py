from __future__ import annotations

from typing import Any, Dict, List, Optional

from kalshi_as.execution import ExecutionEngine, QuoteIntent


class _FakeClient:
    def __init__(self, orders: Optional[List[Dict[str, Any]]] = None) -> None:
        self._orders = list(orders or [])
        self.created: List[Dict[str, Any]] = []
        self.canceled: List[str] = []

    def get_orders(self, *, status: Optional[str] = None, ticker: Optional[str] = None, limit: int = 200, cursor: Optional[str] = None) -> Dict[str, Any]:  # noqa: E501
        # Filter by ticker if provided.
        out = self._orders
        if ticker:
            out = [o for o in out if o.get("ticker") == ticker]
        return {"orders": out}

    def create_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.created.append(payload)
        # Simulate response containing order_id.
        return {"order_id": f"oid_{len(self.created)}"}

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        self.canceled.append(order_id)
        return {"ok": True}


def test_execution_dry_run_does_not_call_api():
    c = _FakeClient(
        orders=[
            {"order_id": "a", "client_order_id": "as:T:buy:x", "ticker": "T", "side": "yes", "action": "buy", "type": "limit", "yes_price": 40, "remaining_count": 1},
            {"order_id": "b", "client_order_id": "as:T:sell:x", "ticker": "T", "side": "yes", "action": "sell", "type": "limit", "yes_price": 60, "remaining_count": 1},
        ]
    )
    eng = ExecutionEngine(c, mode="dry-run", max_contracts=10)
    acts = eng.sync_quotes(QuoteIntent(market_ticker="T", yes_bid_cents=41, yes_ask_cents=59, contracts=2))
    assert any("would cancel" in a for a in acts)
    assert any("would place" in a for a in acts)
    assert c.created == []
    assert c.canceled == []


def test_execution_live_cancels_and_places():
    c = _FakeClient(
        orders=[
            {"order_id": "a", "client_order_id": "as:T:buy:x", "ticker": "T", "side": "yes", "action": "buy", "type": "limit", "yes_price": 40, "remaining_count": 1},
            {"order_id": "b", "client_order_id": "as:T:sell:x", "ticker": "T", "side": "yes", "action": "sell", "type": "limit", "yes_price": 60, "remaining_count": 1},
        ]
    )
    eng = ExecutionEngine(c, mode="live", max_contracts=10)
    acts = eng.sync_quotes(QuoteIntent(market_ticker="T", yes_bid_cents=41, yes_ask_cents=59, contracts=2))
    assert "a" in c.canceled and "b" in c.canceled
    assert len(c.created) == 2
    assert any("placed buy" in a for a in acts)
    assert any("placed sell" in a for a in acts)


def test_execution_keeps_matching_order():
    c = _FakeClient(
        orders=[
            {"order_id": "a", "client_order_id": "as:T:buy:x", "ticker": "T", "side": "yes", "action": "buy", "type": "limit", "yes_price": 41, "remaining_count": 2},
        ]
    )
    eng = ExecutionEngine(c, mode="live", max_contracts=10)
    acts = eng.sync_quotes(QuoteIntent(market_ticker="T", yes_bid_cents=41, yes_ask_cents=59, contracts=2))
    assert any("keep buy" in a for a in acts)

