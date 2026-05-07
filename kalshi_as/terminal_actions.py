from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def build_terminal_record(
    *,
    market_ticker: str,
    tau_hours: float,
    qty_yes: float,
    action: str,
    note: str = "",
) -> Dict[str, Any]:
    return {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "type": "terminal_action",
        "market_ticker": market_ticker,
        "tau_hours": float(tau_hours),
        "qty_yes": float(qty_yes),
        "action": str(action),
        "note": str(note),
    }


def append_records_jsonl(path: str, records: List[Dict[str, Any]]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

