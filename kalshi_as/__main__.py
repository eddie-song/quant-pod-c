from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

from kalshi_ws.stream import run_ws_stream

from .model import ASConfig
from .strategy_loop import run_as_strategy_loop


def main() -> None:
    load_dotenv(override=False)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    parser = argparse.ArgumentParser(description="Kalshi WebSocket + Avellaneda–Stoikov quote monitor")
    parser.add_argument("--interval", type=float, default=5.0, help="Seconds between AS refresh cycles")
    parser.add_argument("--gamma", type=float, default=0.05, help="Risk aversion γ")
    parser.add_argument("--k", type=float, default=1.5, help="Order-intensity parameter k")
    parser.add_argument("--tau-hours", type=float, default=4.0, help="Horizon (T−t) in hours")
    parser.add_argument("--tick", type=float, default=0.01, help="Price tick for rounding (dollars)")
    parser.add_argument("--inventory", type=float, default=0.0, help="YES inventory (contracts); + = long YES")
    parser.add_argument("--min-spread", type=float, default=0.02, help="Only markets with YES spread ≥ this")
    parser.add_argument("--max-markets", type=int, default=12, help="Max markets to log per cycle")
    parser.add_argument("--mid-history", type=int, default=80, help="Rolling mids per ticker")
    parser.add_argument("--sigma-min-samples", type=int, default=12, help="Min mids before σ estimate")
    parser.add_argument(
        "--sample-contracts",
        type=float,
        default=0.0,
        help="If >0, append hypothetical BUY YES @ model bid / SELL YES @ model ask (JSONL + log line suffix)",
    )
    parser.add_argument(
        "--sample-orders-file",
        type=str,
        default="",
        help="JSONL output path (default: env KALSHI_AS_SAMPLE_ORDERS or data/kalshi/as_sample_orders.jsonl)",
    )
    args = parser.parse_args()

    base_url = os.getenv("KALSHI_WS_URL", "")
    out_dir = os.getenv("KALSHI_WS_OUT_DIR", "data/kalshi/ws")
    trade_buffer = int(os.getenv("KALSHI_WS_TRADE_BUFFER", "5000"))

    cfg = ASConfig(gamma=args.gamma, k=args.k, tau_hours=args.tau_hours, tick=args.tick)

    async def _run() -> None:
        await asyncio.gather(
            run_ws_stream(
                base_url=base_url or None,
                out_dir=out_dir,
                trade_buffer_size=trade_buffer,
            ),
            run_as_strategy_loop(
                interval_s=args.interval,
                config=cfg,
                inventory_yes=args.inventory,
                min_spread=args.min_spread,
                max_markets=args.max_markets,
                mid_history_len=args.mid_history,
                sigma_min_samples=args.sigma_min_samples,
                sample_contracts_per_side=args.sample_contracts,
                sample_orders_path=args.sample_orders_file or None,
            ),
        )

    asyncio.run(_run())


if __name__ == "__main__":
    main()
