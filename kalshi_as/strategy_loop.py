from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from kalshi_ws.models import MarketTicker
from kalshi_ws.stream import get_market_states

from .calibration_log import append_records_jsonl as append_calibration_jsonl
from .calibration_log import build_calibration_record
from .inventory import inventory_for_ticker
from .market_meta import MarketMetaCache
from .model import ASConfig, compute_quotes
from .sample_orders import append_records_jsonl, build_sample_order_record
from .sigma import estimate_sigma_per_sqrt_hour

logger = logging.getLogger(__name__)


def _mid(mt: MarketTicker) -> Optional[float]:
    if mt.yes_bid <= 0 or mt.yes_ask <= 0:
        return None
    if mt.yes_ask <= mt.yes_bid:
        return None
    return 0.5 * (mt.yes_bid + mt.yes_ask)


async def run_as_strategy_loop(
    *,
    interval_s: float,
    config: ASConfig,
    inventory_yes: float = 0.0,
    inventory_by_ticker: Optional[Dict[str, float]] = None,
    market_meta: Optional[MarketMetaCache] = None,
    min_spread: float = 0.0,
    max_markets: int = 12,
    mid_history_len: int = 80,
    sigma_min_samples: int = 12,
    sample_contracts_per_side: float = 0.0,
    sample_orders_path: Optional[str] = None,
    calibration_log_path: Optional[str] = None,
) -> None:
    """Periodically compute AS quotes from live `get_market_states()` (websocket must run)."""
    history: Dict[str, Deque[float]] = {}
    out_path = sample_orders_path or os.getenv("KALSHI_AS_SAMPLE_ORDERS", "data/kalshi/as_sample_orders.jsonl")
    calib_path = calibration_log_path or os.getenv("KALSHI_AS_CALIBRATION_LOG", "data/kalshi/as_calibration.jsonl")
    inv_map = inventory_by_ticker or {}
    while True:
        await asyncio.sleep(interval_s)
        if market_meta is not None:
            try:
                await asyncio.to_thread(market_meta.maybe_refresh, now_ts=time.time())
            except Exception:
                logger.exception("Failed refreshing market metadata; using default tau fallback.")
        states: Dict[str, MarketTicker] = get_market_states()
        if not states:
            logger.info("Avellaneda–Stoikov: no market state yet (waiting for ticker updates).")
            continue

        candidates: list[tuple[str, MarketTicker, float]] = []
        for ticker, mt in states.items():
            m = _mid(mt)
            if m is None:
                continue
            if mt.spread < min_spread:
                continue
            candidates.append((ticker, mt, m))

        candidates.sort(key=lambda x: x[1].spread, reverse=True)
        candidates = candidates[: max(max_markets * 4, max_markets)]

        lines: list[str] = []
        json_rows: List[Dict[str, Any]] = []
        calibration_rows: List[Dict[str, Any]] = []
        shown = 0
        for ticker, mt, _ in candidates:
            if shown >= max_markets:
                break
            m = _mid(mt)
            if m is None:
                continue
            buf = history.get(ticker)
            if buf is None:
                buf = deque(maxlen=mid_history_len)
                history[ticker] = buf
            buf.append(m)

            sigma = estimate_sigma_per_sqrt_hour(
                buf,
                sample_interval_s=interval_s,
                min_samples=sigma_min_samples,
                ewma_alpha=0.25,
            )
            if sigma is None:
                continue

            q_inventory = inventory_for_ticker(inv_map, ticker) if inv_map else float(inventory_yes)
            tau_hours = market_meta.tau_hours_for_ticker(ticker) if market_meta is not None else float(config.tau_hours)
            try:
                q = compute_quotes(
                    m,
                    inventory_yes=q_inventory,
                    sigma=sigma,
                    config=config,
                    tau_hours=tau_hours,
                )
            except ValueError:
                continue

            line = (
                f"{ticker} mid={m:.4f} sigma~{sigma:.3f} book={mt.yes_bid:.2f}/{mt.yes_ask:.2f} "
                f"r={q.reservation:.4f} bid={q.bid:.2f} ask={q.ask:.2f} (q={q_inventory:.2f}, A={config.A}, gamma={config.gamma}, k={config.k}, tau={tau_hours:.3f}h)"
            )
            calibration_rows.append(
                build_calibration_record(
                    market_ticker=ticker,
                    mid=m,
                    model_bid=q.bid,
                    model_ask=q.ask,
                    A=config.A,
                    k=config.k,
                    gamma=config.gamma,
                )
            )
            if sample_contracts_per_side > 0:
                n = sample_contracts_per_side
                line += f" | sample orders: BUY YES {n:g} @ {q.bid:.2f}, SELL YES {n:g} @ {q.ask:.2f}"
                json_rows.append(
                    build_sample_order_record(
                        market_ticker=ticker,
                        model_quotes=q,
                        sigma=sigma,
                        book_bid=mt.yes_bid,
                        book_ask=mt.yes_ask,
                        sample_count_per_side=n,
                        gamma=config.gamma,
                        k=config.k,
                        tau_hours=tau_hours,
                        inventory_yes=q_inventory,
                    )
                )
            lines.append(line)
            shown += 1

        if json_rows:
            try:
                await asyncio.to_thread(append_records_jsonl, out_path, json_rows)
            except OSError:
                logger.exception("Failed appending sample orders to %s", out_path)
        if calibration_rows:
            try:
                await asyncio.to_thread(append_calibration_jsonl, calib_path, calibration_rows)
            except OSError:
                logger.exception("Failed appending calibration rows to %s", calib_path)

        if lines:
            logger.info("Avellaneda–Stoikov quotes:\n%s", "\n".join(lines))
