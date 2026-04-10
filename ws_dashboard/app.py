import glob
import json
import os
import sys
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Repo root must be on sys.path so `kalshi_ws` imports work when Streamlit runs
# from another cwd or via file:// launcher.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import numpy as np
import pandas as pd
import streamlit as st

from kalshi_ws.models import MarketTicker, Trade


def _latest_file(pattern: str) -> Optional[str]:
    files = glob.glob(pattern)
    if not files:
        return None
    return max(files, key=lambda p: os.path.getmtime(p))


def _parse_trade_line(line: str) -> Optional[Dict[str, Any]]:
    line = line.strip()
    if not line:
        return None
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None

    if data.get("type") != "trade":
        return None

    msg = data.get("msg") or {}
    trade = Trade.from_msg(msg)
    ts = trade.ts
    ts_dt = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None

    return {
        "ts": ts,
        "time_utc": ts_dt.isoformat() if ts_dt else None,
        "trade_id": trade.trade_id,
        "market_ticker": trade.market_ticker,
        "yes_price": trade.yes_price,
        "no_price": trade.no_price,
        "size": trade.size,
        "taker_side": trade.taker_side,
    }


def _parse_ticker_line(line: str) -> Optional[Dict[str, Any]]:
    line = line.strip()
    if not line:
        return None
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    if data.get("type") != "ticker":
        return None
    msg = data.get("msg")
    return msg if isinstance(msg, dict) else None


def _apply_ticker_msg(states: Dict[str, MarketTicker], msg: Dict[str, Any]) -> None:
    ticker = (msg.get("market_ticker") or "").strip()
    if not ticker:
        return
    existing = states.get(ticker)
    if existing is None:
        states[ticker] = MarketTicker.from_msg(msg)
    else:
        existing.update(msg)


def _tail_seek_bytes(path: str, max_tail_bytes: int) -> int:
    try:
        size = os.path.getsize(path)
    except OSError:
        return 0
    return max(0, size - max_tail_bytes)


def _seek_after_next_newline(path: str, pos: int) -> int:
    """If `pos` lands mid-line, skip to the start of the next line for JSONL reads."""
    try:
        with open(path, "rb") as f:
            f.seek(max(0, pos))
            f.readline()
            return f.tell()
    except OSError:
        return pos


SPREAD_HIST_MAXLEN = 200


def _record_spread_after_ticker_update(
    states: Dict[str, MarketTicker],
    spread_hist: Dict[str, deque],
    msg: Dict[str, Any],
) -> None:
    tkr = (msg.get("market_ticker") or "").strip()
    if not tkr:
        return
    mt = states.get(tkr)
    if mt is None:
        return
    if mt.yes_bid <= 0 or mt.yes_ask <= 0 or mt.yes_ask <= mt.yes_bid:
        return
    sp = float(mt.spread)
    if sp <= 0:
        return
    ts_k = int(mt.last_update_ts or 0)
    deq = spread_hist.setdefault(tkr, deque(maxlen=SPREAD_HIST_MAXLEN))
    if deq and deq[-1][0] == ts_k and deq[-1][1] == sp:
        return
    deq.append((ts_k, sp))


def _spread_hist_values(spread_hist: Dict[str, deque], tkr: str) -> List[float]:
    d = spread_hist.get(tkr)
    if not d:
        return []
    return [float(x[1]) for x in d]


def _spread_mean_and_stability(spreads: List[float], current_spread: float) -> Tuple[float, float]:
    arr = np.asarray(spreads, dtype=float) if spreads else np.asarray([current_spread], dtype=float)
    mean_sp = float(np.mean(arr)) if len(arr) else float(current_spread)
    if len(arr) < 3:
        stab = 0.5
    else:
        m = float(np.mean(arr))
        if m < 1e-6:
            stab = 0.5
        else:
            cv = float(np.std(arr) / m)
            stab = float(np.clip(1.0 / (1.0 + cv), 0.0, 1.0))
    return mean_sp, stab


def _trade_microstructure(df_market: pd.DataFrame) -> Dict[str, Any]:
    """Trade-based flow, two-sided activity, mean-reversion proxy, short-horizon vol (websocket window only)."""
    out: Dict[str, Any] = {
        "n_trades": 0,
        "contracts": 0.0,
        "trades_per_hour": 0.0,
        "two_sided": False,
        "reversion_01": 0.5,
        "trade_vol_score": 0.5,
    }
    if df_market is None or len(df_market) < 2:
        return out
    tdf = df_market.sort_values("ts")
    n = len(tdf)
    out["n_trades"] = n
    out["contracts"] = float(tdf["size"].sum())
    ts0, ts1 = int(tdf["ts"].iloc[0]), int(tdf["ts"].iloc[-1])
    span = max(ts1 - ts0, 1)
    hours = max(span / 3600.0, 1.0 / 120.0)
    out["trades_per_hour"] = float(n / hours)
    sides = {str(x).strip().lower() for x in tdf["taker_side"].dropna().unique()}
    out["two_sided"] = "yes" in sides and "no" in sides

    prices = tdf["yes_price"].astype(float).to_numpy()
    if len(prices) >= 5:
        rets = np.diff(prices)
        den = np.maximum(np.abs(prices[:-1]), 1e-4)
        r = rets / den
        if len(r) >= 4 and float(np.nanstd(r)) > 1e-8:
            rho = float(np.corrcoef(r[:-1], r[1:])[0, 1])
            if not np.isnan(rho):
                out["reversion_01"] = float(np.clip(0.5 * (1.0 - rho), 0.0, 1.0))
            vol = float(np.std(r))
            tgt, sigma_bw = 0.025, 0.03
            out["trade_vol_score"] = float(
                np.clip(np.exp(-((vol - tgt) ** 2) / (2 * sigma_bw**2)), 0.0, 1.0)
            )
    return out


def _minmax_series(s: pd.Series) -> pd.Series:
    lo, hi = float(s.min()), float(s.max())
    if hi <= lo + 1e-12:
        return pd.Series(0.5, index=s.index, dtype=float)
    return ((s - lo) / (hi - lo)).astype(float)


def _sigma_stability_score(sigma: Optional[float]) -> float:
    if sigma is None or not np.isfinite(sigma) or sigma <= 0:
        return 0.5
    tgt, w = 0.35, 0.25
    return float(np.clip(np.exp(-((float(sigma) - tgt) ** 2) / (2 * w**2)), 0.0, 1.0))


def _parse_sample_order_line(line: str) -> Optional[Dict[str, Any]]:
    line = line.strip()
    if not line:
        return None
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    if data.get("model") != "avellaneda_stoikov_reduced_symmetric":
        return None
    return _flatten_sample_record(data)


def _flatten_sample_record(r: Dict[str, Any]) -> Dict[str, Any]:
    """One row for UI: model quotes, book, params, and hypothetical gross edge."""
    orders = r.get("hypothetical_orders_yes_side") or []
    buy = next((o for o in orders if o.get("action") == "post_limit_buy_yes"), {})
    sell = next((o for o in orders if o.get("action") == "post_limit_sell_yes"), {})
    bid_p = buy.get("limit_price_dollars")
    ask_p = sell.get("limit_price_dollars")
    cnt_b = float(buy.get("count") or 0)
    cnt_a = float(sell.get("count") or 0)
    contracts = min(cnt_b, cnt_a) if cnt_b and cnt_a else max(cnt_b, cnt_a)
    spread: Optional[float] = None
    if bid_p is not None and ask_p is not None:
        spread = float(ask_p) - float(bid_p)
    gross = spread * contracts if spread is not None and contracts else None
    p = r.get("params") or {}
    return {
        "cycle_ts_utc": r.get("cycle_ts_utc"),
        "market_ticker": r.get("market_ticker"),
        "computed_mid": r.get("computed_mid"),
        "reservation_yes": r.get("reservation_yes"),
        "half_spread_model": r.get("half_spread_model"),
        "book_yes_bid": r.get("observed_book_yes_bid"),
        "book_yes_ask": r.get("observed_book_yes_ask"),
        "sigma_sqrt_h": r.get("sigma_per_sqrt_hour_est"),
        "gamma": p.get("gamma"),
        "k": p.get("k"),
        "tau_hours": p.get("tau_hours"),
        "inventory_yes": p.get("inventory_yes"),
        "model_bid_yes": bid_p,
        "model_ask_yes": ask_p,
        "contracts_per_leg": cnt_b,
        "spread_dollars": spread,
        "gross_profit_if_both_fill": gross,
    }


def _read_new_lines(path: str, start_pos: int) -> Tuple[List[str], int]:
    with open(path, "r", encoding="utf-8") as f:
        f.seek(start_pos)
        lines = f.readlines()
        new_pos = f.tell()
    return lines, new_pos


def _file_mtime_utc(path: str) -> datetime:
    return datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)


def _status_badge(age_s: float, stale_after_s: float) -> str:
    return "Receiving" if age_s <= stale_after_s else "Stale"


st.set_page_config(page_title="Kalshi Trades Dashboard", layout="wide")

out_dir = os.getenv("KALSHI_WS_OUT_DIR", "data/kalshi/ws")
default_sample_path = os.getenv("KALSHI_AS_SAMPLE_ORDERS", "data/kalshi/as_sample_orders.jsonl")

st.sidebar.header("Data source")
st.sidebar.text(f"Trade/ticker dir: {out_dir}")
if "dash_as_sample_path" not in st.session_state:
    st.session_state.dash_as_sample_path = default_sample_path
st.sidebar.text_input("AS sample orders JSONL", key="dash_as_sample_path")

st.sidebar.slider("Auto-refresh (ms)", min_value=500, max_value=5000, value=2000, step=250, key="dash_refresh_ms")
st.sidebar.slider("Max trades to keep", min_value=50, max_value=2000, value=500, step=50, key="dash_max_trades")
st.sidebar.slider("Max AS sample rows to keep", min_value=20, max_value=500, value=150, step=10, key="dash_max_sample_rows")
st.sidebar.slider("Mark stale after (seconds)", min_value=2, max_value=120, value=10, step=1, key="dash_stale_after_s")

st.sidebar.subheader("Viable markets (MM)")
st.sidebar.slider("Min book spread ($)", min_value=0.0, max_value=0.25, value=0.01, step=0.005, key="dash_mm_min_spread")
st.sidebar.slider("Min trades in window", min_value=0, max_value=200, value=0, step=1, key="dash_mm_min_trades")
st.sidebar.slider("Max rows to show", min_value=10, max_value=200, value=50, step=5, key="dash_mm_top_n")
st.sidebar.slider("Ticker file tail (MB) on new file", min_value=1, max_value=32, value=8, step=1, key="dash_ticker_tail_mb")
st.sidebar.slider(
    "Exclude settled NO (mid <)",
    min_value=0.01,
    max_value=0.30,
    value=0.06,
    step=0.01,
    help="Drop markets whose YES mid is below this (near-certain NO / resolved).",
    key="dash_mm_mid_low",
)
st.sidebar.slider(
    "Exclude settled YES (mid >)",
    min_value=0.70,
    max_value=0.99,
    value=0.94,
    step=0.01,
    help="Drop markets whose YES mid is above this (near-certain YES / resolved).",
    key="dash_mm_mid_high",
)
st.sidebar.slider(
    "Max book spread ($, cap)",
    min_value=0.10,
    max_value=1.0,
    value=1.0,
    step=0.05,
    help="Optional: hide very wide bid/ask (e.g. 0.01 vs 0.99). Default 1.0 = no cap.",
    key="dash_mm_max_book_spread",
)
st.sidebar.slider(
    "Assumed round-trip fee ($)",
    min_value=0.0,
    max_value=0.20,
    value=0.04,
    step=0.005,
    help="Spread edge vs fees (one round-trip, both sides). Tune to your fee tier.",
    key="dash_mm_round_trip_fee",
)
with st.sidebar.expander("MM score weights (normalize to sum)"):
    st.slider("Spread quality", 0, 100, 22, help="Edge vs fees + quoted spread stability", key="dash_mm_w1")
    st.slider("Fill / flow", 0, 100, 22, help="Trade rate and two-sided taker flow", key="dash_mm_w2")
    st.slider("Adverse / reversion", 0, 100, 20, help="Lag-1 return autocorr proxy on YES prints", key="dash_mm_w3")
    st.slider("Volume / depth", 0, 100, 18, help="Contracts, $ volume, OI (logged)", key="dash_mm_w4")
    st.slider("Vol stability", 0, 100, 18, help="Moderate short-horizon vol; optional AS σ", key="dash_mm_w5")

if "trade_file" not in st.session_state:
    st.session_state.trade_file = None
if "trade_pos" not in st.session_state:
    st.session_state.trade_pos = 0
if "trades" not in st.session_state:
    st.session_state.trades = []

if "sample_path" not in st.session_state:
    st.session_state.sample_path = None
if "sample_pos" not in st.session_state:
    st.session_state.sample_pos = 0
if "sample_rows" not in st.session_state:
    st.session_state.sample_rows = []

if "tick_file" not in st.session_state:
    st.session_state.tick_file = None
if "tick_pos" not in st.session_state:
    st.session_state.tick_pos = 0
if "ticker_states" not in st.session_state:
    st.session_state.ticker_states = {}
if "spread_hist" not in st.session_state:
    st.session_state.spread_hist = {}

st.title("Live Kalshi Trades")
with st.expander("How viable market scoring works (data limits)"):
    st.markdown(
        """
        **Proxies from your websocket JSONL only** (not full L2 depth or settlement text):

        - **Spread quality** — mean quoted spread from recent ticker updates vs your **assumed round-trip fee**, plus **inverse CV** of that spread (stable quotes → spread does not collapse every tick).
        - **Fill / flow** — trades per hour in the dashboard window, boosted when both **yes** and **no** taker sides appear (two-sided flow).
        - **Adverse selection / reversion** — lag-1 autocorrelation of short YES returns: **negative** autocorr → higher score (mean-reverting tape in the window).
        - **Volume / depth** — `log1p` mix of contracts traded, dollar volume, and open interest (no order-book depth).
        - **Vol stability** — prefers **moderate** short-horizon trade volatility; blends in **AS σ** when the sample JSONL row exists.

        **Resolution clarity** is not scored (needs REST/event metadata). Rankings are **relative** within the current candidate set (min–max on flow/depth).
        """
    )

st.text_input("Filter market_ticker (substring, empty = all)", key="dash_market_filter")

_dash_refresh_ms = int(st.session_state.get("dash_refresh_ms", 2000))


@st.fragment(run_every=timedelta(milliseconds=max(500, _dash_refresh_ms)))
def _live_dashboard() -> None:
    out_dir = os.getenv("KALSHI_WS_OUT_DIR", "data/kalshi/ws")
    trade_pattern = os.path.join(out_dir, "trade_stream_*.jsonl")
    tick_pattern = os.path.join(out_dir, "ticker_stream_*.jsonl")
    sample_orders_path = str(st.session_state.get("dash_as_sample_path") or "").strip()
    max_trades = int(st.session_state.dash_max_trades)
    max_sample_rows = int(st.session_state.dash_max_sample_rows)
    stale_after_s = int(st.session_state.dash_stale_after_s)
    min_book_spread = float(st.session_state.dash_mm_min_spread)
    min_recent_trades = int(st.session_state.dash_mm_min_trades)
    viable_top_n = int(st.session_state.dash_mm_top_n)
    ticker_tail_mb = int(st.session_state.dash_ticker_tail_mb)
    mm_mid_low = float(st.session_state.dash_mm_mid_low)
    mm_mid_high = float(st.session_state.dash_mm_mid_high)
    mm_max_book_spread = float(st.session_state.dash_mm_max_book_spread)
    mm_round_trip_fee = float(st.session_state.dash_mm_round_trip_fee)
    _wa = np.array(
        [
            float(st.session_state.get("dash_mm_w1", 22)),
            float(st.session_state.get("dash_mm_w2", 22)),
            float(st.session_state.get("dash_mm_w3", 20)),
            float(st.session_state.get("dash_mm_w4", 18)),
            float(st.session_state.get("dash_mm_w5", 18)),
        ],
        dtype=float,
    )
    _ws = float(np.sum(_wa))
    mm_weights = (_wa / _ws) if _ws >= 1e-6 else (np.ones(5, dtype=float) / 5.0)

    latest_trade_file = _latest_file(trade_pattern)
    latest_tick_file = _latest_file(tick_pattern)

    if not latest_trade_file:
        st.error(f"No `trade_stream_*.jsonl` files found under: {out_dir}")
        st.info(
            "Start the websocket (or `kalshi_as` with `--sample-contracts`) so trades are recorded. "
            "Sample orders below may still work if that file exists."
        )
    else:
        _bits = [f"trades `{os.path.basename(latest_trade_file)}`"]
        if latest_tick_file:
            _bits.append(f"ticker `{os.path.basename(latest_tick_file)}`")
        st.caption(" · ".join(_bits))
    if not latest_tick_file:
        st.warning("No `ticker_stream_*.jsonl` found (viable-markets book data will be empty until the ticker stream writes).")

    lines: List[str] = []
    if latest_trade_file:
        if st.session_state.trade_file != latest_trade_file:
            st.session_state.trade_file = latest_trade_file
            st.session_state.trade_pos = 0
            st.session_state.trades = []
        lines, new_pos = _read_new_lines(latest_trade_file, st.session_state.trade_pos)
        st.session_state.trade_pos = new_pos
        if lines:
            for line in lines:
                parsed = _parse_trade_line(line)
                if parsed is None:
                    continue
                st.session_state.trades.append(parsed)
            if len(st.session_state.trades) > max_trades:
                st.session_state.trades = st.session_state.trades[-max_trades:]

    # ── Ticker snapshots (last known book per market) ─────────────────────
    tick_lines: List[str] = []
    if latest_tick_file:
        tail_b = max(1, int(ticker_tail_mb)) * 1024 * 1024
        if st.session_state.tick_file != latest_tick_file:
            st.session_state.tick_file = latest_tick_file
            raw_pos = _tail_seek_bytes(latest_tick_file, tail_b)
            st.session_state.tick_pos = (
                _seek_after_next_newline(latest_tick_file, raw_pos) if raw_pos > 0 else 0
            )
            st.session_state.ticker_states = {}
            st.session_state.spread_hist = {}
        tick_lines, new_tick_pos = _read_new_lines(latest_tick_file, st.session_state.tick_pos)
        st.session_state.tick_pos = new_tick_pos
        for line in tick_lines:
            msg = _parse_ticker_line(line)
            if msg is None:
                continue
            _apply_ticker_msg(st.session_state.ticker_states, msg)
            _record_spread_after_ticker_update(
                st.session_state.ticker_states,
                st.session_state.spread_hist,
                msg,
            )
    else:
        st.session_state.tick_file = None
        st.session_state.tick_pos = 0
        st.session_state.ticker_states = {}
        st.session_state.spread_hist = {}

    df = pd.DataFrame(st.session_state.trades)

    # ── Sample orders (Avellaneda–Stoikov JSONL) ───────────────────────────
    sample_path_clean = (sample_orders_path or "").strip()
    if sample_path_clean and os.path.isfile(sample_path_clean):
        if st.session_state.sample_path != sample_path_clean:
            st.session_state.sample_path = sample_path_clean
            st.session_state.sample_pos = 0
            st.session_state.sample_rows = []
        slines, spos = _read_new_lines(sample_path_clean, st.session_state.sample_pos)
        st.session_state.sample_pos = spos
        for line in slines:
            row = _parse_sample_order_line(line)
            if row is None:
                continue
            st.session_state.sample_rows.append(row)
        if len(st.session_state.sample_rows) > max_sample_rows:
            st.session_state.sample_rows = st.session_state.sample_rows[-max_sample_rows:]
    elif sample_path_clean:
        st.session_state.sample_path = None
        st.session_state.sample_pos = 0
        st.session_state.sample_rows = []

    df_samples = pd.DataFrame(st.session_state.sample_rows)

    st.subheader("WebSocket status")
    now = datetime.now(timezone.utc)
    
    if latest_trade_file:
        trade_mtime = _file_mtime_utc(latest_trade_file)
        trade_age_s = (now - trade_mtime).total_seconds()
        trade_status = _status_badge(trade_age_s, stale_after_s)
    else:
        trade_status = "N/A"
        trade_age_s = None
    
    tick_status = "N/A"
    tick_age_s: Optional[float] = None
    if latest_tick_file:
        tick_mtime = _file_mtime_utc(latest_tick_file)
        tick_age_s = (now - tick_mtime).total_seconds()
        tick_status = _status_badge(tick_age_s, stale_after_s)
    
    ws1, ws2, ws3, ws4 = st.columns(4)
    ws1.metric("Trade stream", trade_status, f"{trade_age_s:.1f}s since write" if trade_age_s is not None else "")
    ws2.metric("Ticker stream", tick_status, f"{tick_age_s:.1f}s since write" if tick_age_s is not None else "")
    ws3.metric("Trade file(os)", os.path.basename(latest_trade_file) if latest_trade_file else "—")
    ws4.metric("Output dir", out_dir)
    
    st.subheader("Viable markets for market making")

    rows_mm: List[Dict[str, Any]] = []
    for tkr, mt in st.session_state.ticker_states.items():
        if mt.yes_bid <= 0 or mt.yes_ask <= 0 or mt.yes_ask <= mt.yes_bid:
            continue
        mid = 0.5 * (mt.yes_bid + mt.yes_ask)
        rows_mm.append(
            {
                "market_ticker": tkr,
                "yes_bid": mt.yes_bid,
                "yes_ask": mt.yes_ask,
                "book_spread_dollars": round(mt.spread, 6),
                "quoted_mid": round(mid, 6),
                "volume_24h_fp": mt.volume,
                "dollar_volume": int(mt.dollar_volume),
                "open_interest_fp": mt.open_interest,
                "dollar_open_interest": int(mt.dollar_open_interest),
                "last_price_dollars": mt.last_price,
            }
        )
    vdf_mm = pd.DataFrame(rows_mm)
    if len(vdf_mm) == 0:
        st.info(
            "No two-sided **ticker** snapshots loaded yet. Run the Kalshi websocket so `ticker_stream_*.jsonl` is written under the output dir, "
            "then refresh — book columns come from the ticker channel."
        )
    else:
        if len(df) > 0 and "market_ticker" in df.columns:
            tg = df.groupby("market_ticker", as_index=False).agg(
                recent_trade_count=("size", "count"),
                contracts_traded=("size", "sum"),
            )
            vdf_mm = vdf_mm.merge(tg, on="market_ticker", how="left")
        else:
            vdf_mm["recent_trade_count"] = 0
            vdf_mm["contracts_traded"] = np.nan
        vdf_mm["recent_trade_count"] = vdf_mm["recent_trade_count"].fillna(0).astype(int)
        vdf_mm["contracts_traded"] = vdf_mm["contracts_traded"].fillna(0.0)
    
        if len(df_samples) > 0 and "market_ticker" in df_samples.columns and "cycle_ts_utc" in df_samples.columns:
            samp = df_samples.sort_values("cycle_ts_utc").groupby("market_ticker", as_index=False).last()
            merge_cols = ["market_ticker"]
            for c in ("sigma_sqrt_h", "half_spread_model", "gross_profit_if_both_fill", "spread_dollars"):
                if c in samp.columns:
                    merge_cols.append(c)
            if len(merge_cols) > 1:
                samp_small = samp[merge_cols].copy()
                if "spread_dollars" in samp_small.columns:
                    samp_small = samp_small.rename(columns={"spread_dollars": "model_bid_ask_spread_as"})
                vdf_mm = vdf_mm.merge(samp_small, on="market_ticker", how="left")
    
        spread_hist: Dict[str, deque] = st.session_state.spread_hist
        score_parts: List[Dict[str, Any]] = []
        for _, row in vdf_mm.iterrows():
            tkr = str(row["market_ticker"])
            sp_list = _spread_hist_values(spread_hist, tkr)
            mean_sp, stab = _spread_mean_and_stability(sp_list, float(row["book_spread_dollars"]))
            edge = mean_sp - float(mm_round_trip_fee)
            edge_norm = float(np.clip(edge / 0.10, 0.0, 1.0))
            spread_pillar = 0.55 * edge_norm + 0.45 * stab
    
            if len(df) > 0 and "market_ticker" in df.columns:
                tdf_m = df[df["market_ticker"] == tkr]
            else:
                tdf_m = pd.DataFrame()
            tm = _trade_microstructure(tdf_m)
    
            depth_raw = float(
                np.log1p(max(float(row.get("contracts_traded", 0)), 0.0))
                + np.log1p(max(float(row.get("dollar_volume", 0)), 0.0) + 1.0)
                + np.log1p(max(float(row.get("open_interest_fp", 0)), 0.0) + 1.0)
            )
            two_boost = 1.0 if tm["two_sided"] else 0.35
            fill_raw = float(np.log1p(max(tm["trades_per_hour"], 0.0)) * (0.45 + 0.55 * two_boost))
    
            sg = row["sigma_sqrt_h"] if "sigma_sqrt_h" in row.index else np.nan
            vol_p = 0.7 * float(tm["trade_vol_score"]) + 0.3 * _sigma_stability_score(
                float(sg) if pd.notna(sg) else None
            )
    
            score_parts.append(
                {
                    "market_ticker": tkr,
                    "mean_book_spread": round(mean_sp, 4),
                    "spread_stability_01": round(stab, 3),
                    "spread_edge_norm": round(edge_norm, 3),
                    "spread_pillar": float(spread_pillar),
                    "trades_per_hour": round(float(tm["trades_per_hour"]), 2),
                    "fill_raw": fill_raw,
                    "reversion_01": float(tm["reversion_01"]),
                    "depth_raw": depth_raw,
                    "vol_pillar": float(vol_p),
                    "two_sided_flow": bool(tm["two_sided"]),
                }
            )
        spm = pd.DataFrame(score_parts)
        vdf_mm = vdf_mm.merge(spm, on="market_ticker", how="left")
        vdf_mm["fill_pillar"] = _minmax_series(vdf_mm["fill_raw"])
        vdf_mm["depth_pillar"] = _minmax_series(vdf_mm["depth_raw"])
        vdf_mm["mm_score"] = (
            mm_weights[0] * vdf_mm["spread_pillar"].astype(float)
            + mm_weights[1] * vdf_mm["fill_pillar"].astype(float)
            + mm_weights[2] * vdf_mm["reversion_01"].astype(float)
            + mm_weights[3] * vdf_mm["depth_pillar"].astype(float)
            + mm_weights[4] * vdf_mm["vol_pillar"].astype(float)
        ) * 100.0
    
        low = float(min(mm_mid_low, mm_mid_high))
        high = float(max(mm_mid_low, mm_mid_high))
        vmask = (
            (vdf_mm["book_spread_dollars"] >= min_book_spread)
            & (vdf_mm["recent_trade_count"] >= min_recent_trades)
            & (vdf_mm["quoted_mid"] >= low)
            & (vdf_mm["quoted_mid"] <= high)
        )
        if mm_max_book_spread < 0.999:
            vmask = vmask & (vdf_mm["book_spread_dollars"] <= float(mm_max_book_spread))
        vshow = (
            vdf_mm[vmask]
            .sort_values("mm_score", ascending=False)
            .head(int(viable_top_n))
            .reset_index(drop=True)
        )
    
        mm_cols = [
            "mm_score",
            "spread_pillar",
            "fill_pillar",
            "reversion_01",
            "depth_pillar",
            "vol_pillar",
            "market_ticker",
            "mean_book_spread",
            "book_spread_dollars",
            "spread_stability_01",
            "spread_edge_norm",
            "recent_trade_count",
            "trades_per_hour",
            "two_sided_flow",
            "contracts_traded",
            "yes_bid",
            "yes_ask",
            "quoted_mid",
            "dollar_volume",
            "open_interest_fp",
            "sigma_sqrt_h",
            "half_spread_model",
            "model_bid_ask_spread_as",
            "gross_profit_if_both_fill",
        ]
        mm_cols = [c for c in mm_cols if c in vshow.columns]
        if len(vshow) == 0:
            st.warning("No markets pass the sidebar filters (min spread / min trades). Loosen filters to see candidates.")
        else:
            v_disp = vshow[mm_cols].copy()
            for _c in ("mm_score", "spread_pillar", "fill_pillar", "depth_pillar", "vol_pillar", "reversion_01"):
                if _c in v_disp.columns:
                    v_disp[_c] = v_disp[_c].round(3)
            st.dataframe(v_disp, use_container_width=True, hide_index=True)
    
    st.subheader("Avellaneda–Stoikov sample orders (hypothetical)")
    st.caption(
        "`gross_profit_if_both_fill` = (model ask − model bid) × contracts if both resting legs fill at those limits. "
        "Excludes Kalshi fees, partial fills, adverse selection, and inventory risk — not realized P&L."
    )
    if not sample_path_clean:
        st.info("Set **AS sample orders JSONL** in the sidebar (e.g. `data/kalshi/as_sample_orders.jsonl`).")
    elif not os.path.isfile(sample_path_clean):
        st.warning(f"File not found: `{sample_path_clean}`. Run `python -m kalshi_as --sample-contracts …` to create it.")
    elif len(df_samples) == 0:
        st.warning("No sample-order rows loaded yet. Wait for `kalshi_as` to append cycles, or check the file path.")
    else:
        sdf = df_samples.sort_values("cycle_ts_utc", ascending=False).head(200)
        show_cols = [
            "cycle_ts_utc",
            "market_ticker",
            "book_yes_bid",
            "book_yes_ask",
            "computed_mid",
            "reservation_yes",
            "half_spread_model",
            "model_bid_yes",
            "model_ask_yes",
            "spread_dollars",
            "contracts_per_leg",
            "gross_profit_if_both_fill",
            "sigma_sqrt_h",
            "gamma",
            "k",
            "tau_hours",
            "inventory_yes",
        ]
        show_cols = [c for c in show_cols if c in sdf.columns]
        st.dataframe(sdf[show_cols], use_container_width=True, hide_index=True)
    
    st.divider()
    
    col1, col2, col3 = st.columns(3)
    col1.metric("Trades loaded", len(df))
    if len(df) > 0 and df["time_utc"].notna().any():
        col2.metric("Last trade", df["time_utc"].dropna().iloc[-1])
    else:
        col2.metric("Last trade", "N/A")
    col3.metric("Trades (new lines this tick)", len(lines) if latest_trade_file else 0)
    
    if len(df) == 0:
        st.warning("No exchange trades in memory yet — ticker/trade JSONL may still be writing; sample orders above are independent.")
    else:
        st.subheader("Top markets (by recent trade count)")
        top_markets = (
            df["market_ticker"]
            .value_counts()
            .head(10)
            .reset_index()
            .rename(columns={"index": "market_ticker", 0: "count"})
        )
        st.dataframe(top_markets, use_container_width=True)
    
        st.subheader("Recent trades")
        market_filter = str(st.session_state.get("dash_market_filter") or "")
        df_view = df
        if market_filter.strip():
            df_view = df_view[df_view["market_ticker"].str.contains(market_filter.strip(), na=False)]
        df_view = df_view.sort_values("ts", ascending=False).head(200)
        display_cols = ["time_utc", "market_ticker", "taker_side", "yes_price", "no_price", "size", "trade_id"]
        st.dataframe(df_view[display_cols], use_container_width=True, hide_index=True)


_live_dashboard()
