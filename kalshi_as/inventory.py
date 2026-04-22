from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Optional

from kalshi_ingest.auth import KalshiAuth
from kalshi_ingest.client import KalshiClient

logger = logging.getLogger(__name__)


def _parse_fp(value: object) -> float:
    if isinstance(value, str):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


@dataclass
class InventoryState:
    _positions: Dict[str, float] = field(default_factory=dict)
    _applied_fill_ids: set[str] = field(default_factory=set)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def get_position(self, market_ticker: str, default: float = 0.0) -> float:
        with self._lock:
            return float(self._positions.get(market_ticker, default))

    def snapshot(self) -> Dict[str, float]:
        with self._lock:
            return dict(self._positions)

    def set_position(self, market_ticker: str, position_yes: float) -> None:
        with self._lock:
            if abs(position_yes) < 1e-12:
                self._positions.pop(market_ticker, None)
            else:
                self._positions[market_ticker] = float(position_yes)

    def set_from_positions(self, positions: Iterable[dict]) -> None:
        next_positions: Dict[str, float] = {}
        for row in positions:
            ticker = str(row.get("market_ticker") or row.get("ticker") or "").strip()
            if not ticker:
                continue
            position = _parse_fp(row.get("position_fp", 0.0))
            if abs(position) >= 1e-12:
                next_positions[ticker] = position
        with self._lock:
            self._positions = next_positions

    def apply_fill(self, fill: dict) -> bool:
        fill_id = str(fill.get("fill_id") or fill.get("trade_id") or "").strip()
        ticker = str(fill.get("market_ticker") or fill.get("ticker") or "").strip()
        if not fill_id or not ticker:
            return False

        size = _parse_fp(fill.get("count_fp", 0.0))
        side = str(fill.get("side") or fill.get("purchased_side") or "").strip().lower()
        action = str(fill.get("action") or "").strip().lower()
        if size <= 0 or side not in {"yes", "no"} or action not in {"buy", "sell"}:
            return False

        signed_delta = size
        if side == "no":
            signed_delta *= -1.0
        if action == "sell":
            signed_delta *= -1.0

        post_position = fill.get("post_position_fp")
        with self._lock:
            if fill_id in self._applied_fill_ids:
                return False
            self._applied_fill_ids.add(fill_id)

            if post_position is not None:
                new_position = _parse_fp(post_position)
            else:
                new_position = self._positions.get(ticker, 0.0) + signed_delta

            if abs(new_position) < 1e-12:
                self._positions.pop(ticker, None)
            else:
                self._positions[ticker] = float(new_position)
        return True

    def load_seed_file(self, path: str | Path) -> int:
        seed_path = Path(path)
        data = json.loads(seed_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Inventory seed file must contain a JSON object keyed by ticker")

        loaded = 0
        with self._lock:
            for ticker, value in data.items():
                ticker_str = str(ticker).strip()
                if not ticker_str:
                    continue
                position = _parse_fp(value)
                if abs(position) < 1e-12:
                    self._positions.pop(ticker_str, None)
                else:
                    self._positions[ticker_str] = position
                loaded += 1
        return loaded


_inventory_state = InventoryState()


def get_inventory_state() -> InventoryState:
    return _inventory_state


def sync_inventory_from_positions(
    state: Optional[InventoryState] = None,
    *,
    client: Optional[KalshiClient] = None,
    limit: int = 1000,
) -> int:
    inventory = state or _inventory_state
    kalshi_client = client or KalshiClient(KalshiAuth.from_env())
    positions: list[dict] = []

    for page, _ in kalshi_client.paginate(
        "/portfolio/positions",
        params={"count_filter": "position", "settlement_status": "unsettled"},
        limit=limit,
    ):
        rows = page.get("market_positions", []) or []
        if isinstance(rows, list):
            positions.extend(row for row in rows if isinstance(row, dict))

    inventory.set_from_positions(positions)
    logger.info("Loaded %d live market positions into inventory state.", len(inventory.snapshot()))
    return len(positions)
