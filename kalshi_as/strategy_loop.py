from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Set

from kalshi_ws.models import MarketTicker
from kalshi_ws.stream import get_market_states

from .calibration_log import append_records_jsonl as append_calibration_jsonl
from .calibration_log import build_calibration_record
from .execution import ExecutionEngine, QuoteIntent
from .execution_monitor import ExecutionMonitor
from .inventory import inventory_for_ticker
from .ledger import PortfolioLedger
from .market_meta import MarketMetaCache
from .model import ASConfig, compute_quotes
from .sample_orders import append_records_jsonl, build_sample_order_record
from .sigma import estimate_sigma_per_sqrt_hour
from .terminal_actions import append_records_jsonl as append_terminal_jsonl
from .terminal_actions import build_terminal_record

logger = logging.getLogger(__name__)

def _ws_is_stale(states: Dict[str, MarketTicker], *, now_s: float, stale_s: float) -> bool:
    """Return True if we haven't seen any ticker updates recently."""
    if stale_s <= 0:
        return False
    latest_ts = 0
    for mt in states.values():
        if mt.last_update_ts > latest_ts:
            latest_ts = mt.last_update_ts
    if latest_ts <= 0:
        return True
    return (now_s - float(latest_ts)) > float(stale_s)


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
    execution: Optional[ExecutionEngine] = None,
    execution_contracts: int = 1,
    execution_monitor: Optional[ExecutionMonitor] = None,
    ledger: Optional[PortfolioLedger] = None,
    terminal_tau_minutes: float = 5.0,
    terminal_actions_path: Optional[str] = None,
    whitelist_tickers: Optional[Set[str]] = None,
    ws_stale_s: float = 10.0,
    max_tagged_resting_orders: int = 20,
    kill_on_stale: bool = True,
) -> None:
    """Periodically compute AS quotes from live `get_market_states()` (websocket must run)."""
    history: Dict[str, Deque[float]] = {}
    out_path = sample_orders_path or os.getenv("KALSHI_AS_SAMPLE_ORDERS", "data/kalshi/as_sample_orders.jsonl")
    calib_path = calibration_log_path or os.getenv("KALSHI_AS_CALIBRATION_LOG", "data/kalshi/as_calibration.jsonl")
    inv_map = inventory_by_ticker or {}
    term_path = terminal_actions_path or os.getenv("KALSHI_AS_TERMINAL_ACTIONS", "data/kalshi/as_terminal_actions.jsonl")
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

        # Canary safety: if websocket data is stale, cancel all tagged orders and stop quoting.
        now_s = time.time()
        if kill_on_stale and _ws_is_stale(states, now_s=now_s, stale_s=ws_stale_s):
            if execution is not None and execution.mode != "off":
                try:
                    msgs = await asyncio.to_thread(execution.reconcile_cancel_all_tagged)
                    logger.error("WS stale (>%ss). Canceled tagged orders and pausing. %s", ws_stale_s, "; ".join(msgs[:3]))
                except Exception:
                    logger.exception("WS stale cancel-all failed")
            else:
                logger.error("WS stale (>%ss). Execution off; pausing.", ws_stale_s)
            continue

        candidates: list[tuple[str, MarketTicker, float]] = []
        for ticker, mt in states.items():
            if whitelist_tickers is not None and ticker not in whitelist_tickers:
                continue
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
            if ledger is not None:
                # If live fills are updating the ledger, prefer that over static inventory inputs.
                q_inventory = float(ledger.qty_for_ticker(ticker))

            # Step 7: terminal condition. Stop quoting near expiry; cancel resting quotes and emit flatten intent.
            if float(tau_hours) * 60.0 <= float(max(0.0, terminal_tau_minutes)):
                if ledger is not None:
                    ledger.set_mid(ticker, int(round(m * 100)))
                term_action = "flatten_intent" if abs(q_inventory) > 0 else "stop_quoting"
                term_note = (
                    "Near expiry threshold reached; cancel resting quotes. "
                    "If qty_yes != 0, execution layer should flatten position."
                )
                try:
                    await asyncio.to_thread(
                        append_terminal_jsonl,
                        term_path,
                        [build_terminal_record(market_ticker=ticker, tau_hours=tau_hours, qty_yes=q_inventory, action=term_action, note=term_note)],
                    )
                except Exception:
                    logger.exception("Failed writing terminal action record for %s", ticker)
                if execution is not None and execution.mode != "off":
                    try:
                        events, msgs = await asyncio.to_thread(execution.cancel_all_quotes_events, ticker=ticker, mid_cents=int(round(m * 100)))
                        if execution_monitor is not None and events:
                            await asyncio.to_thread(execution_monitor.ingest_events, events)
                        lines.append(f"{ticker} terminal: tau={tau_hours:.4f}h action={term_action} | exec: " + "; ".join(msgs[:3]))
                    except Exception:
                        logger.exception("Terminal cancel failed for %s", ticker)
                else:
                    lines.append(f"{ticker} terminal: tau={tau_hours:.4f}h action={term_action}")
                shown += 1
                continue
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
            if ledger is not None:
                ledger.set_mid(ticker, int(round(m * 100)))

            # Step 4 (optional): sync resting limit quotes to the exchange.
            if execution is not None and execution.mode != "off":
                # Canary safety: cap total number of our resting orders.
                try:
                    n_rest = await asyncio.to_thread(execution.count_tagged_resting)
                except Exception:
                    n_rest = 0
                if int(n_rest) >= int(max_tagged_resting_orders):
                    lines.append(f"{ticker} safety: too many tagged resting orders ({n_rest}); skipping execution this cycle")
                    shown += 1
                    continue
                bid_cents = int(round(q.bid * 100))
                ask_cents = int(round(q.ask * 100))
                mid_cents = int(round(m * 100))
                intent = QuoteIntent(
                    market_ticker=ticker,
                    yes_bid_cents=bid_cents,
                    yes_ask_cents=ask_cents,
                    contracts=int(max(1, execution_contracts)),
                    mid_cents=mid_cents,
                )
                try:
                    events, acts = await asyncio.to_thread(execution.sync_quotes_events, intent)
                    if acts:
                        line += " | exec: " + "; ".join(acts[:3])
                    if execution_monitor is not None and events:
                        await asyncio.to_thread(execution_monitor.ingest_events, events)
                except Exception:
                    logger.exception("Execution sync failed for %s", ticker)
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
