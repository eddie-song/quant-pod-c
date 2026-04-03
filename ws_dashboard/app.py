import glob
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Repo root must be on sys.path so `kalshi_ws` imports work when Streamlit runs
# from another cwd or via file:// launcher.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

from kalshi_ws.models import Trade


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
sample_orders_path = st.sidebar.text_input("AS sample orders JSONL", value=default_sample_path)

trade_pattern = os.path.join(out_dir, "trade_stream_*.jsonl")
tick_pattern = os.path.join(out_dir, "ticker_stream_*.jsonl")

refresh_ms = st.sidebar.slider("Auto-refresh (ms)", min_value=500, max_value=5000, value=2000, step=250)
max_trades = st.sidebar.slider("Max trades to keep", min_value=50, max_value=2000, value=500, step=50)
max_sample_rows = st.sidebar.slider("Max AS sample rows to keep", min_value=20, max_value=500, value=150, step=10)
stale_after_s = st.sidebar.slider("Mark stale after (seconds)", min_value=2, max_value=120, value=10, step=1)

latest_trade_file = _latest_file(trade_pattern)
if not latest_trade_file:
    st.error(f"No `trade_stream_*.jsonl` files found under: {out_dir}")
    st.info("Start the websocket (or `kalshi_as` with `--sample-contracts`) so trades are recorded. Sample orders below may still work if that file exists.")
else:
    st.sidebar.markdown(f"**Latest trade file**: `{os.path.basename(latest_trade_file)}`")

latest_tick_file = _latest_file(tick_pattern)
if latest_tick_file:
    st.sidebar.markdown(f"**Latest ticker file**: `{os.path.basename(latest_tick_file)}`")
else:
    st.sidebar.warning("No `ticker_stream_*.jsonl` found (ticker status will be unavailable).")

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

st_autorefresh(interval=refresh_ms, limit=None, key="trade_autorefresh")

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

st.title("Live Kalshi Trades")

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
    market_filter = st.text_input("Filter market_ticker (substring, empty = all)", value="")
    df_view = df
    if market_filter.strip():
        df_view = df_view[df_view["market_ticker"].str.contains(market_filter.strip(), na=False)]
    df_view = df_view.sort_values("ts", ascending=False).head(200)
    display_cols = ["time_utc", "market_ticker", "taker_side", "yes_price", "no_price", "size", "trade_id"]
    st.dataframe(df_view[display_cols], use_container_width=True, hide_index=True)
