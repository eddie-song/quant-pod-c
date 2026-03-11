## Project overview

`quant-pod-c` downloads Kalshi market and trade data to your machine for analysis.

**What it does:**
- Calls `GET /markets` to download market metadata.
- Calls `GET /markets/trades` to download executed trades.

**Where data goes:** A folder you choose (default `data/kalshi/`). You get raw API pages in `*.jsonl` and flat tables in `*.csv`.

## Folder layout

| Path | Purpose |
|------|--------|
| `requirements.txt` | Python dependencies |
| `.env.example` | Template for API key and key file path |
| `kalshi_ingest/` | Code: auth, client, ingest, CLI |
| `docs/` | This doc and the function reference |

## How the pieces fit together

1. **auth** — Reads `.env`, loads your private key, signs each request.
2. **client** — Sends signed GET requests and handles pagination (cursor).
3. **ingest** — Uses the client to fetch markets or trades, then writes JSONL and CSV.
4. **cli** — Parses your command, loads env, runs the right ingest and prints where files were saved.

## Setup

In `quant-pod-c/`:

```bash
pip install -r requirements.txt
```

Create `.env` (copy from `.env.example`) and set:
- `KALSHI_API_KEY_ID`
- `KALSHI_PRIVATE_KEY_PATH`
- `KALSHI_BASE_URL` (demo or prod)

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

**Useful options:**
- `--env-file PATH` — use a specific `.env` file
- `--out-dir PATH` — where to save files
- `trades`: `--ticker`, `--min-ts`, `--max-ts`, `--limit`
- `markets`: `--status`, `--series-ticker`, `--event-ticker`, `--tickers`, `--limit`

