from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Mapping, Optional


def load_inventory_by_ticker(path: Optional[str]) -> Dict[str, float]:
    """Load per-ticker inventory (YES contracts) from JSON.

    Expected shape:
    {
      "TICKER_A": 10,
      "TICKER_B": -5.5
    }
    Missing tickers should be treated as 0 by callers.
    """
    if not path:
        return {}
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"Inventory JSON file not found: {path}")
    raw = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Inventory JSON must be an object mapping ticker -> numeric inventory")

    out: Dict[str, float] = {}
    for key, value in raw.items():
        ticker = str(key).strip()
        if not ticker:
            continue
        try:
            out[ticker] = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Inventory for ticker {ticker!r} must be numeric") from exc
    return out


def inventory_for_ticker(inventory_by_ticker: Mapping[str, float], ticker: str) -> float:
    return float(inventory_by_ticker.get(ticker, 0.0))
