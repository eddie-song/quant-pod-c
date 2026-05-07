from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from kalshi_ws.stream import consume_fills

from .ledger import PortfolioLedger


def _append_jsonl(path: str, rows: List[Dict[str, Any]]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


async def run_ws_fill_consumer(
    *,
    ledger: PortfolioLedger,
    poll_s: float = 0.5,
    out_path: str = "data/kalshi/as_ws_fills.jsonl",
) -> None:
    """Consume WS fill events and update ledger (no REST polling)."""
    poll_s = max(0.01, float(poll_s))
    while True:
        await asyncio.sleep(poll_s)
        fills = consume_fills()
        if not fills:
            continue

        rows: List[Dict[str, Any]] = []
        for f in fills:
            ticker = str(f.market_ticker or "").strip()
            action = str(f.action or "").strip().lower()
            side = str(f.side or "yes").strip().lower()
            if side != "yes":
                # This repo currently quotes YES; ignore NO fills for now.
                continue
            if action not in {"buy", "sell"}:
                continue
            count = int(max(0.0, float(f.count)))
            # Prefer yes_price if present (dollars).
            px_cents = int(round(float(f.yes_price) * 100))
            if count <= 0 or px_cents <= 0:
                continue

            ledger.apply_fill(ticker=ticker, action=action, price_cents=px_cents, count=count)
            rows.append(
                {
                    "ts_utc": datetime.now(timezone.utc).isoformat(),
                    "type": "ws_fill",
                    "market_ticker": ticker,
                    "side": side,
                    "action": action,
                    "count": count,
                    "price_cents": px_cents,
                    "order_id": f.order_id,
                    "client_order_id": f.client_order_id,
                }
            )
        if rows:
            await asyncio.to_thread(_append_jsonl, out_path, rows)

