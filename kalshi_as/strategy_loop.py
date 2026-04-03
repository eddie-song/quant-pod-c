from __future__ import annotations

import asyncio
import logging
import os
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from kalshi_ws.models import MarketTicker
from kalshi_ws.stream import get_market_states

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
    min_spread: float = 0.0,
    max_markets: int = 12,
    mid_history_len: int = 80,
    sigma_min_samples: int = 12,
    sample_contracts_per_side: float = 0.0,
    sample_orders_path: Optional[str] = None,
) -> None:
    """Periodically compute AS quotes from live `get_market_states()` (websocket must run)."""
    history: Dict[str, Deque[float]] = {}
    out_path = sample_orders_path or os.getenv("KALSHI_AS_SAMPLE_ORDERS", "data/kalshi/as_sample_orders.jsonl")
    while True:
        await asyncio.sleep(interval_s)
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
            )
            if sigma is None:
                continue

            try:
                q = compute_quotes(
                    m,
                    inventory_yes=inventory_yes,
                    sigma=sigma,
                    config=config,
                )
            except ValueError:
                continue

            line = (
                f"{ticker} mid={m:.4f} σ≈{sigma:.3f} book={mt.yes_bid:.2f}/{mt.yes_ask:.2f} "
                f"r={q.reservation:.4f} bid={q.bid:.2f} ask={q.ask:.2f} (γ={config.gamma}, k={config.k}, τ={config.tau_hours}h)"
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
                        tau_hours=config.tau_hours,
                        inventory_yes=inventory_yes,
                    )
                )
            lines.append(line)
            shown += 1

        if json_rows:
            try:
                await asyncio.to_thread(append_records_jsonl, out_path, json_rows)
            except OSError:
                logger.exception("Failed appending sample orders to %s", out_path)

        if lines:
            logger.info("Avellaneda–Stoikov quotes:\n%s", "\n".join(lines))