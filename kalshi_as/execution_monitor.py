from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Protocol, Tuple

from .execution import ExecutionEvent
from .ledger import PortfolioLedger


class _KalshiOrdersClient(Protocol):
    def get_orders(self, *, status: Optional[str] = None, ticker: Optional[str] = None, limit: int = 200, cursor: Optional[str] = None) -> Dict[str, Any]:  # noqa: E501
        ...


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_jsonl(path: str, rows: List[Dict[str, Any]]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _extract_order_id(order: Dict[str, Any]) -> str:
    for key in ("order_id", "id"):
        v = order.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    inner = order.get("order")
    if isinstance(inner, dict):
        return _extract_order_id(inner)
    return ""


@dataclass
class TrackedOrder:
    order_id: str
    market_ticker: str
    action: str  # buy|sell
    price_cents: int
    contracts: int
    placed_ts: float
    delta_cents_from_mid: Optional[int]
    last_remaining: Optional[int] = None


class ArrivalFitter:
    """Very simple fitter: bucket by delta and estimate lambda = fills / exposure_time."""

    def __init__(self) -> None:
        self._exposure_s_by_delta: Dict[int, float] = {}
        self._fills_by_delta: Dict[int, int] = {}

    def add_exposure(self, delta_cents: int, seconds: float) -> None:
        self._exposure_s_by_delta[delta_cents] = self._exposure_s_by_delta.get(delta_cents, 0.0) + max(0.0, seconds)

    def add_fill(self, delta_cents: int) -> None:
        self._fills_by_delta[delta_cents] = self._fills_by_delta.get(delta_cents, 0) + 1

    def fit_A_k(self, *, min_points: int = 2, min_exposure_s: float = 10.0) -> Optional[Dict[str, float]]:
        xs: List[float] = []
        ys: List[float] = []
        for d, expo in self._exposure_s_by_delta.items():
            if expo < min_exposure_s:
                continue
            fills = self._fills_by_delta.get(d, 0)
            if fills <= 0:
                continue
            lam = fills / expo
            xs.append(float(d))
            ys.append(math.log(lam))
        if len(xs) < min_points:
            return None
        xbar = sum(xs) / len(xs)
        ybar = sum(ys) / len(ys)
        num = sum((x - xbar) * (y - ybar) for x, y in zip(xs, ys))
        den = sum((x - xbar) ** 2 for x in xs)
        if den <= 0:
            return None
        slope = num / den  # log(lambda) ~ intercept + slope * delta
        intercept = ybar - slope * xbar
        k = float(-slope)
        A = float(math.exp(intercept))
        return {"A_est": A, "k_est": k, "points": float(len(xs))}


class ExecutionMonitor:
    def __init__(
        self,
        client: _KalshiOrdersClient,
        *,
        event_log_path: str = "data/kalshi/as_execution_events.jsonl",
        poll_s: float = 2.0,
        ledger: Optional[PortfolioLedger] = None,
    ) -> None:
        self._client = client
        self._event_log_path = event_log_path
        self._poll_s = max(0.5, float(poll_s))
        self._tracked: Dict[str, TrackedOrder] = {}
        self._fitter = ArrivalFitter()
        self._ledger = ledger

    def ingest_events(self, events: Iterable[ExecutionEvent]) -> None:
        """Add newly placed orders to tracking (and log raw events)."""
        rows: List[Dict[str, Any]] = []
        for e in events:
            rows.append(e.__dict__)
            if e.event == "place" and e.order_id:
                self._tracked[e.order_id] = TrackedOrder(
                    order_id=e.order_id,
                    market_ticker=e.market_ticker,
                    action=str(e.action or ""),
                    price_cents=int(e.price_cents or 0),
                    contracts=int(e.contracts or 0),
                    placed_ts=time.time(),
                    delta_cents_from_mid=e.delta_cents_from_mid,
                )
        if rows:
            _append_jsonl(self._event_log_path, rows)

    def poll_once(self) -> List[Dict[str, Any]]:
        """Poll REST and emit terminal events for tracked orders that are no longer resting."""
        if not self._tracked:
            return []

        # Group by ticker to reduce API calls.
        by_ticker: Dict[str, List[TrackedOrder]] = {}
        for o in self._tracked.values():
            by_ticker.setdefault(o.market_ticker, []).append(o)

        out_rows: List[Dict[str, Any]] = []
        now = time.time()

        for ticker, orders in by_ticker.items():
            resting_map = self._resting_orders_for_ticker(ticker)
            for o in orders:
                resting_obj = resting_map.get(o.order_id)
                if resting_obj is not None:
                    # Still resting; accrue exposure and detect partial fills via remaining_count decreases.
                    if o.delta_cents_from_mid is not None:
                        self._fitter.add_exposure(o.delta_cents_from_mid, self._poll_s)

                    rem = _extract_remaining_count(resting_obj)
                    if rem is not None:
                        if o.last_remaining is None:
                            o.last_remaining = rem
                        elif rem < o.last_remaining:
                            filled = int(o.last_remaining - rem)
                            o.last_remaining = rem
                            row_pf = {
                                "ts_utc": _now_iso(),
                                "market_ticker": ticker,
                                "order_id": o.order_id,
                                "action": o.action,
                                "fill_price_cents": _extract_fill_price_cents(resting_obj) or o.price_cents,
                                "fill_count": filled,
                                "type": "partial_fill",
                                "delta_cents_from_mid": o.delta_cents_from_mid,
                            }
                            out_rows.append(row_pf)
                            if o.delta_cents_from_mid is not None:
                                self._fitter.add_fill(o.delta_cents_from_mid)
                            if self._ledger is not None:
                                try:
                                    self._ledger.apply_fill(
                                        ticker=ticker,
                                        action=o.action,
                                        price_cents=int(row_pf["fill_price_cents"]),
                                        count=int(row_pf["fill_count"]),
                                    )
                                except Exception:
                                    pass
                    continue

                # No longer resting. Try to classify as filled vs canceled, best-effort.
                status, fill_obj = self._classify_terminal_status(ticker, o.order_id)
                duration_s = max(0.0, now - o.placed_ts)
                row = {
                    "ts_utc": _now_iso(),
                    "market_ticker": ticker,
                    "order_id": o.order_id,
                    "action": o.action,
                    "price_cents": o.price_cents,
                    "contracts": o.contracts,
                    "terminal_status": status,
                    "resting_duration_s": duration_s,
                    "delta_cents_from_mid": o.delta_cents_from_mid,
                }
                if status == "filled" and isinstance(fill_obj, dict):
                    row["fill_price_cents"] = _extract_fill_price_cents(fill_obj) or o.price_cents
                    row["fill_count"] = _extract_fill_count(fill_obj) or o.contracts
                out_rows.append(row)

                if status == "filled" and o.delta_cents_from_mid is not None:
                    self._fitter.add_fill(o.delta_cents_from_mid)
                if status == "filled" and self._ledger is not None:
                    px = int(row.get("fill_price_cents") or o.price_cents)
                    cnt = int(row.get("fill_count") or o.contracts)
                    try:
                        self._ledger.apply_fill(ticker=ticker, action=o.action, price_cents=px, count=cnt)
                    except Exception:
                        pass

                # Remove from tracking.
                self._tracked.pop(o.order_id, None)

        if out_rows:
            _append_jsonl(self._event_log_path, out_rows)

        # Periodically emit a small fit summary.
        fit = self._fitter.fit_A_k()
        if fit is not None:
            summary = {"ts_utc": _now_iso(), "type": "arrival_fit", **fit}
            _append_jsonl(self._event_log_path, [summary])
            out_rows.append(summary)

        return out_rows

    def _resting_orders_for_ticker(self, ticker: str) -> Dict[str, Dict[str, Any]]:
        try:
            page = self._client.get_orders(status="resting", ticker=ticker, limit=200)
        except Exception:
            return {}
        orders = [o for o in (page.get("orders") or []) if isinstance(o, dict)]
        out: Dict[str, Dict[str, Any]] = {}
        for o in orders:
            oid = _extract_order_id(o)
            if oid:
                out[oid] = o
        return out

    def _classify_terminal_status(self, ticker: str, order_id: str) -> Tuple[str, Optional[Dict[str, Any]]]:
        # Best-effort: try common status buckets. If the API doesn't support a status, ignore.
        for s in ("filled", "canceled", "cancelled", "executed", "completed"):
            try:
                page = self._client.get_orders(status=s, ticker=ticker, limit=200)
            except Exception:
                continue
            orders = [o for o in (page.get("orders") or []) if isinstance(o, dict)]
            for o in orders:
                if _extract_order_id(o) == order_id:
                    status = "filled" if s == "filled" else "canceled" if s in {"canceled", "cancelled"} else s
                    return status, o
        return "not_resting", None


def _extract_fill_price_cents(order: Dict[str, Any]) -> Optional[int]:
    for k in ("yes_price", "price", "limit_price", "price_cents"):
        v = order.get(k)
        if isinstance(v, int):
            return v
        if isinstance(v, str) and v.isdigit():
            return int(v)
    return None


def _extract_fill_count(order: Dict[str, Any]) -> Optional[int]:
    for k in ("filled_count", "filled", "executed_count", "count"):
        v = order.get(k)
        if isinstance(v, int):
            return v
        if isinstance(v, str) and v.isdigit():
            return int(v)
    return None

def _extract_remaining_count(order: Dict[str, Any]) -> Optional[int]:
    for k in ("remaining_count", "remaining"):
        v = order.get(k)
        if isinstance(v, int):
            return v
        if isinstance(v, str) and v.isdigit():
            return int(v)
    return None


    async def run_forever(self) -> None:
        import asyncio

        while True:
            await asyncio.sleep(self._poll_s)
            try:
                await asyncio.to_thread(self.poll_once)
            except Exception:
                pass

