## Project overview

`quant-pod-c` downloads Kalshi market and trade data to your machine for analysis, and can stream live ticker and trade data over the WebSocket.

**What it does:**
- Calls `GET /markets` to download market metadata.
- Calls `GET /markets/trades` to download executed trades.
- Calls `GET /markets/{ticker}/orderbook` to download orderbook snapshots.
- Optionally runs `kalshi_ws` to connect to Kalshi’s WebSocket (`ticker` + `trade` channels) and append raw messages to daily JSONL files under `data/kalshi/ws/` by default.
- Optionally runs `ws_dashboard` (Streamlit) to view live trades and a simple connection health indicator from those JSONL files.

**Where data goes:** A folder you choose (default `data/kalshi/` for REST). WebSocket streams default to `data/kalshi/ws/` (`ticker_stream_*.jsonl`, `trade_stream_*.jsonl`). REST output is raw API pages in `*.jsonl` and flat tables in `*.csv`.

## Folder layout

| Path | Purpose |
|------|--------|
| `requirements.txt` | Python dependencies |
| `.env.example` | Template for API key and key file path |
| `kalshi_ingest/` | Code: auth, client, ingest, CLI |
| `kalshi_ws/` | Async WebSocket client; ticker + trade streams; JSONL persistence |
| `ws_dashboard/` | Streamlit UI for live trades (reads JSONL written by `kalshi_ws`) |
| `docs/` | This doc and the function reference |

## How the pieces fit together

1. **auth** — Reads `.env`, loads your private key, signs each request.
2. **client** — Sends signed GET requests and handles pagination (cursor).
3. **ingest** — Uses the client to fetch markets, trades, or orderbooks, then writes JSONL and CSV.
4. **cli** — Parses your command, loads env, runs the right ingest and prints where files were saved.

## Setup

In `quant-pod-c/`:

```bash
pip install -r requirements.txt
```

Create `.env` in the **repository root** (copy from `.env.example`) and set:
- `KALSHI_API_KEY_ID`
- `KALSHI_PRIVATE_KEY_PATH`
- `KALSHI_BASE_URL` (demo or prod)

`kalshi_ws` loads `.env` from the **current working directory**; run it from the project root so the same `.env` is used. For `kalshi_ingest`, you can pass `--env-file` to a different path if needed. See the main `README.md` for WebSocket env vars (`KALSHI_WS_*`) and the Streamlit dashboard.

## Run

From `quant-pod-c/`:

**Markets:**
```bash
python -m kalshi_ingest markets --out-dir data/kalshi
```

**Trades for a specific market:**  
Use the **exact** ticker from the API: **uppercase** and **include the suffix** (e.g. `-EWU`). Wrong format = no data.

```bash
python -m kalshi_ingest trades --ticker KXNCAAMBGAME-26MAR10IDHOEWU-EWU --out-dir data/kalshi
```

To see the correct ticker format, run:

```bash
python -m kalshi_ingest trades-sample --limit 100
```

That prints sample tickers from the API so you can copy the right one.

**All trades (no ticker filter):**
```bash
python -m kalshi_ingest trades --out-dir data/kalshi
```

**Orderbook for multiple markets (10 levels deep):**
```bash
python -m kalshi_ingest orderbook --tickers TICKER1,TICKER2 --depth 10 --out-dir data/kalshi
```

**Useful options:**
- `--env-file PATH` — use a specific `.env` file
- `--out-dir PATH` — where to save files
- `trades`: `--ticker`, `--min-ts`, `--max-ts`, `--limit`
- `markets`: `--status`, `--series-ticker`, `--event-ticker`, `--tickers`, `--limit`
- `orderbook`: `--tickers` (required, comma-separated), `--depth`

## WebSocket and dashboard

From the repository root, after installing dependencies:

```bash
python -m kalshi_ws
```

This writes `ticker_stream_*.jsonl` and `trade_stream_*.jsonl` under `KALSHI_WS_OUT_DIR` (default `data/kalshi/ws`). Stop with `Ctrl+C`.

Optional live trades UI:

```bash
streamlit run ws_dashboard/app.py
```

Stop Streamlit with `Ctrl+C`. The dashboard reads the same JSONL files; keep `kalshi_ws` running in another terminal for live updates. See `README.md` for full details.

