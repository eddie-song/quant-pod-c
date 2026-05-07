from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

from kalshi_ingest.auth import KalshiAuth
from kalshi_ingest.client import KalshiClient
from kalshi_ws.stream import run_ws_stream

from .execution import ExecutionEngine
from .execution_monitor import ExecutionMonitor
from .ledger import PortfolioLedger
from .inventory import load_inventory_by_ticker
from .market_meta import MarketMetaCache
from .model import ASConfig
from .strategy_loop import run_as_strategy_loop
from .ws_fill_consumer import run_ws_fill_consumer


def main() -> None:
    load_dotenv(override=False)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    parser = argparse.ArgumentParser(description="Kalshi WebSocket + Avellaneda-Stoikov quote monitor")
    parser.add_argument("--interval", type=float, default=5.0, help="Seconds between AS refresh cycles")
    parser.add_argument("--gamma", type=float, default=0.05, help="Risk aversion gamma")
    parser.add_argument(
        "--A",
        dest="A",
        type=float,
        default=float(os.getenv("KALSHI_AS_A", "1.0")),
        help="Calibration parameter A (stored and logged; spread formula unchanged)",
    )
    parser.add_argument("--k", type=float, default=1.5, help="Order-intensity parameter k")
    parser.add_argument("--tau-hours", type=float, default=4.0, help="Horizon (T-t) in hours")
    parser.add_argument("--tick", type=float, default=0.01, help="Price tick for rounding (dollars)")
    parser.add_argument("--inventory", type=float, default=0.0, help="YES inventory (contracts); + = long YES")
    parser.add_argument(
        "--inventory-file",
        type=str,
        default="",
        help="JSON file mapping market_ticker -> inventory_yes; missing tickers default to 0",
    )
    parser.add_argument(
        "--market-meta-refresh-s",
        type=float,
        default=300.0,
        help="Seconds between REST refreshes of market close/expiry times for per-ticker tau",
    )
    parser.add_argument("--min-spread", type=float, default=0.02, help="Only markets with YES spread >= this")
    parser.add_argument("--max-markets", type=int, default=12, help="Max markets to log per cycle")
    parser.add_argument("--mid-history", type=int, default=80, help="Rolling mids per ticker")
    parser.add_argument("--sigma-min-samples", type=int, default=12, help="Min mids before sigma estimate")
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
    parser.add_argument(
        "--calibration-log-file",
        type=str,
        default="",
        help="JSONL path for per-ticker calibration rows (default: env KALSHI_AS_CALIBRATION_LOG or data/kalshi/as_calibration.jsonl)",
    )
    parser.add_argument(
        "--execution-mode",
        type=str,
        default="off",
        choices=["off", "dry-run", "live"],
        help="Place/cancel model quotes via REST. off=dont trade, dry-run=log actions only, live=send API calls",
    )
    parser.add_argument(
        "--execution-contracts",
        type=int,
        default=1,
        help="Contracts per side to quote in execution mode (capped separately by execution engine max).",
    )
    parser.add_argument(
        "--execution-max-contracts",
        type=int,
        default=10,
        help="Hard cap on contracts per order for execution mode.",
    )
    parser.add_argument(
        "--execution-monitor-poll-s",
        type=float,
        default=2.0,
        help="Seconds between REST polls for order status monitoring (Step 5).",
    )
    parser.add_argument(
        "--monitor-mode",
        type=str,
        default="ws",
        choices=["ws", "rest"],
        help="Step 5/6 monitoring source. ws=use websocket fills (no polling). rest=use REST polling monitor.",
    )
    parser.add_argument(
        "--execution-events-file",
        type=str,
        default="",
        help="JSONL output path for execution monitor events (default: data/kalshi/as_execution_events.jsonl)",
    )
    parser.add_argument(
        "--ledger-file",
        type=str,
        default="",
        help="JSONL output path for Step 6 inventory/PnL snapshots (default: data/kalshi/as_ledger.jsonl)",
    )
    parser.add_argument(
        "--assumed-fee-cents",
        type=float,
        default=0.0,
        help="Assumed fee per contract (in cents) for Step 6 cash accounting (optional).",
    )
    parser.add_argument(
        "--terminal-tau-minutes",
        type=float,
        default=5.0,
        help="If time-to-expiry (tau) is below this, stop quoting and cancel resting quotes (Step 7).",
    )
    parser.add_argument(
        "--terminal-actions-file",
        type=str,
        default="",
        help="JSONL output path for Step 7 terminal/flatten-intent records (default: data/kalshi/as_terminal_actions.jsonl)",
    )
    parser.add_argument(
        "--whitelist-tickers",
        type=str,
        default="",
        help="Comma-separated list of tickers to quote/trade (empty = no whitelist).",
    )
    parser.add_argument(
        "--ws-stale-s",
        type=float,
        default=10.0,
        help="If websocket ticker updates are older than this, trigger kill switch (cancel tagged orders + pause).",
    )
    parser.add_argument(
        "--max-tagged-resting-orders",
        type=int,
        default=20,
        help="Hard cap on number of tagged resting orders (safety for canary runs).",
    )
    parser.add_argument(
        "--panic-cancel-all",
        action="store_true",
        help="Cancel all tagged resting orders and exit immediately.",
    )
    args = parser.parse_args()

    base_url = os.getenv("KALSHI_WS_URL", "")
    out_dir = os.getenv("KALSHI_WS_OUT_DIR", "data/kalshi/ws")
    trade_buffer = int(os.getenv("KALSHI_WS_TRADE_BUFFER", "5000"))

    cfg = ASConfig(gamma=args.gamma, k=args.k, tau_hours=args.tau_hours, A=args.A, tick=args.tick)
    inventory_by_ticker = load_inventory_by_ticker(args.inventory_file or None) if args.inventory_file else {}
    client = KalshiClient(KalshiAuth.from_env())
    market_meta = MarketMetaCache(
        client=client,
        refresh_s=args.market_meta_refresh_s,
        default_tau_hours=args.tau_hours,
    )
    execution = ExecutionEngine(client, mode=args.execution_mode, max_contracts=args.execution_max_contracts)
    if args.panic_cancel_all and execution.mode != "off":
        msgs = execution.reconcile_cancel_all_tagged()
        for m in msgs[:50]:
            print(m)
        raise SystemExit(0)
    ledger = PortfolioLedger(
        out_path=(args.ledger_file or "data/kalshi/as_ledger.jsonl"),
        assumed_fee_cents_per_contract=args.assumed_fee_cents,
    )
    monitor = ExecutionMonitor(
        client,
        event_log_path=(args.execution_events_file or "data/kalshi/as_execution_events.jsonl"),
        poll_s=args.execution_monitor_poll_s,
        ledger=ledger,
    )

    async def _run() -> None:
        # Warm metadata once at startup so early quote cycles have ticker-specific tau when available.
        await asyncio.to_thread(market_meta.refresh)
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
                inventory_by_ticker=inventory_by_ticker,
                market_meta=market_meta,
                min_spread=args.min_spread,
                max_markets=args.max_markets,
                mid_history_len=args.mid_history,
                sigma_min_samples=args.sigma_min_samples,
                sample_contracts_per_side=args.sample_contracts,
                sample_orders_path=args.sample_orders_file or None,
                calibration_log_path=args.calibration_log_file or None,
                execution=execution,
                execution_contracts=args.execution_contracts,
                execution_monitor=monitor,
                ledger=ledger,
                terminal_tau_minutes=args.terminal_tau_minutes,
                terminal_actions_path=args.terminal_actions_file or None,
                whitelist_tickers=set(t.strip() for t in args.whitelist_tickers.split(",") if t.strip()) or None,
                ws_stale_s=args.ws_stale_s,
                max_tagged_resting_orders=args.max_tagged_resting_orders,
                kill_on_stale=True,
            ),
            # Step 5/6: choose monitoring mode.
            (run_ws_fill_consumer(ledger=ledger) if args.monitor_mode == "ws" else monitor.run_forever()),
        )

    asyncio.run(_run())


if __name__ == "__main__":
    main()
