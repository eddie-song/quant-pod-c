from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from kalshi_as.execution import ExecutionEvent
from kalshi_as.execution_monitor import ExecutionMonitor


class _FakeClient:
    def __init__(self) -> None:
        # status -> list of orders
        self.pages: Dict[str, List[Dict[str, Any]]] = {"resting": [], "filled": [], "canceled": []}

    def get_orders(self, *, status: Optional[str] = None, ticker: Optional[str] = None, limit: int = 200, cursor: Optional[str] = None) -> Dict[str, Any]:  # noqa: E501
        s = status or ""
        orders = list(self.pages.get(s, []))
        if ticker:
            orders = [o for o in orders if o.get("ticker") == ticker]
        return {"orders": orders}


def test_monitor_tracks_and_emits_terminal_event(tmp_path: Path):
    c = _FakeClient()
    mon = ExecutionMonitor(c, event_log_path=str(tmp_path / "events.jsonl"), poll_s=1.0)

    # Ingest a placed order (live has order_id).
    mon.ingest_events(
        [
            ExecutionEvent(
                ts_utc="t",
                market_ticker="T",
                event="place",
                action="buy",
                price_cents=40,
                contracts=1,
                order_id="OID1",
                mid_cents=50,
                delta_cents_from_mid=10,
            )
        ]
    )

    # Initially resting.
    c.pages["resting"] = [{"order_id": "OID1", "ticker": "T", "side": "yes", "action": "buy", "type": "limit", "yes_price": 40, "remaining_count": 2}]
    assert mon.poll_once() == []

    # Partial fill while still resting: remaining_count goes down.
    c.pages["resting"] = [{"order_id": "OID1", "ticker": "T", "side": "yes", "action": "buy", "type": "limit", "yes_price": 40, "remaining_count": 1}]
    rows_pf = mon.poll_once()
    assert any(r.get("type") == "partial_fill" for r in rows_pf)

    # Then filled: no longer resting, appears in filled.
    c.pages["resting"] = []
    c.pages["filled"] = [{"order_id": "OID1", "ticker": "T", "yes_price": 40, "filled_count": 1}]
    rows = mon.poll_once()
    assert any(r.get("terminal_status") == "filled" for r in rows)

