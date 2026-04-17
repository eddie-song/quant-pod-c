from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


def build_calibration_record(
    *,
    market_ticker: str,
    mid: float,
    model_bid: float,
    model_ask: float,
    A: float,
    k: float,
    gamma: float,
) -> Dict[str, Any]:
    bid_distance_from_mid = float(mid - model_bid)
    ask_distance_from_mid = float(model_ask - mid)
    return {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "market_ticker": market_ticker,
        "mid": float(mid),
        "model_bid": float(model_bid),
        "model_ask": float(model_ask),
        "bid_distance_from_mid": bid_distance_from_mid,
        "ask_distance_from_mid": ask_distance_from_mid,
        "A": float(A),
        "k": float(k),
        "gamma": float(gamma),
    }


def append_records_jsonl(path: str, records: List[Dict[str, Any]]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
