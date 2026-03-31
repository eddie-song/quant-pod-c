from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional

from kalshi_ingest.auth import KalshiAuth
from kalshi_ingest.client import KalshiClient
from kalshi_ingest.ingest import ingest_markets

from .config import Config

logger = logging.getLogger(__name__)


@dataclass
class MarketMetadata:
    ticker: str
    expiration_time: float      # unix timestamp
    status: str                 # "open", "closed", "settled", etc.
    event_ticker: str
    result: str                 # empty string if unresolved
    close_time: float           # unix timestamp

    @classmethod
    def from_api_dict(cls, d: dict) -> "MarketMetadata":
        return cls(
            ticker=d.get("ticker", ""),
            expiration_time=_parse_iso_ts(d.get("expiration_time", "")),
            status=d.get("status", ""),
            event_ticker=d.get("event_ticker", ""),
            result=d.get("result", ""),
            close_time=_parse_iso_ts(d.get("close_time", "")),
        )


def _parse_iso_ts(s: str) -> float:
    """Parse an ISO 8601 timestamp string to unix seconds."""
    if not s:
        return 0.0
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.timestamp()
    except (ValueError, TypeError):
        return 0.0


# ── Module-level state ────────────────────────────────────────────────

_market_metadata: Dict[str, MarketMetadata] = {}


def get_market_metadata() -> Dict[str, MarketMetadata]:
    """Return the full metadata dict (read-only by convention)."""
    return _market_metadata


def get_metadata(ticker: str) -> Optional[MarketMetadata]:
    """Return metadata for a single ticker, or None if unknown."""
    return _market_metadata.get(ticker)


# ── Sync fetch (runs in a thread) ────────────────────────────────────

def _fetch_metadata_sync(config: Config) -> Dict[str, MarketMetadata]:
    """Create a REST client, call ingest_markets, parse results from JSONL."""
    auth = KalshiAuth.from_env()
    client = KalshiClient(auth)
    out_dir = config.paths.metadata_out_dir

    result = ingest_markets(client, out_dir, status="open")
    logger.info("Fetched markets: raw=%s csv=%s", result.raw_jsonl_path, result.flat_csv_path)

    metadata: Dict[str, MarketMetadata] = {}
    try:
        with open(result.raw_jsonl_path, encoding="utf-8") as f:
            for line in f:
                row = json.loads(line)
                markets = row.get("page", {}).get("markets", []) or []
                for m in markets:
                    if isinstance(m, dict) and m.get("ticker"):
                        md = MarketMetadata.from_api_dict(m)
                        metadata[md.ticker] = md
    except Exception:
        logger.exception("Failed to parse metadata from %s", result.raw_jsonl_path)

    return metadata


# ── Async wrapper ─────────────────────────────────────────────────────

async def refresh_metadata(config: Config) -> int:
    """Fetch metadata in a background thread and replace the global dict.

    Returns the number of markets loaded.
    """
    global _market_metadata
    try:
        new_meta = await asyncio.to_thread(_fetch_metadata_sync, config)
        _market_metadata = new_meta
        logger.info("Metadata refreshed: %d active markets", len(new_meta))
        return len(new_meta)
    except Exception:
        logger.exception("Metadata refresh failed, keeping previous %d markets", len(_market_metadata))
        return len(_market_metadata)
