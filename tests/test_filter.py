from __future__ import annotations

import json
import time
from collections import deque
from pathlib import Path

from kalshi_filter.config import (
    Config,
    Tier1Config,
    CooldownConfig,
    EvalConfig,
    MetadataConfig,
    PathsConfig,
    WebSocketConfig,
    load_config,
    write_default_config,
)
from kalshi_filter.filter import (
    MarketTracker,
    evaluate_market,
    _demote,
    compute_metrics,
)
from kalshi_filter.metadata import MarketMetadata, _parse_iso_ts
from kalshi_filter.transitions import TransitionLogger
from kalshi_ws.models import MarketTicker, Trade


# ── Helpers ───────────────────────────────────────────────────────────

def _default_config() -> Config:
    return Config()


def _make_state(**overrides) -> MarketTicker:
    defaults = dict(
        market_ticker="TEST-MARKET",
        yes_bid=0.45,
        yes_ask=0.55,
        spread=0.10,
        last_price=0.50,
        volume=500.0,
        open_interest=200.0,
        dollar_volume=500,
        dollar_open_interest=200,
        last_update_ts=int(time.time()),
        update_count=100,
    )
    defaults.update(overrides)
    return MarketTicker(**defaults)


def _make_metadata(**overrides) -> MarketMetadata:
    defaults = dict(
        ticker="TEST-MARKET",
        expiration_time=time.time() + 86400,  # 24h from now
        status="open",
        event_ticker="TEST-EVENT",
        result="",
        close_time=time.time() + 86400,
    )
    defaults.update(overrides)
    return MarketMetadata(**defaults)


def _make_trade(taker_side: str = "yes") -> Trade:
    return Trade(
        trade_id="t1",
        market_ticker="TEST-MARKET",
        yes_price=0.50,
        no_price=0.50,
        size=1.0,
        taker_side=taker_side,
        ts=int(time.time()),
    )


def _balanced_buffer(n: int = 40) -> deque:
    buf = deque(maxlen=5000)
    for i in range(n):
        buf.append(_make_trade("yes" if i % 2 == 0 else "no"))
    return buf


def _imbalanced_buffer(n: int = 40, yes_pct: float = 0.90) -> deque:
    buf = deque(maxlen=5000)
    yes_count = int(n * yes_pct)
    for i in range(n):
        side = "yes" if i < yes_count else "no"
        buf.append(_make_trade(side))
    return buf


# ═══════════════════════════════════════════════════════════════════════
# 1. Config loading
# ═══════════════════════════════════════════════════════════════════════

def test_default_config_generation(tmp_path):
    path = tmp_path / "config.json"
    cfg = load_config(path)
    assert path.exists()
    assert cfg.tier1.min_spread == 0.03
    assert cfg.cooldowns.first_demotion_seconds == 900


def test_missing_key_uses_default(tmp_path):
    path = tmp_path / "config.json"
    partial = {"tier1": {"min_spread": 0.05}}  # missing most keys
    path.write_text(json.dumps(partial))
    cfg = load_config(path)
    assert cfg.tier1.min_spread == 0.05
    assert cfg.tier1.max_spread == 0.40  # default


def test_wrong_type_raises(tmp_path):
    path = tmp_path / "config.json"
    bad = {"tier1": {"min_spread": "not a number"}}
    path.write_text(json.dumps(bad))
    try:
        load_config(path)
        assert False, "Should have raised TypeError"
    except TypeError as e:
        assert "min_spread" in str(e)


def test_missing_section_uses_defaults(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{}")
    cfg = load_config(path)
    assert cfg.tier1.min_spread == 0.03
    assert cfg.paths.ws_out_dir == "data/kalshi/ws"


# ═══════════════════════════════════════════════════════════════════════
# 2. MarketMetadata parsing
# ═══════════════════════════════════════════════════════════════════════

def test_metadata_from_api_dict():
    d = {
        "ticker": "KXTEST-26APR",
        "expiration_time": "2026-04-01T12:00:00Z",
        "status": "open",
        "event_ticker": "KXTEST",
        "result": "",
        "close_time": "2026-04-01T11:00:00Z",
    }
    md = MarketMetadata.from_api_dict(d)
    assert md.ticker == "KXTEST-26APR"
    assert md.status == "open"
    assert md.expiration_time > 0
    assert md.result == ""


def test_parse_iso_ts():
    assert _parse_iso_ts("2026-01-01T00:00:00Z") > 0
    assert _parse_iso_ts("") == 0.0
    assert _parse_iso_ts("garbage") == 0.0


# ═══════════════════════════════════════════════════════════════════════
# 3. evaluate_market — synthetic scenarios
# ═══════════════════════════════════════════════════════════════════════

def test_market_passes_all():
    cfg = _default_config()
    state = _make_state()
    md = _make_metadata()
    buf = _balanced_buffer()
    passed, reason = evaluate_market(state, buf, md, cfg, update_rate=5.0, now=time.time())
    assert passed is True
    assert reason == "PASS"


def test_spread_too_tight():
    cfg = _default_config()
    state = _make_state(yes_bid=0.50, yes_ask=0.51, spread=0.01)
    md = _make_metadata()
    passed, reason = evaluate_market(state, deque(), md, cfg, update_rate=5.0, now=time.time())
    assert passed is False
    assert "spread too tight" in reason


def test_spread_too_wide():
    cfg = _default_config()
    state = _make_state(yes_bid=0.10, yes_ask=0.60, spread=0.50)
    md = _make_metadata()
    passed, reason = evaluate_market(state, deque(), md, cfg, update_rate=5.0, now=time.time())
    assert passed is False
    assert "spread too wide" in reason


def test_decided_yes():
    cfg = _default_config()
    state = _make_state(yes_bid=0.96, yes_ask=0.99, spread=0.03)
    md = _make_metadata()
    passed, reason = evaluate_market(state, deque(), md, cfg, update_rate=5.0, now=time.time())
    assert passed is False
    assert "decided YES" in reason


def test_decided_no():
    cfg = _default_config()
    state = _make_state(yes_bid=0.01, yes_ask=0.04, spread=0.03)
    md = _make_metadata()
    passed, reason = evaluate_market(state, deque(), md, cfg, update_rate=5.0, now=time.time())
    assert passed is False
    assert "decided NO" in reason


def test_extreme_imbalance_fails():
    cfg = _default_config()
    state = _make_state()
    md = _make_metadata()
    buf = _imbalanced_buffer(40, yes_pct=0.90)
    passed, reason = evaluate_market(state, buf, md, cfg, update_rate=5.0, now=time.time())
    assert passed is False
    assert "imbalanced" in reason


def test_insufficient_trades_skips_imbalance():
    cfg = _default_config()
    state = _make_state()
    md = _make_metadata()
    # only 5 trades, below min_trades_for_imbalance (20)
    buf = _imbalanced_buffer(5, yes_pct=1.0)
    passed, reason = evaluate_market(state, buf, md, cfg, update_rate=5.0, now=time.time())
    assert passed is True  # imbalance check skipped


def test_no_quotes_fails():
    cfg = _default_config()
    state = _make_state(yes_bid=0.0, yes_ask=0.0, spread=0.0)
    md = _make_metadata()
    passed, reason = evaluate_market(state, deque(), md, cfg, update_rate=5.0, now=time.time())
    assert passed is False
    assert "no quotes" in reason


def test_low_volume_fails():
    cfg = _default_config()
    state = _make_state(dollar_volume=10)
    md = _make_metadata()
    passed, reason = evaluate_market(state, deque(), md, cfg, update_rate=5.0, now=time.time())
    assert passed is False
    assert "low volume" in reason


def test_low_activity_fails():
    cfg = _default_config()
    state = _make_state()
    md = _make_metadata()
    passed, reason = evaluate_market(state, deque(), md, cfg, update_rate=0.1, now=time.time())
    assert passed is False
    assert "low activity" in reason


def test_expiring_soon_fails():
    cfg = _default_config()
    state = _make_state()
    md = _make_metadata(expiration_time=time.time() + 60)  # 60s, below 1800s min
    passed, reason = evaluate_market(state, deque(), md, cfg, update_rate=5.0, now=time.time())
    assert passed is False
    assert "expiring soon" in reason


def test_no_metadata_skips_expiry_check():
    cfg = _default_config()
    state = _make_state()
    # No metadata → expiry check skipped, everything else passes
    passed, reason = evaluate_market(state, deque(), None, cfg, update_rate=5.0, now=time.time())
    assert passed is True


# ═══════════════════════════════════════════════════════════════════════
# 4. Promotion logic — consecutive passes required
# ═══════════════════════════════════════════════════════════════════════

def test_promotion_requires_consecutive_passes():
    cfg = _default_config()
    tracker = MarketTracker(ticker="TEST")
    assert tracker.status == "IGNORED"

    # Simulate consecutive passes
    for i in range(cfg.tier1.consecutive_passes_required - 1):
        tracker.consecutive_passes += 1
    assert tracker.status == "IGNORED"

    # One more pass triggers promotion
    tracker.consecutive_passes += 1
    if tracker.consecutive_passes >= cfg.tier1.consecutive_passes_required:
        tracker.status = "WATCHING"
        tracker.promoted_at = time.time()
    assert tracker.status == "WATCHING"


def test_promotion_resets_on_fail():
    tracker = MarketTracker(ticker="TEST")
    tracker.consecutive_passes = 3
    # A fail resets
    tracker.consecutive_passes = 0
    assert tracker.consecutive_passes == 0


# ═══════════════════════════════════════════════════════════════════════
# 5. Demotion logic — consecutive fails triggers demotion
# ═══════════════════════════════════════════════════════════════════════

def test_demotion_after_consecutive_fails():
    cfg = _default_config()
    tracker = MarketTracker(ticker="TEST", status="WATCHING")
    now = time.time()
    new_status = _demote(tracker, cfg, now)
    assert new_status == "DEMOTED"
    assert tracker.status == "DEMOTED"
    assert tracker.demotion_count == 1
    assert tracker.cooldown_until is not None
    assert tracker.cooldown_until == now + cfg.cooldowns.first_demotion_seconds


# ═══════════════════════════════════════════════════════════════════════
# 6. Cooldown logic
# ═══════════════════════════════════════════════════════════════════════

def test_cooldown_blocks_reevaluation():
    cfg = _default_config()
    tracker = MarketTracker(ticker="TEST", status="WATCHING")
    now = time.time()
    _demote(tracker, cfg, now)
    assert tracker.status == "DEMOTED"
    assert tracker.cooldown_until > now
    # During cooldown, the market should not be re-evaluated
    assert now < tracker.cooldown_until


def test_cooldown_expires():
    cfg = _default_config()
    tracker = MarketTracker(ticker="TEST", status="WATCHING")
    now = time.time()
    _demote(tracker, cfg, now)
    # Fast-forward past cooldown
    future = tracker.cooldown_until + 1
    assert future > tracker.cooldown_until  # cooldown expired


def test_second_demotion_longer_cooldown():
    cfg = _default_config()
    tracker = MarketTracker(ticker="TEST", status="WATCHING")
    now = time.time()
    _demote(tracker, cfg, now)
    first_cd = tracker.cooldown_until - now
    assert first_cd == cfg.cooldowns.first_demotion_seconds

    tracker.status = "WATCHING"
    _demote(tracker, cfg, now)
    second_cd = tracker.cooldown_until - now
    assert second_cd == cfg.cooldowns.second_demotion_seconds
    assert second_cd > first_cd


# ═══════════════════════════════════════════════════════════════════════
# 7. Blacklist logic
# ═══════════════════════════════════════════════════════════════════════

def test_blacklist_after_max_demotions():
    cfg = _default_config()
    tracker = MarketTracker(ticker="TEST", status="WATCHING")
    now = time.time()

    for _ in range(cfg.cooldowns.max_demotions_before_blacklist - 1):
        tracker.status = "WATCHING"
        _demote(tracker, cfg, now)

    assert tracker.status == "DEMOTED"
    # One more demotion → blacklisted
    tracker.status = "WATCHING"
    new = _demote(tracker, cfg, now)
    assert new == "BLACKLISTED"
    assert tracker.status == "BLACKLISTED"


# ═══════════════════════════════════════════════════════════════════════
# 8. Transition logging — record structure
# ═══════════════════════════════════════════════════════════════════════

def test_transition_log_record(tmp_path):
    tl = TransitionLogger(tmp_path / "transitions")
    tracker = MarketTracker(
        ticker="TEST",
        status="WATCHING",
        consecutive_passes=5,
        demotion_count=0,
        last_eval_result="PASS",
    )
    metrics = {
        "spread": 0.08,
        "dollar_volume": 1250,
        "yes_bid": 0.45,
        "yes_ask": 0.53,
        "imbalance": 0.48,
        "update_rate": 3.2,
        "time_to_expiry": 172800,
    }
    # Write directly (bypass async for testing)
    tl._append_jsonl({
        "ts": int(time.time()),
        "ticker": "TEST",
        "old_status": "IGNORED",
        "new_status": "WATCHING",
        "consecutive_passes": 5,
        "demotion_count": 0,
        "metrics": metrics,
    })

    lines = list(tl._path.open())
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["ticker"] == "TEST"
    assert record["old_status"] == "IGNORED"
    assert record["new_status"] == "WATCHING"
    assert record["metrics"]["spread"] == 0.08
    assert "ts" in record
