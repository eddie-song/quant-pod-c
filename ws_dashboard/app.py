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
    # Pick newest by filesystem mtime.
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


def _read_new_lines(path: str, start_pos: int) -> Tuple[List[str], int]:
    # Reads from file offset `start_pos` and returns (lines, new_pos).
    # This avoids rereading the entire file on each refresh.
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
st.sidebar.header("Data source")
st.sidebar.text(f"Output dir: {out_dir}")

trade_pattern = os.path.join(out_dir, "trade_stream_*.jsonl")
tick_pattern = os.path.join(out_dir, "ticker_stream_*.jsonl")

refresh_ms = st.sidebar.slider("Auto-refresh (ms)", min_value=500, max_value=5000, value=2000, step=250)
max_trades = st.sidebar.slider("Max trades to keep", min_value=50, max_value=2000, value=500, step=50)
stale_after_s = st.sidebar.slider("Mark stale after (seconds)", min_value=2, max_value=120, value=10, step=1)

latest_trade_file = _latest_file(trade_pattern)
if not latest_trade_file:
    st.error(f"No `trade_stream_*.jsonl` files found under: {out_dir}")
    st.info("Start the websocket first, then rerun/reload this dashboard.")
    st.stop()

st.sidebar.markdown(f"**Latest file**: `{os.path.basename(latest_trade_file)}`")
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

# Force periodic reruns.
st_autorefresh(interval=refresh_ms, limit=None, key="trade_autorefresh")

# Reset offsets/trades if the file rotated (new day).
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
    # Keep only the most recent trades.
    if len(st.session_state.trades) > max_trades:
        st.session_state.trades = st.session_state.trades[-max_trades:]

df = pd.DataFrame(st.session_state.trades)

st.title("Live Kalshi Trades")

st.subheader("WebSocket status")
now = datetime.now(timezone.utc)

trade_mtime = _file_mtime_utc(latest_trade_file)
trade_age_s = (now - trade_mtime).total_seconds()
trade_status = _status_badge(trade_age_s, stale_after_s)

tick_status = "N/A"
tick_age_s: Optional[float] = None
if latest_tick_file:
    tick_mtime = _file_mtime_utc(latest_tick_file)
    tick_age_s = (now - tick_mtime).total_seconds()
    tick_status = _status_badge(tick_age_s, stale_after_s)

ws1, ws2, ws3, ws4 = st.columns(4)
ws1.metric("Trade stream", trade_status, f"{trade_age_s:.1f}s since write")
ws2.metric("Ticker stream", tick_status, f"{tick_age_s:.1f}s since write" if tick_age_s is not None else "")
ws3.metric("Trade file", os.path.basename(latest_trade_file))
ws4.metric("Output dir", out_dir)

col1, col2, col3 = st.columns(3)
col1.metric("Trades loaded", len(df))
if len(df) > 0 and df["time_utc"].notna().any():
    col2.metric("Last trade", df["time_utc"].dropna().iloc[-1])
else:
    col2.metric("Last trade", "N/A")
col3.metric("Trades since load", len(lines))

if len(df) == 0:
    st.warning("Waiting for trade messages...")
    st.stop()

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

# Show the most recent rows first.
df_view = df_view.sort_values("ts", ascending=False).head(200)

display_cols = ["time_utc", "market_ticker", "taker_side", "yes_price", "no_price", "size", "trade_id"]
df_view = df_view[display_cols]
st.dataframe(df_view, use_container_width=True, hide_index=True)

