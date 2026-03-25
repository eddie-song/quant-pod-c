# quant-pod-c: Kalshi data ingestion

A small tool to download Kalshi market and trade data to your machine for analysis. Run from the `quant-pod-c` folder.

## Setup

```bash
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and set:

- `KALSHI_API_KEY_ID` — your API key from Kalshi
- `KALSHI_PRIVATE_KEY_PATH` — path to your downloaded `.key` file
- `KALSHI_BASE_URL` — demo or prod (defaults to demo)

## Commands

Outputs go to `--out-dir` (default `data/kalshi`):

- **Raw** API pages → `*.jsonl`
- **Flat** tables → `*.csv` (for EDA)

### Download markets

```bash
python -m kalshi_ingest markets --out-dir data/kalshi
```

With filters:

```bash
python -m kalshi_ingest markets --status open --limit 500 --out-dir data/kalshi
```

### Download trades

All trades (no filter):

```bash
python -m kalshi_ingest trades --out-dir data/kalshi
```

Trades for one market (use exact ticker from API):

```bash
python -m kalshi_ingest trades --ticker KXNCAAMBGAME-26MAR10IDHOEWU-EWU --out-dir data/kalshi
```

**Important:** Use the **exact** market ticker from the API: **uppercase** and **include the suffix** (e.g. `-EWU`). If you use the wrong format, you get no rows. To see the correct ticker format, run:

```bash
python -m kalshi_ingest trades-sample --limit 100
```

That prints recent tickers as returned by the API so you can copy the right one.

Optional: limit by time (Unix seconds):

```bash
python -m kalshi_ingest trades --ticker KXNCAAMBGAME-26MAR10IDHOEWU-EWU --min-ts 1700000000 --max-ts 1710000000 --out-dir data/kalshi
```

### Check that the API returns trades

```bash
python -m kalshi_ingest trades-sample --limit 100
```

Shows how many trades came back and sample tickers. Use this to confirm your env and to get the exact ticker string for a market.

### Download orderbooks

Orderbook for one market (full depth):

```bash
python -m kalshi_ingest orderbook --tickers KXNCAAMBGAME-26MAR10IDHOEWU-EWU --out-dir data/kalshi
```

Multiple markets at once (comma-separated):

```bash
python -m kalshi_ingest orderbook --tickers TICKER1,TICKER2,TICKER3 --out-dir data/kalshi
```

Limit to 10 price levels per side:

```bash
python -m kalshi_ingest orderbook --tickers TICKER1,TICKER2 --depth 10 --out-dir data/kalshi
```

The flat CSV has one row per price level with columns: `fetched_at`, `ticker`, `side`, `price_cents`, `quantity`, `level_index`. If a ticker fails (e.g. invalid ticker), it is skipped and the rest continue.

**Useful options:**

- `--env-file PATH` — use a specific `.env` file
- `--out-dir PATH` — where to save files
- `trades`: `--ticker`, `--min-ts`, `--max-ts`, `--limit`
- `markets`: `--status`, `--series-ticker`, `--event-ticker`, `--tickers`, `--limit`
- `orderbook`: `--tickers` (required, comma-separated), `--depth`
