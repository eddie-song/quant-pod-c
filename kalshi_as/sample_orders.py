from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from .model import AvellanedaStoikovQuotes


def build_sample_order_record(
    *,
    market_ticker: str,
    model_quotes: AvellanedaStoikovQuotes,
    sigma: float,
    book_bid: float,
    book_ask: float,
    sample_count_per_side: float,
    gamma: float,
    k: float,
    tau_hours: float,
    inventory_yes: float,
) -> Dict[str, Any]:
    """One JSON object describing hypothetical maker quotes (not sent to the API)."""
    cycle_ts = datetime.now(timezone.utc).isoformat()
    n = float(sample_count_per_side)
    return {
        "cycle_ts_utc": cycle_ts,
        "market_ticker": market_ticker,
        "model": "avellaneda_stoikov_reduced_symmetric",
        "note": "Hypothetical limit intents only; no REST order placement.",
        "observed_book_yes_bid": book_bid,
        "observed_book_yes_ask": book_ask,
        "computed_mid": model_quotes.mid,
        "reservation_yes": model_quotes.reservation,
        "half_spread_model": model_quotes.half_spread,
        "sigma_per_sqrt_hour_est": sigma,
        "params": {"gamma": gamma, "k": k, "tau_hours": tau_hours, "inventory_yes": inventory_yes},
        "hypothetical_orders_yes_side": [
            {
                "action": "post_limit_buy_yes",
                "limit_price_dollars": model_quotes.bid,
                "count": n,
            },
            {
                "action": "post_limit_sell_yes",
                "limit_price_dollars": model_quotes.ask,
                "count": n,
            },
        ],
    }


def append_records_jsonl(path: str, records: List[Dict[str, Any]]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
