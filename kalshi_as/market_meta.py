from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, Optional

from kalshi_ingest.client import KalshiClient


def _parse_ts_utc(value: object) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _extract_close_dt_utc(market: Dict[str, object]) -> Optional[datetime]:
    # API fields vary by endpoint/version; use first valid one.
    for key in ("close_time", "expiration_time", "expiration_ts", "close_ts"):
        dt = _parse_ts_utc(market.get(key))
        if dt is not None:
            return dt
    return None


@dataclass
class MarketMetaCache:
    client: KalshiClient
    refresh_s: float = 300.0
    default_tau_hours: float = 4.0

    def __post_init__(self) -> None:
        self._close_dt_by_ticker: Dict[str, datetime] = {}
        self._last_refresh_ts: float = 0.0

    def maybe_refresh(self, *, now_ts: float) -> bool:
        if self._last_refresh_ts > 0 and (now_ts - self._last_refresh_ts) < max(self.refresh_s, 1.0):
            return False
        self.refresh()
        self._last_refresh_ts = float(now_ts)
        return True

    def refresh(self) -> None:
        close_dt_by_ticker: Dict[str, datetime] = {}
        for market in self._iter_open_markets():
            ticker = str(market.get("ticker") or "").strip()
            if not ticker:
                continue
            close_dt = _extract_close_dt_utc(market)
            if close_dt is not None:
                close_dt_by_ticker[ticker] = close_dt
        self._close_dt_by_ticker = close_dt_by_ticker

    def _iter_open_markets(self) -> Iterable[Dict[str, object]]:
        for page, _ in self.client.paginate("/markets", params={"status": "open"}, limit=1000):
            markets = page.get("markets", []) or []
            for market in markets:
                if isinstance(market, dict):
                    yield market

    def tau_hours_for_ticker(self, ticker: str, *, now_dt: Optional[datetime] = None) -> float:
        dt_now = now_dt or datetime.now(timezone.utc)
        if dt_now.tzinfo is None:
            dt_now = dt_now.replace(tzinfo=timezone.utc)
        else:
            dt_now = dt_now.astimezone(timezone.utc)

        close_dt = self._close_dt_by_ticker.get(ticker)
        if close_dt is None:
            return float(max(self.default_tau_hours, 1e-6))

        tau = (close_dt - dt_now).total_seconds() / 3600.0
        return float(max(tau, 1e-6))
