from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time

from dotenv import load_dotenv

from kalshi_ws.stream import get_market_states, run_ws_stream

from .config import Config, config_summary, load_config
from .filter import get_candidates, run_evaluation
from .metadata import refresh_metadata
from .transitions import TransitionLogger

logger = logging.getLogger(__name__)


async def run(config_path: str = "config.json") -> None:
    """Main orchestrator — starts all subsystems and runs until interrupted."""
    load_dotenv(override=False)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    # ── 1. Load config ────────────────────────────────────────────────
    config = load_config(config_path)
    print("=" * 60)
    print("Kalshi Market-Making System — Tier 1 Filter")
    print(f"Config: {config_path}")
    print(f"Metadata refresh: every {config.metadata.refresh_interval_seconds}s")
    print(f"Evaluation interval: every {config.evaluation.interval_seconds}s")
    print("=" * 60)
    logger.info("Config loaded:\n%s", config_summary(config))

    # ── 2. Initial metadata bootstrap ─────────────────────────────────
    print("Loading market metadata from REST API...")
    n_markets = await refresh_metadata(config)
    print(f"Loaded metadata for {n_markets} active markets")

    # ── 3. Transition logger ──────────────────────────────────────────
    tlogger = TransitionLogger(config.paths.transition_log_dir)

    # ── 4. Start WebSocket stream as background task ──────────────────
    ws_task = asyncio.create_task(
        run_ws_stream(
            out_dir=config.paths.ws_out_dir,
            trade_buffer_size=config.websocket.trade_buffer_size,
        )
    )
    print("WebSocket stream started. Waiting for initial data...")

    # Wait until at least one ticker message arrives
    for _ in range(120):  # up to 60 seconds
        if get_market_states():
            break
        await asyncio.sleep(0.5)

    if not get_market_states():
        logger.error("No market data received after 60s — check credentials and connectivity.")
        ws_task.cancel()
        return

    print(f"Receiving data for {len(get_market_states())} markets")

    # ── 5. Metadata refresh loop (background) ─────────────────────────
    async def metadata_loop() -> None:
        while True:
            await asyncio.sleep(config.metadata.refresh_interval_seconds)
            try:
                await refresh_metadata(config)
            except Exception:
                logger.exception("Metadata refresh loop error")

    meta_task = asyncio.create_task(metadata_loop())

    # ── 6. Evaluation loop ────────────────────────────────────────────
    first_eval = True
    try:
        while True:
            summary = run_evaluation(config, on_transition=tlogger.log_transition)
            tlogger.log_eval_summary(summary)

            if first_eval:
                passes_req = config.tier1.consecutive_passes_required
                print(
                    f"FIRST EVAL: {summary['total']} markets scanned | "
                    f"0 WATCHING (need {passes_req} consecutive passes)"
                )
                first_eval = False

            candidates = get_candidates()
            if candidates:
                logger.info("WATCHING (%d): %s", len(candidates), ", ".join(candidates[:20]))

            await asyncio.sleep(config.evaluation.interval_seconds)

    except asyncio.CancelledError:
        logger.info("Evaluation loop cancelled")
    finally:
        # ── 7. Graceful shutdown ──────────────────────────────────────
        logger.info("Shutting down...")
        meta_task.cancel()
        ws_task.cancel()
        try:
            await meta_task
        except asyncio.CancelledError:
            pass
        try:
            await ws_task
        except asyncio.CancelledError:
            pass
        logger.info("Shutdown complete.")


def main() -> None:
    config_path = os.getenv("KALSHI_CONFIG_PATH", "config.json")
    try:
        asyncio.run(run(config_path))
    except KeyboardInterrupt:
        print("\nInterrupted. Goodbye.")


if __name__ == "__main__":
    main()
