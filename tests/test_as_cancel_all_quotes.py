from __future__ import annotations

from typing import Any, Dict, List, Optional

from kalshi_as.execution import ExecutionEngine


class _FakeClient:
    def __init__(self, orders: List[Dict[str, Any]]) -> None:
        self._orders = orders
        self.canceled: List[str] = []

    def get_orders(self, *, status: Optional[str] = None, ticker: Optional[str] = None, limit: int = 200, cursor: Optional[str] = None) -> Dict[str, Any]:  # noqa: E501
        out = self._orders
        if ticker:
            out = [o for o in out if o.get("ticker") == ticker]
        return {"orders": out}

    def create_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raise AssertionError("should not create")

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        self.canceled.append(order_id)
        return {"ok": True}


def test_cancel_all_quotes_live():
    c = _FakeClient(
        orders=[
            {"order_id": "a", "client_order_id": "as:T:buy:x", "ticker": "T", "side": "yes", "action": "buy", "type": "limit", "yes_price": 40, "remaining_count": 1},
            {"order_id": "b", "client_order_id": "as:T:sell:x", "ticker": "T", "side": "yes", "action": "sell", "type": "limit", "yes_price": 60, "remaining_count": 1},
            {"order_id": "c", "client_order_id": "manual", "ticker": "T", "side": "yes", "action": "buy", "type": "limit", "yes_price": 41, "remaining_count": 1},
        ]
    )
    eng = ExecutionEngine(c, mode="live", max_contracts=10)
    events, msgs = eng.cancel_all_quotes_events(ticker="T", mid_cents=50)
    assert set(c.canceled) == {"a", "b"}
    assert len(events) == 2
    assert any("canceled" in m for m in msgs)


def test_cancel_all_quotes_dry_run_does_not_cancel():
    c = _FakeClient(
        orders=[
            {"order_id": "a", "client_order_id": "as:T:buy:x", "ticker": "T", "side": "yes", "action": "buy", "type": "limit", "yes_price": 40, "remaining_count": 1},
        ]
    )
    eng = ExecutionEngine(c, mode="dry-run", max_contracts=10)
    events, msgs = eng.cancel_all_quotes_events(ticker="T", mid_cents=50)
    assert c.canceled == []
    assert len(events) == 1
    assert any("would cancel" in m for m in msgs)

