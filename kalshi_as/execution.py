from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol


class _KalshiOrdersClient(Protocol):
    def get_orders(self, *, status: Optional[str] = None, ticker: Optional[str] = None, limit: int = 200, cursor: Optional[str] = None) -> Dict[str, Any]:  # noqa: E501
        ...

    def create_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        ...

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        ...


@dataclass(frozen=True)
class QuoteIntent:
    market_ticker: str
    yes_bid_cents: int
    yes_ask_cents: int
    contracts: int
    mid_cents: Optional[int] = None
    post_only: bool = True
    time_in_force: str = "good_till_canceled"

@dataclass(frozen=True)
class ExecutionEvent:
    ts_utc: str
    market_ticker: str
    event: str  # keep|cancel|place|skip
    action: Optional[str] = None  # buy|sell
    side: str = "yes"
    price_cents: Optional[int] = None
    contracts: Optional[int] = None
    order_id: Optional[str] = None
    mid_cents: Optional[int] = None
    delta_cents_from_mid: Optional[int] = None


def _extract_order_id(order: Dict[str, Any]) -> str:
    # Kalshi responses vary; handle common shapes.
    for key in ("order_id", "id"):
        v = order.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    # Some responses wrap:
    inner = order.get("order")
    if isinstance(inner, dict):
        return _extract_order_id(inner)
    return ""

def _extract_client_order_id(order: Dict[str, Any]) -> str:
    v = order.get("client_order_id")
    return v.strip() if isinstance(v, str) and v.strip() else ""


def _order_price_cents(order: Dict[str, Any]) -> Optional[int]:
    # For YES-side limit orders, API commonly returns yes_price or price.
    for key in ("yes_price", "price", "limit_price"):
        v = order.get(key)
        if isinstance(v, int):
            return v
        if isinstance(v, str) and v.isdigit():
            return int(v)
    return None


def _remaining_count(order: Dict[str, Any]) -> Optional[int]:
    for key in ("remaining_count", "remaining", "count"):
        v = order.get(key)
        if isinstance(v, int):
            return v
        if isinstance(v, str) and v.isdigit():
            return int(v)
    return None


def _is_yes_limit(order: Dict[str, Any]) -> bool:
    return str(order.get("side") or "").lower() == "yes" and str(order.get("type") or "").lower() == "limit"


def _is_action(order: Dict[str, Any], action: str) -> bool:
    return str(order.get("action") or "").lower() == action.lower()


def _build_limit_payload(*, ticker: str, action: str, yes_price_cents: int, count: int, post_only: bool, time_in_force: str) -> Dict[str, Any]:
    return {
        "ticker": ticker,
        "side": "yes",
        "action": action,
        "count": int(count),
        "type": "limit",
        "client_order_id": str(uuid.uuid4()),
        "time_in_force": str(time_in_force),
        "post_only": bool(post_only),
        "yes_price": int(yes_price_cents),
    }


class ExecutionEngine:
    """Keep Kalshi resting orders aligned with model bid/ask.

    Safety: defaults to dry-run or off; live mode must be explicitly chosen by caller.
    """

    def __init__(
        self,
        client: _KalshiOrdersClient,
        *,
        mode: str = "dry-run",
        max_contracts: int = 10,
        order_tag_prefix: str = "as:",
    ) -> None:
        mode = (mode or "").strip().lower()
        if mode not in {"off", "dry-run", "live"}:
            raise ValueError("mode must be one of: off, dry-run, live")
        self._client = client
        self._mode = mode
        self._max_contracts = int(max(1, max_contracts))
        self._tag = str(order_tag_prefix or "as:").strip() or "as:"

    @property
    def mode(self) -> str:
        return self._mode

    def sync_quotes(self, intent: QuoteIntent) -> List[str]:
        """Return a list of human-readable actions taken (or planned in dry-run)."""
        return self.sync_quotes_events(intent)[1]

    def sync_quotes_events(self, intent: QuoteIntent) -> tuple[list[ExecutionEvent], list[str]]:
        """Return (structured events, human-readable messages)."""
        if self._mode == "off":
            return [], []

        ticker = intent.market_ticker.strip()
        if not ticker:
            return [], []

        bid = int(intent.yes_bid_cents)
        ask = int(intent.yes_ask_cents)
        if bid <= 0 or ask >= 100 or bid >= ask:
            ts = datetime.now(timezone.utc).isoformat()
            ev = ExecutionEvent(ts_utc=ts, market_ticker=ticker, event="skip", mid_cents=intent.mid_cents)
            return [ev], [f"{ticker}: skip (invalid prices bid={bid} ask={ask})"]

        contracts = int(max(1, min(intent.contracts, self._max_contracts)))

        page = self._client.get_orders(status="resting", ticker=ticker, limit=200)
        orders = [o for o in (page.get("orders") or []) if isinstance(o, dict)]
        # Only manage our own quotes (tagged via client_order_id prefix).
        yes_limits = [o for o in orders if _is_yes_limit(o) and _extract_client_order_id(o).startswith(self._tag)]

        events: list[ExecutionEvent] = []
        msgs: List[str] = []
        ev1, m1 = self._sync_one_side_events(
            ticker,
            yes_limits,
            action="buy",
            target_price=bid,
            contracts=contracts,
            post_only=intent.post_only,
            time_in_force=intent.time_in_force,
            mid_cents=intent.mid_cents,
        )
        ev2, m2 = self._sync_one_side_events(
            ticker,
            yes_limits,
            action="sell",
            target_price=ask,
            contracts=contracts,
            post_only=intent.post_only,
            time_in_force=intent.time_in_force,
            mid_cents=intent.mid_cents,
        )
        events.extend(ev1)
        events.extend(ev2)
        msgs.extend(m1)
        msgs.extend(m2)
        return events, msgs

    def _sync_one_side_events(
        self,
        ticker: str,
        yes_limits: List[Dict[str, Any]],
        *,
        action: str,
        target_price: int,
        contracts: int,
        post_only: bool,
        time_in_force: str,
        mid_cents: Optional[int],
    ) -> tuple[list[ExecutionEvent], list[str]]:
        existing = [o for o in yes_limits if _is_action(o, action)]
        # If any existing order already matches, do nothing (leave other stale ones to cancellation below).
        for o in existing:
            p = _order_price_cents(o)
            rc = _remaining_count(o)
            if p == target_price and (rc is None or rc == contracts):
                ts = datetime.now(timezone.utc).isoformat()
                delta = None if mid_cents is None else (target_price - mid_cents) if action == "sell" else (mid_cents - target_price)
                ev = ExecutionEvent(
                    ts_utc=ts,
                    market_ticker=ticker,
                    event="keep",
                    action=action,
                    price_cents=target_price,
                    contracts=contracts,
                    order_id=_extract_order_id(o) or None,
                    mid_cents=mid_cents,
                    delta_cents_from_mid=delta,
                )
                return [ev], [f"{ticker}: keep {action} yes {contracts} @ {target_price}c"]

        # Otherwise cancel all existing orders on that side.
        events: list[ExecutionEvent] = []
        out: List[str] = []
        for o in existing:
            oid = _extract_order_id(o)
            if not oid:
                continue
            if self._mode == "dry-run":
                out.append(f"{ticker}: would cancel {action} order {oid}")
                ts = datetime.now(timezone.utc).isoformat()
                events.append(ExecutionEvent(ts_utc=ts, market_ticker=ticker, event="cancel", action=action, order_id=oid, mid_cents=mid_cents))
            else:
                self._client.cancel_order(oid)
                out.append(f"{ticker}: canceled {action} order {oid}")
                ts = datetime.now(timezone.utc).isoformat()
                events.append(ExecutionEvent(ts_utc=ts, market_ticker=ticker, event="cancel", action=action, order_id=oid, mid_cents=mid_cents))

        # Place new order.
        # Tag client_order_id so monitors/cancel logic can safely identify "our" orders.
        payload = _build_limit_payload(
            ticker=ticker,
            action=action,
            yes_price_cents=target_price,
            count=contracts,
            post_only=post_only,
            time_in_force=time_in_force,
        )
        payload["client_order_id"] = f"{self._tag}{ticker}:{action}:{uuid.uuid4()}"
        if self._mode == "dry-run":
            out.append(f"{ticker}: would place {action} yes {contracts} @ {target_price}c")
            ts = datetime.now(timezone.utc).isoformat()
            delta = None if mid_cents is None else (target_price - mid_cents) if action == "sell" else (mid_cents - target_price)
            events.append(
                ExecutionEvent(
                    ts_utc=ts,
                    market_ticker=ticker,
                    event="place",
                    action=action,
                    price_cents=target_price,
                    contracts=contracts,
                    order_id=None,
                    mid_cents=mid_cents,
                    delta_cents_from_mid=delta,
                )
            )
            return events, out

        resp = self._client.create_order(payload)
        oid = _extract_order_id(resp) or "n/a"
        out.append(f"{ticker}: placed {action} yes {contracts} @ {target_price}c (order_id={oid})")
        ts = datetime.now(timezone.utc).isoformat()
        delta = None if mid_cents is None else (target_price - mid_cents) if action == "sell" else (mid_cents - target_price)
        events.append(
            ExecutionEvent(
                ts_utc=ts,
                market_ticker=ticker,
                event="place",
                action=action,
                price_cents=target_price,
                contracts=contracts,
                order_id=oid,
                mid_cents=mid_cents,
                delta_cents_from_mid=delta,
            )
        )
        return events, out

    def cancel_all_quotes_events(self, *, ticker: str, mid_cents: Optional[int] = None) -> tuple[list[ExecutionEvent], list[str]]:
        """Cancel all resting YES limit orders for a ticker (both buy and sell)."""
        t = str(ticker or "").strip()
        if not t or self._mode == "off":
            return [], []

        page = self._client.get_orders(status="resting", ticker=t, limit=200)
        orders = [o for o in (page.get("orders") or []) if isinstance(o, dict)]
        # Only cancel our tagged quotes.
        yes_limits = [o for o in orders if _is_yes_limit(o) and _extract_client_order_id(o).startswith(self._tag)]

        events: list[ExecutionEvent] = []
        msgs: list[str] = []
        for o in yes_limits:
            oid = _extract_order_id(o)
            if not oid:
                continue
            action = str(o.get("action") or "").lower() or None
            px = _order_price_cents(o)
            rc = _remaining_count(o)
            ts = datetime.now(timezone.utc).isoformat()
            if self._mode == "dry-run":
                msgs.append(f"{t}: would cancel {action or 'order'} {oid}")
            else:
                self._client.cancel_order(oid)
                msgs.append(f"{t}: canceled {action or 'order'} {oid}")
            events.append(
                ExecutionEvent(
                    ts_utc=ts,
                    market_ticker=t,
                    event="cancel",
                    action=action,
                    price_cents=px,
                    contracts=rc,
                    order_id=oid,
                    mid_cents=mid_cents,
                )
            )
        if not msgs:
            msgs.append(f"{t}: no resting quotes to cancel")
        return events, msgs

    def reconcile_cancel_all_tagged(self) -> List[str]:
        """Cancel all tagged resting orders across the portfolio (safety on startup)."""
        if self._mode == "off":
            return []
        page = self._client.get_orders(status="resting", limit=200)
        orders = [o for o in (page.get("orders") or []) if isinstance(o, dict)]
        tagged = [o for o in orders if _extract_client_order_id(o).startswith(self._tag)]
        msgs: List[str] = []
        for o in tagged:
            oid = _extract_order_id(o)
            if not oid:
                continue
            if self._mode == "dry-run":
                msgs.append(f"would cancel tagged order {oid}")
            else:
                self._client.cancel_order(oid)
                msgs.append(f"canceled tagged order {oid}")
        return msgs

    def count_tagged_resting(self, *, ticker: Optional[str] = None) -> int:
        """Count currently resting orders that belong to this engine (tag prefix)."""
        if self._mode == "off":
            return 0
        page = self._client.get_orders(status="resting", ticker=ticker, limit=200)
        orders = [o for o in (page.get("orders") or []) if isinstance(o, dict)]
        return sum(1 for o in orders if _extract_client_order_id(o).startswith(self._tag))

