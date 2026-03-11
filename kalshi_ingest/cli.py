from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from .auth import KalshiAuth
from .client import KalshiClient
from .ingest import fetch_trades_sample, ingest_markets, ingest_trades


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--env-file", default=None, help="Optional path to .env file (defaults to loading .env if present).")
    p.add_argument("--out-dir", default="data/kalshi", help="Output directory for saved files.")


def cmd_markets(args: argparse.Namespace) -> int:
    res = ingest_markets(
        _client_from_env(args.env_file),
        args.out_dir,
        status=args.status,
        series_ticker=args.series_ticker,
        event_ticker=args.event_ticker,
        tickers=args.tickers,
        limit=args.limit,
    )
    print(res)
    return 0


def cmd_trades(args: argparse.Namespace) -> int:
    res = ingest_trades(
        _client_from_env(args.env_file),
        args.out_dir,
        ticker=args.ticker,
        min_ts=args.min_ts,
        max_ts=args.max_ts,
        limit=args.limit,
    )
    print(res)
    return 0


def cmd_trades_sample(args: argparse.Namespace) -> int:
    client = _client_from_env(args.env_file)
    sample = fetch_trades_sample(client, limit=args.limit)
    print(f"Trades returned: {sample['count']}")
    print(f"Cursor: {sample['cursor']!r}")
    if sample["tickers_sample"]:
        print("Sample tickers (exact format from API):")
        for t in sample["tickers_sample"][:15]:
            print(f"  {t!r}")
    else:
        print("No trades in this page (try --limit 100 or run without ticker filter).")
    return 0


def _client_from_env(env_file: Optional[str]) -> KalshiClient:
    if env_file:
        load_dotenv(env_file, override=True)
    else:
        load_dotenv(override=False)

    pk = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
    if pk and not os.path.isabs(pk):
        base = Path(env_file).resolve().parent if env_file else Path.cwd()
        os.environ["KALSHI_PRIVATE_KEY_PATH"] = str((base / pk).resolve())

    return KalshiClient(KalshiAuth.from_env())


def main() -> int:
    parser = argparse.ArgumentParser(prog="kalshi_ingest", description="Simple Kalshi data ingestion (REST).")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_markets = sub.add_parser("markets", help="Download markets (GET /markets).")
    _add_common(p_markets)
    p_markets.add_argument("--status", default=None, help="Market status: unopened/open/closed/settled")
    p_markets.add_argument("--series-ticker", default=None, help="Filter by series_ticker")
    p_markets.add_argument("--event-ticker", default=None, help="Filter by event_ticker")
    p_markets.add_argument("--tickers", default=None, help="Filter by comma-separated tickers")
    p_markets.add_argument("--limit", type=int, default=1000, help="Page size (1-1000).")
    p_markets.set_defaults(func=cmd_markets)

    p_trades = sub.add_parser("trades", help="Download trades (GET /markets/trades).")
    _add_common(p_trades)
    p_trades.add_argument("--ticker", default=None, help="Filter by market ticker")
    p_trades.add_argument("--min-ts", type=int, default=None, help="Min unix timestamp (seconds)")
    p_trades.add_argument("--max-ts", type=int, default=None, help="Max unix timestamp (seconds)")
    p_trades.add_argument("--limit", type=int, default=1000, help="Page size (1-1000).")
    p_trades.set_defaults(func=cmd_trades)

    p_sample = sub.add_parser("trades-sample", help="Fetch one page of trades (no ticker filter); print count and sample tickers.")
    _add_common(p_sample)
    p_sample.add_argument("--limit", type=int, default=100, help="Page size (1-1000).")
    p_sample.set_defaults(func=cmd_trades_sample)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

