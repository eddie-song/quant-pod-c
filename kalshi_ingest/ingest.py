from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .client import KalshiClient
from .save import atomic_write_text, ensure_dir, try_write_csv, write_jsonl


@dataclass
class IngestResult:
    raw_jsonl_path: str
    flat_csv_path: Optional[str]
    note: Optional[str] = None


def ingest_markets(
    client: KalshiClient,
    out_dir: str | Path,
    *,
    status: Optional[str] = None,
    series_ticker: Optional[str] = None,
    event_ticker: Optional[str] = None,
    tickers: Optional[str] = None,
    limit: int = 1000,
) -> IngestResult:
    out_dir = ensure_dir(out_dir)
    stamp = client.now_utc_iso().replace(":", "").replace("+", "Z").replace(".", "")

    params: Dict[str, Any] = {}
    if status:
        params["status"] = status
    if series_ticker:
        params["series_ticker"] = series_ticker
    if event_ticker:
        params["event_ticker"] = event_ticker
    if tickers:
        params["tickers"] = tickers

    raw_rows: List[Dict[str, Any]] = []
    flat: List[Dict[str, Any]] = []

    for page, cursor_used in client.paginate("/markets", params=params, limit=limit):
        raw_rows.append(
            {
                "fetched_at": client.now_utc_iso(),
                "endpoint": "/markets",
                "cursor_used": cursor_used,
                "params": params,
                "page": page,
            }
        )
        markets = page.get("markets", []) or []
        for m in markets:
            if isinstance(m, dict):
                flat.append({"fetched_at": raw_rows[-1]["fetched_at"], **m})

    raw_path = out_dir / f"markets_raw_{stamp}.jsonl"
    write_jsonl(raw_path, raw_rows)

    csv_path = out_dir / f"markets_flat_{stamp}.csv"
    err = try_write_csv(csv_path, flat)
    if err:
        atomic_write_text(out_dir / f"markets_flat_{stamp}.txt", f"Could not write CSV: {err}\n")
        return IngestResult(raw_jsonl_path=str(raw_path), flat_csv_path=None, note=err)

    return IngestResult(raw_jsonl_path=str(raw_path), flat_csv_path=str(csv_path))


def ingest_trades(
    client: KalshiClient,
    out_dir: str | Path,
    *,
    ticker: Optional[str] = None,
    min_ts: Optional[int] = None,
    max_ts: Optional[int] = None,
    limit: int = 1000,
) -> IngestResult:
    out_dir = ensure_dir(out_dir)
    stamp = client.now_utc_iso().replace(":", "").replace("+", "Z").replace(".", "")

    params: Dict[str, Any] = {}
    if ticker:
        params["ticker"] = ticker
    if min_ts is not None:
        params["min_ts"] = int(min_ts)
    if max_ts is not None:
        params["max_ts"] = int(max_ts)

    raw_rows: List[Dict[str, Any]] = []
    flat: List[Dict[str, Any]] = []

    for page, cursor_used in client.paginate("/markets/trades", params=params, limit=limit):
        raw_rows.append(
            {
                "fetched_at": client.now_utc_iso(),
                "endpoint": "/markets/trades",
                "cursor_used": cursor_used,
                "params": params,
                "page": page,
            }
        )
        trades = page.get("trades", []) or []
        for t in trades:
            if isinstance(t, dict):
                flat.append({"fetched_at": raw_rows[-1]["fetched_at"], **t})

    raw_path = out_dir / f"trades_raw_{stamp}.jsonl"
    write_jsonl(raw_path, raw_rows)

    csv_path = out_dir / f"trades_flat_{stamp}.csv"
    err = try_write_csv(csv_path, flat)
    if err:
        atomic_write_text(out_dir / f"trades_flat_{stamp}.txt", f"Could not write CSV: {err}\n")
        return IngestResult(raw_jsonl_path=str(raw_path), flat_csv_path=None, note=err)

    return IngestResult(raw_jsonl_path=str(raw_path), flat_csv_path=str(csv_path))


def fetch_trades_sample(client: KalshiClient, limit: int = 10) -> Dict[str, Any]:
    page = client.get("/markets/trades", params={"limit": limit})
    trades = page.get("trades", []) or []
    tickers = list({t.get("ticker") for t in trades if isinstance(t, dict) and t.get("ticker")})
    return {"count": len(trades), "cursor": page.get("cursor", ""), "tickers_sample": tickers[:20], "raw_page": page}

