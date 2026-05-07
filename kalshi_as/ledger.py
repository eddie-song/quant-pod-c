from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass
class PositionState:
    qty_yes: float = 0.0
    cash_cents: float = 0.0  # cash from fills only (sell adds, buy subtracts)
    last_mid_cents: Optional[int] = None


class PortfolioLedger:
    """Track per-ticker inventory and a simple mark-to-mid PnL.

    This is not a full exchange-grade ledger (fees/settlement not modeled here),
    but it's enough for Step 6: update q and compute a running mark-to-mid value.
    """

    def __init__(self, *, out_path: str = "data/kalshi/as_ledger.jsonl", assumed_fee_cents_per_contract: float = 0.0) -> None:
        self._out_path = out_path
        self._fee_cents = float(max(0.0, assumed_fee_cents_per_contract))
        self._by_ticker: Dict[str, PositionState] = {}

    def set_mid(self, ticker: str, mid_cents: int) -> None:
        t = str(ticker or "").strip()
        if not t:
            return
        st = self._by_ticker.setdefault(t, PositionState())
        st.last_mid_cents = int(mid_cents)

    def apply_fill(self, *, ticker: str, action: str, price_cents: int, count: int, fee_cents: float | None = None) -> None:
        t = str(ticker or "").strip()
        if not t:
            return
        action = str(action or "").strip().lower()
        n = int(max(0, count))
        px = int(price_cents)
        if n <= 0:
            return

        st = self._by_ticker.setdefault(t, PositionState())
        fee = float(self._fee_cents * n) if fee_cents is None else float(max(0.0, fee_cents))
        if action == "buy":
            st.qty_yes += n
            st.cash_cents -= (px * n + fee)
        elif action == "sell":
            st.qty_yes -= n
            st.cash_cents += (px * n - fee)
        else:
            return

        self._append_snapshot(t, reason="fill", fill_action=action, fill_price_cents=px, fill_count=n, fee_cents=float(fee))

    def qty_for_ticker(self, ticker: str) -> float:
        st = self._by_ticker.get(str(ticker or "").strip())
        return float(st.qty_yes) if st is not None else 0.0

    def snapshot_row(self, ticker: str) -> Dict[str, Any]:
        t = str(ticker or "").strip()
        st = self._by_ticker.get(t) or PositionState()
        mid = st.last_mid_cents
        mtm = None if mid is None else float(st.cash_cents + st.qty_yes * mid)
        return {
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "market_ticker": t,
            "qty_yes": float(st.qty_yes),
            "cash_cents": float(st.cash_cents),
            "last_mid_cents": mid,
            "mark_to_mid_pnl_cents": mtm,
        }

    def _append_snapshot(
        self,
        ticker: str,
        *,
        reason: str,
        fill_action: Optional[str] = None,
        fill_price_cents: Optional[int] = None,
        fill_count: Optional[int] = None,
        fee_cents: Optional[float] = None,
    ) -> None:
        row = self.snapshot_row(ticker)
        row.update(
            {
                "type": "ledger_snapshot",
                "reason": reason,
                "fill_action": fill_action,
                "fill_price_cents": fill_price_cents,
                "fill_count": fill_count,
                "assumed_fee_cents": fee_cents,
            }
        )
        p = Path(self._out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

