from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from kalshi_ws.models import MarketTicker, Trade
from kalshi_ws.stream import get_market_states, get_trade_buffer

from .config import Config
from .metadata import MarketMetadata, get_metadata

logger = logging.getLogger(__name__)


# ── Per-market tracker ────────────────────────────────────────────────

@dataclass
class MarketTracker:
    ticker: str
    status: str = "IGNORED"
    consecutive_passes: int = 0
    consecutive_fails: int = 0
    demoted_at: Optional[float] = None
    demotion_count: int = 0
    cooldown_until: Optional[float] = None
    last_eval_result: str = ""
    last_eval_time: float = 0.0
    promoted_at: Optional[float] = None
    # For computing update rate between evaluations
    prev_update_count: int = 0
    prev_eval_time: float = 0.0


# ── Module-level state ────────────────────────────────────────────────

_trackers: Dict[str, MarketTracker] = {}


def get_candidates() -> List[str]:
    """Return tickers with status WATCHING."""
    return [t for t, tr in _trackers.items() if tr.status == "WATCHING"]


def get_market_status(ticker: str) -> Optional[str]:
    """Return the lifecycle status for a ticker, or None if untracked."""
    tr = _trackers.get(ticker)
    return tr.status if tr else None


def get_all_trackers() -> Dict[str, MarketTracker]:
    """Return the full tracker dict (for debugging/logging)."""
    return _trackers


# ── Single-market evaluation (pure logic) ─────────────────────────────

def evaluate_market(
    market_state: MarketTicker,
    trade_buffer: deque,
    metadata: Optional[MarketMetadata],
    config: Config,
    update_rate: Optional[float],
    now: float,
) -> Tuple[bool, str]:
    """Evaluate one market against Tier 1 criteria.

    Returns ``(passed, reason)`` where *reason* is ``"PASS"`` or a short
    description of why the market failed.
    """
    # No quotes on one side
    if market_state.yes_bid == 0.0 or market_state.yes_ask == 0.0:
        return (False, "no quotes")

    # 1. Spread
    if market_state.spread < config.tier1.min_spread:
        return (False, f"spread too tight ({market_state.spread:.3f})")
    if market_state.spread > config.tier1.max_spread:
        return (False, f"spread too wide ({market_state.spread:.3f})")

    # 2. Decided
    if market_state.yes_bid >= config.tier1.max_confidence_threshold:
        return (False, f"decided YES (bid={market_state.yes_bid:.2f})")
    if market_state.yes_ask <= config.tier1.min_confidence_threshold:
        return (False, f"decided NO (ask={market_state.yes_ask:.2f})")

    # 3. Expiry (skip if metadata unavailable)
    if metadata is not None:
        tte = metadata.expiration_time - now
        if tte < config.tier1.min_expiry_seconds:
            return (False, f"expiring soon ({tte:.0f}s)")

    # 4. Volume
    if market_state.dollar_volume < config.tier1.min_dollar_volume:
        return (False, f"low volume (${market_state.dollar_volume})")

    # 5. Activity (skip on first eval when rate is unknown)
    if update_rate is not None and update_rate < config.tier1.min_update_rate:
        return (False, f"low activity ({update_rate:.1f}/min)")

    # 6. Imbalance (skip if not enough trades)
    if len(trade_buffer) >= config.tier1.min_trades_for_imbalance:
        yes_count = sum(1 for t in trade_buffer if t.taker_side == "yes")
        yes_ratio = yes_count / len(trade_buffer)
        deviation = abs(yes_ratio - 0.5)
        if deviation > config.tier1.max_imbalance_deviation:
            return (False, f"imbalanced ({yes_ratio:.2f})")

    return (True, "PASS")


# ── Helpers ───────────────────────────────────────────────────────────

def _compute_update_rate(
    tracker: MarketTracker, market_state: MarketTicker, now: float,
) -> Optional[float]:
    """Updates per minute between the previous eval and *now*."""
    if tracker.prev_eval_time <= 0:
        return None
    elapsed_min = (now - tracker.prev_eval_time) / 60.0
    if elapsed_min <= 0:
        return None
    delta = market_state.update_count - tracker.prev_update_count
    return delta / elapsed_min


def compute_metrics(
    market_state: MarketTicker,
    trade_buffer: deque,
    metadata: Optional[MarketMetadata],
    update_rate: Optional[float],
    now: float,
) -> dict:
    """Build a summary metrics dict for transition logging."""
    imbalance = None
    if trade_buffer:
        yes_count = sum(1 for t in trade_buffer if t.taker_side == "yes")
        imbalance = round(yes_count / len(trade_buffer), 3)
    tte = int(metadata.expiration_time - now) if metadata else None
    return {
        "spread": market_state.spread,
        "dollar_volume": market_state.dollar_volume,
        "yes_bid": market_state.yes_bid,
        "yes_ask": market_state.yes_ask,
        "imbalance": imbalance,
        "update_rate": round(update_rate, 2) if update_rate is not None else None,
        "time_to_expiry": tte,
    }


def _demote(tracker: MarketTracker, config: Config, now: float) -> str:
    """Demote a market.  Returns the new status string."""
    tracker.demoted_at = now
    tracker.demotion_count += 1
    tracker.consecutive_passes = 0
    tracker.consecutive_fails = 0

    if tracker.demotion_count >= config.cooldowns.max_demotions_before_blacklist:
        tracker.status = "BLACKLISTED"
        tracker.cooldown_until = None
        return "BLACKLISTED"

    if tracker.demotion_count == 1:
        cooldown = config.cooldowns.first_demotion_seconds
    else:
        cooldown = config.cooldowns.second_demotion_seconds
    tracker.cooldown_until = now + cooldown
    tracker.status = "DEMOTED"
    return "DEMOTED"


# ── Full evaluation loop ──────────────────────────────────────────────

def run_evaluation(config: Config, on_transition=None) -> dict:
    """Run one evaluation cycle across all markets in ``market_states``.

    Parameters
    ----------
    config:
        The loaded Config object.
    on_transition:
        Optional callback ``(ticker, old_status, new_status, tracker, metrics)``.

    Returns
    -------
    dict
        Summary with keys ``total``, ``IGNORED``, ``WATCHING``, ``DEMOTED``,
        ``BLACKLISTED``.
    """
    now = time.time()
    market_states = get_market_states()

    for ticker, state in market_states.items():
        tracker = _trackers.get(ticker)
        if tracker is None:
            tracker = MarketTracker(ticker=ticker)
            _trackers[ticker] = tracker

        # ── Blacklisted: skip ─────────────────────────────────────
        if tracker.status == "BLACKLISTED":
            continue

        # ── Demoted: check cooldown ───────────────────────────────
        if tracker.status == "DEMOTED":
            if tracker.cooldown_until is not None and now < tracker.cooldown_until:
                continue
            # Cooldown expired → return to IGNORED for re-evaluation
            old = tracker.status
            tracker.status = "IGNORED"
            tracker.consecutive_passes = 0
            tracker.consecutive_fails = 0
            if on_transition:
                metadata = get_metadata(ticker)
                trade_buffer = get_trade_buffer(ticker)
                ur = _compute_update_rate(tracker, state, now)
                on_transition(ticker, old, "IGNORED", tracker,
                              compute_metrics(state, trade_buffer, metadata, ur, now))

        # ── Metadata required for evaluation ──────────────────────
        metadata = get_metadata(ticker)
        if metadata is None:
            # New market not yet in REST data — skip, don't penalize
            continue

        trade_buffer = get_trade_buffer(ticker)
        update_rate = _compute_update_rate(tracker, state, now)

        passed, reason = evaluate_market(state, trade_buffer, metadata, config, update_rate, now)
        tracker.last_eval_result = reason
        tracker.last_eval_time = now
        tracker.prev_update_count = state.update_count
        tracker.prev_eval_time = now

        # ── IGNORED ───────────────────────────────────────────────
        if tracker.status == "IGNORED":
            if passed:
                tracker.consecutive_passes += 1
                if tracker.consecutive_passes >= config.tier1.consecutive_passes_required:
                    old = "IGNORED"
                    tracker.status = "WATCHING"
                    tracker.promoted_at = now
                    tracker.consecutive_fails = 0
                    if on_transition:
                        on_transition(ticker, old, "WATCHING", tracker,
                                      compute_metrics(state, trade_buffer, metadata, update_rate, now))
            else:
                tracker.consecutive_passes = 0

        # ── WATCHING ──────────────────────────────────────────────
        elif tracker.status == "WATCHING":
            if passed:
                tracker.consecutive_fails = 0
            else:
                tracker.consecutive_fails += 1
                if tracker.consecutive_fails >= config.tier1.consecutive_fails_allowed:
                    old = "WATCHING"
                    metrics = compute_metrics(state, trade_buffer, metadata, update_rate, now)
                    new_status = _demote(tracker, config, now)
                    if on_transition:
                        on_transition(ticker, old, new_status, tracker, metrics)

    # Count final statuses for markets currently in market_states
    counts = {"IGNORED": 0, "WATCHING": 0, "DEMOTED": 0, "BLACKLISTED": 0}
    for ticker in market_states:
        tr = _trackers.get(ticker)
        if tr and tr.status in counts:
            counts[tr.status] += 1
    return {"total": len(market_states), **counts}
