## Project overview

`quant-pod-c` is a Kalshi market-making system that automatically identifies prediction markets where posting bids and asks inside the spread is profitable, and avoids markets where informed traders would pick us off. Pure microstructure — no opinions about event outcomes.

**What exists today:**

| Package | Purpose | Runtime |
|---------|---------|---------|
| `kalshi_ingest/` | Batch REST data ingestion (markets, trades, orderbooks) | Sync |
| `kalshi_ws/` | Live WebSocket stream (all markets, all trades) | Async |
| `kalshi_filter/` | Tier 1 filter, config system, metadata bootstrap, orchestrator | Async |

**What's next:** Orderbook subscriber, Tier 2 analysis, quoting engine, execution, risk management.

## Folder layout

| Path | Purpose |
|------|--------|
| `requirements.txt` | Python dependencies (requests, python-dotenv, cryptography, pandas, websockets) |
| `.env.example` | Template for API key and key file path |
| `config.json` | All system parameters — auto-generated with defaults on first run |
| `kalshi_ingest/` | REST client: auth, pagination, market/trade/orderbook download, CLI |
| `kalshi_ws/` | WebSocket client: connection, subscription, in-memory state, raw persistence |
| `kalshi_filter/` | Config loader, metadata bootstrap, Tier 1 filter, transition logger, orchestrator |
| `tests/` | Unit tests for data models (8) and filter logic (26) |
| `docs/` | This doc and the function reference |
| `data/` | Output directory for all data files (gitignored) |

## How the pieces fit together

```
┌─────────────────────────────────────────────────────────┐
│  kalshi_filter/__main__.py  (orchestrator)              │
│                                                         │
│  1. Load config.json → typed Config object              │
│  2. REST bootstrap → Dict[str, MarketMetadata]          │
│  3. Start kalshi_ws stream (background async task)      │
│  4. Start metadata refresh loop (every 5 min)           │
│  5. Start evaluation loop (every 60s)                   │
│     → reads market_states + trade_buffers from kalshi_ws│
│     → reads metadata from kalshi_filter/metadata        │
│     → evaluates 7 checks per market                     │
│     → promotes/demotes, logs transitions                │
└─────────────────────────────────────────────────────────┘
```

1. **auth** (`kalshi_ingest/`) — Reads `.env`, loads your private key, signs each request.
2. **client** (`kalshi_ingest/`) — Sends signed GET requests and handles pagination.
3. **ingest** (`kalshi_ingest/`) — Uses the client to fetch markets, trades, or orderbooks, writes JSONL and CSV.
4. **cli** (`kalshi_ingest/`) — Parses your command, loads env, runs ingest, prints file paths.
5. **stream** (`kalshi_ws/`) — Connects to WebSocket, subscribes to ticker + trade channels, maintains live state in memory, persists raw messages to daily JSONL files.
6. **config** (`kalshi_filter/`) — Loads all thresholds from JSON into typed dataclasses.
7. **metadata** (`kalshi_filter/`) — Pulls market metadata from REST API, refreshes periodically.
8. **filter** (`kalshi_filter/`) — Evaluates every market against Tier 1 criteria, manages lifecycle (IGNORED → WATCHING → DEMOTED → BLACKLISTED).
9. **transitions** (`kalshi_filter/`) — Logs every status change to console and JSONL.
10. **orchestrator** (`kalshi_filter/`) — Starts everything, runs forever, handles graceful shutdown.

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

**Full system (recommended):**
```bash
python3 -m kalshi_filter
```
Starts WebSocket stream + Tier 1 filter. Markets that pass all checks for 5 consecutive evaluations get promoted to WATCHING. All transitions logged to console and `data/kalshi/transitions/`.

**WebSocket stream only:**
```bash
python3 -m kalshi_ws
```
Streams data to memory and disk without filtering.

**Batch REST ingestion:**
```bash
python3 -m kalshi_ingest markets --status open --out-dir data/kalshi
python3 -m kalshi_ingest trades --ticker EXACT-TICKER-HERE --out-dir data/kalshi
python3 -m kalshi_ingest orderbook --tickers TICK1,TICK2 --depth 10 --out-dir data/kalshi
python3 -m kalshi_ingest trades-sample --limit 100
```

**Run tests:**
```bash
python3 -m pytest tests/ -v
```
34 tests verify parsing, filtering, config, lifecycle, and logging without needing API credentials.

## Useful options

- `--env-file PATH` — use a specific `.env` file
- `--out-dir PATH` — where to save files
- `trades`: `--ticker`, `--min-ts`, `--max-ts`, `--limit`
- `markets`: `--status`, `--series-ticker`, `--event-ticker`, `--tickers`, `--limit`
- `orderbook`: `--tickers` (required, comma-separated), `--depth`
- `KALSHI_CONFIG_PATH` env var — custom config file path for kalshi_filter
