# quant-pod-c: Kalshi Market-Making System

An automated market-making system for Kalshi prediction markets. The system connects to every active market on the platform, computes metrics that distinguish toxic (informed) order flow from recreational flow, and only posts quotes in markets where the data says we have an edge. There are no opinions about event outcomes. The entire strategy is microstructure: find wide spreads, confirm the flow is safe, and sit inside the spread collecting the difference.

The core idea: penny-jump the best bid and ask (bid = best\_bid + 1¢, ask = best\_ask − 1¢) in markets where the spread is wide enough and the counterparties are unsophisticated. Avoid any market where informed traders would pick us off before we can react.

## Strategy Summary

1. **Connect** to Kalshi's WebSocket and receive real-time ticker and trade data for all active markets.
2. **Tier 1 filter** uses spread width, volume, update activity, and trade-side imbalance to find candidate markets worth investigating further.
3. **Candidates get promoted to Tier 2**, where we subscribe to full orderbook depth data for those specific markets.
4. **Tier 2 analysis** computes flow toxicity (do prices move against us after trades?), fill probability, mid-price autocorrelation, stationarity, and effective edge (spread captured minus adverse selection cost).
5. **Markets that pass** start getting quoted with penny-jump pricing, sized according to metric confidence and inventory.
6. **Continuous monitoring** demotes markets if metrics deteriorate or P&L bleeds past limits.
7. **Kill switches** at per-market and global level halt quoting immediately when hard limits are breached.

## Architecture

```
REST Bootstrap (kalshi_ingest/ → kalshi_filter/metadata)  ✅ BUILT
    GET /markets → initial market universe + metadata
    Dict[str, MarketMetadata] refreshed every 5 min
            │
            ▼
WebSocket Connection (kalshi_ws/)                          ✅ BUILT
    ticker channel → all markets top-of-book
    trade channel  → all market trades
            │
            ▼
In-Memory State                                            ✅ BUILT
    market_states: Dict[str, MarketTicker]
    trade_buffers: Dict[str, deque[Trade]]
            │
            ▼
Configuration System (kalshi_filter/config)                ✅ BUILT
    JSON config → typed dataclasses
    all thresholds externalized
            │
            ▼
Tier 1 Filter (kalshi_filter/filter)                       ✅ BUILT
    spread, volume, activity, imbalance, expiry, decided
    IGNORED → WATCHING → DEMOTED → BLACKLISTED
    escalating cooldowns, transition logging
            │
            ▼
Orderbook Subscriber                                       ⬜ NOT BUILT
    subscribe to orderbook_delta for candidates only
    maintain live orderbook per candidate
            │
            ▼
Tier 2 Analysis                                            ⬜ NOT BUILT
    flow toxicity, fill probability, autocorrelation,
    ADF stationarity, effective edge
    → approve or reject for quoting
            │
            ▼
Quoting Engine                                             ⬜ NOT BUILT
    penny-jump quotes, size based on metrics + inventory
    skew quotes away from inventory
            │
            ▼
Execution                                                  ⬜ NOT BUILT
    place/cancel orders via REST API
    track resting orders
            │
            ▼
Risk / Kill Switch                                         ⬜ NOT BUILT
    per-market P&L limit, global P&L limit
    metric deterioration triggers demotion
            │
            ▼
Persistence                                                🟡 PARTIAL
    raw WS messages → JSONL                                ✅
    state transitions → JSONL                              ✅
    metrics snapshots, P&L                                 ⬜
```

## What Exists Today

### `kalshi_ingest/` — REST Data Ingester

Batch CLI tool for pulling data from Kalshi's REST API. All commands write raw JSONL (every API response) and flattened CSV (one row per record) to the output directory.

- `ingest_markets()` — download all market metadata, with optional filters by status, series, event, or ticker
- `ingest_trades()` — download historical trade data, with optional ticker and time range filters
- `ingest_orderbook()` — download orderbook snapshots for a list of tickers, with optional depth limit
- `fetch_trades_sample()` — quick one-page fetch to check connectivity and see exact ticker formats
- Auth via RSA-PSS signed headers (`KalshiAuth`)
- Output: `*_raw_{timestamp}.jsonl` + `*_flat_{timestamp}.csv`

---

### `kalshi_ws/` — WebSocket Data Foundation

Live async WebSocket client that streams real-time data for all active markets. This is the data layer that all downstream analysis and trading logic will read from.

- Connects with RSA-signed auth headers, auto-reconnects with exponential backoff (1s → 2s → 4s → … → 60s cap)
- Subscribes to **ticker** (all markets top-of-book) and **trade** (all trades) channels with no market filter
- Maintains in-memory state updated on every message:
  - `market_states`: latest bid, ask, spread, last price, volume, open interest, and update count per market
  - `trade_buffers`: rolling window of recent trades per market (default 5000, configurable)
- Persists raw messages to daily JSONL files (`ticker_stream_YYYYMMDD.jsonl`, `trade_stream_YYYYMMDD.jsonl`)
- Disk writes buffered and flushed in a background thread — never blocks the message loop

**Access state from downstream code:**

```python
from kalshi_ws.stream import get_market_states, get_trade_buffer, get_subscription_ids

states = get_market_states()           # Dict[str, MarketTicker]
trades = get_trade_buffer("KXBTC-…")   # deque[Trade]
sids   = get_subscription_ids()        # {"ticker": 1, "trade": 2}
```

---

### `kalshi_filter/` — Tier 1 Filter + Orchestrator

The core filtering system. Runs the WebSocket stream, periodically pulls REST metadata, and evaluates every market against configurable criteria. Contains five components:

**Configuration (`config.py`):**
- Loads all parameters from `config.json` (or a custom path via `KALSHI_CONFIG_PATH` env var)
- Parses into typed, nested dataclasses — access as `config.tier1.min_spread`, not `config["tier1"]["min_spread"]`
- Missing keys → uses defaults and prints a warning
- Wrong types → raises `TypeError` immediately at startup
- If no config file exists, writes one with all defaults for reference

**REST Metadata Bootstrap (`metadata.py`):**
- At startup, calls `ingest_markets(status="open")` to get expiration times, event tickers, and market status for all active markets
- Reads the JSONL output back into `Dict[str, MarketMetadata]` keyed by ticker
- Refreshes every 5 minutes (configurable) in a background thread via `asyncio.to_thread`
- Accessors: `get_market_metadata()`, `get_metadata(ticker)`

**Tier 1 Filter (`filter.py`):**
- Evaluates every market every 60 seconds (configurable) against 7 checks:
  1. **No quotes** — bid or ask is zero → fail
  2. **Spread bounds** — must be between 3¢ and 40¢ (configurable)
  3. **Decided** — bid ≥ $0.95 or ask ≤ $0.05 means the outcome is essentially known → fail (configurable)
  4. **Expiry** — must have at least 30 minutes until expiration (configurable)
  5. **Volume** — dollar volume must exceed minimum (configurable)
  6. **Activity** — update rate (updates/min) must exceed minimum (configurable)
  7. **Imbalance** — if enough trades exist, the yes/no taker ratio must not deviate too far from 50/50 (configurable)
- 4-state lifecycle per market: `IGNORED` → `WATCHING` → `DEMOTED` → `BLACKLISTED`
- Promotion requires N consecutive passes (default 5). Demotion after N consecutive fails (default 3).
- Escalating cooldowns: 15 min → 2 hours → permanent blacklist (configurable)
- Accessors: `get_candidates()`, `get_market_status(ticker)`, `get_all_trackers()`

**Transition Logger (`transitions.py`):**
- Every status change is printed to console with formatted metrics and written to a daily JSONL file
- End-of-cycle summary: `EVAL: 347 markets | 12 WATCHING | 3 DEMOTED | 2 BLACKLISTED | 330 IGNORED`
- JSONL records include timestamp, ticker, old/new status, metrics snapshot

**Orchestrator (`__main__.py`):**
- Loads config → bootstraps metadata → starts WS stream → waits for initial data → runs metadata refresh loop + evaluation loop
- Handles graceful shutdown on Ctrl+C

---

### Tests

34 unit tests across two files:

**`tests/test_ws_models.py`** — 8 tests:
- String-to-float parsing (`"0.52"` → `0.52`)
- `MarketTicker.from_msg()` construction and field correctness
- `MarketTicker.update()` partial merge
- Spread derivation (`yes_ask - yes_bid`)
- `Trade.from_msg()` construction
- Deque max length enforcement

**`tests/test_filter.py`** — 26 tests:
- Config loading: default generation, missing key handling, type validation, missing section
- `MarketMetadata` parsing from API dict, ISO timestamp parsing
- `evaluate_market` with 11 scenarios: pass all, spread too tight, spread too wide, decided YES, decided NO, extreme imbalance, insufficient trades (imbalance skipped), no quotes, low volume, low activity, expiring soon, missing metadata (expiry skipped)
- Promotion: requires N consecutive passes, resets on fail
- Demotion: triggers after N consecutive fails, correct cooldown duration
- Cooldown: blocks re-evaluation during cooldown, expires correctly, second demotion has longer cooldown
- Blacklist: triggers after max demotions
- Transition logging: JSONL record has correct fields

## Running the System

### Prerequisites

- Python 3.10+
- Kalshi API key — set `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH` in `.env` (see `.env.example`)
- `pip install -r requirements.txt`

### Run the full system (WebSocket + Tier 1 Filter)

This is the primary entry point. It connects to Kalshi, streams all market data, and continuously evaluates markets:

```bash
python3 -m kalshi_filter
```

On startup you'll see:
```
Kalshi Market-Making System — Tier 1 Filter
Config: config.json
Metadata refresh: every 300s
Evaluation interval: every 60s
Loaded metadata for 347 active markets
WebSocket stream started. Waiting for initial data...
Receiving data for 312 markets
FIRST EVAL: 312 markets scanned | 0 WATCHING (need 5 consecutive passes)
```

After several evaluation cycles (5+ minutes), markets that consistently pass all filters will be promoted:
```
[2026-03-30 23:15:01] PROMOTED KXBTC-26APR01-T100000: IGNORED → WATCHING
    spread=0.080  volume=$1250  imbalance=0.48  rate=3.2/min  passes=5/5
[2026-03-30 23:15:01] EVAL: 347 markets | 1 WATCHING | 0 DEMOTED | 0 BLACKLISTED | 346 IGNORED
```

### Run the WebSocket stream only (no filter)

Useful for collecting raw data without running the filter:

```bash
python3 -m kalshi_ws
```

### Batch REST ingestion

```bash
# Download all open markets
python3 -m kalshi_ingest markets --status open --out-dir data/kalshi

# Download trades for a specific market
python3 -m kalshi_ingest trades --ticker SOMETICKER --out-dir data/kalshi

# Check API connectivity and see sample ticker strings
python3 -m kalshi_ingest trades-sample --limit 100

# Download orderbook snapshots
python3 -m kalshi_ingest orderbook --tickers TICK1,TICK2 --out-dir data/kalshi
```

### Verifying that things work

**1. Run the test suite** — this is the fastest way to confirm all parsing, filtering, config loading, and lifecycle logic works without needing API credentials:

```bash
python3 -m pytest tests/ -v
```

What the tests prove:
- **Config system works**: loads JSON, fills missing keys with defaults, rejects wrong types
- **Data parsing is correct**: WebSocket string-to-float conversion, ISO timestamp parsing, spread derivation, partial updates all produce the right values
- **Filter logic is correct**: each of the 7 checks (spread, decided, expiry, volume, activity, imbalance, no quotes) correctly passes or fails on synthetic data
- **Lifecycle state machine is correct**: promotion requires N consecutive passes, demotion triggers after N consecutive fails, cooldowns block re-evaluation for the right duration, escalating cooldowns grow, blacklist triggers after max demotions
- **Transition logger produces valid JSONL**: records have all required fields (timestamp, ticker, statuses, metrics)

**2. Verify REST connectivity** — confirms your API credentials work and you can reach Kalshi:

```bash
python3 -m kalshi_ingest trades-sample --limit 10
```

Expected output: trade count, cursor, and a list of sample tickers. If this fails, check your `.env` file.

**3. Verify WebSocket connectivity** — confirms the live stream works:

```bash
python3 -m kalshi_ws
```

Watch stderr for `Connected.` and `Subscribed to ticker/trade` messages. After a few seconds, raw JSONL files appear in `data/kalshi/ws/`. Ctrl+C to stop.

**4. Run the full filter** — confirms everything works end-to-end:

```bash
python3 -m kalshi_filter
```

Watch for the `FIRST EVAL` line confirming markets are being scanned. After 5+ evaluation cycles (5+ minutes with default 60s interval), any qualifying markets will be promoted to WATCHING. Check `data/kalshi/transitions/` for JSONL transition logs.

### Configuration

All thresholds are in `config.json`. If the file doesn't exist, it's auto-generated with defaults on first run. Key parameters:

| Parameter | Default | What it controls |
|-----------|---------|-----------------|
| `tier1.min_spread` | 0.03 | Minimum spread (in dollars) to consider a market |
| `tier1.max_spread` | 0.40 | Maximum spread — too wide means illiquid |
| `tier1.max_confidence_threshold` | 0.95 | Max bid before market is "decided YES" |
| `tier1.min_confidence_threshold` | 0.05 | Min ask before market is "decided NO" |
| `tier1.min_dollar_volume` | 100 | Minimum dollar volume |
| `tier1.min_update_rate` | 1.0 | Minimum updates per minute |
| `tier1.min_expiry_seconds` | 1800 | Minimum time to expiry (30 min) |
| `tier1.max_imbalance_deviation` | 0.35 | Max deviation from 50/50 yes/no ratio |
| `tier1.consecutive_passes_required` | 5 | Passes needed before promotion |
| `tier1.consecutive_fails_allowed` | 3 | Fails before demotion |
| `cooldowns.first_demotion_seconds` | 900 | First cooldown (15 min) |
| `cooldowns.second_demotion_seconds` | 7200 | Second cooldown (2 hours) |
| `cooldowns.max_demotions_before_blacklist` | 3 | Demotions before permanent blacklist |
| `evaluation.interval_seconds` | 60 | Seconds between evaluation cycles |
| `metadata.refresh_interval_seconds` | 300 | Seconds between REST metadata refreshes |

## Task List

Ordered by dependency. Each task builds on what comes before it.

---

### ~~Task 1: REST Metadata Bootstrap~~ ✅ DONE

Implemented in `kalshi_filter/metadata.py`. Pulls all active markets via REST at startup and refreshes every 5 minutes. Wrapped in `asyncio.to_thread` so it doesn't block the event loop.

---

### ~~Task 2: Tier 1 Filter~~ ✅ DONE

Implemented in `kalshi_filter/filter.py`. Evaluates all markets every 60 seconds against 7 configurable checks. Lifecycle state machine with IGNORED → WATCHING → DEMOTED → BLACKLISTED. Escalating cooldowns.

---

### Task 3: Orderbook Subscriber + State Manager

**Depends on:** Task 2 (done)

**Purpose:** Subscribe to `orderbook_delta` for Tier 1 candidates and maintain a live in-memory orderbook per market.

**Inputs:** Candidate tickers from Tier 1, WebSocket connection from `kalshi_ws/`

**Outputs:** `Dict[str, LiveOrderbook]` in memory with full depth per candidate, plus rolling mid-price history

**Key details:**
- Use `update_subscription` commands to add/remove tickers dynamically as Tier 1 promotes and demotes
- On subscribe: receive `orderbook_snapshot`, initialize book as `{price: quantity}` dicts for yes and no sides
- On `orderbook_delta`: apply additive delta to the correct price level; if quantity reaches zero or below, remove the level
- Track `seq` per subscription ID; a gap in sequence numbers means a missed message — resubscribe to get a fresh snapshot
- Derive and store `mid_price` after every book update in a rolling deque for time-series analysis
- All prices arrive as strings, parse to floats at boundary (same convention as ticker/trade channels)

---

### Task 4: Tier 2 Analysis

**Depends on:** Task 3

**Purpose:** Compute detailed metrics for each candidate market and decide whether it's safe to quote.

**Inputs:** `LiveOrderbook`, `trade_buffers`, mid-price history per market

**Outputs:** Per-market metrics dict and a QUOTE / DON'T QUOTE decision

**Key details:**
- **Flow toxicity:** for each trade, measure the signed mid-price change over the next 30–60 seconds. Mean positive value means prices move against us after we'd fill — that's informed flow, and we skip the market.
- **Fill probability:** match historical trades against book state at time of trade. Estimate P(fill) at penny-jump distance from best bid/ask.
- **Autocorrelation:** of trade-to-trade mid-price returns. Negative autocorrelation = mean-reverting = good for market making.
- **ADF stationarity test:** is the mid-price series stationary over the evaluation window? Stationary = bounded range = good.
- **Effective edge:** estimated spread captured minus estimated adverse selection cost. Must be positive to approve.
- Recompute every 30–60 seconds
- Minimum warm-up period (5–15 minutes of data) before any market can be approved
- All thresholds from config

---

### Task 5: Market Lifecycle State Machine (QUOTING state)

**Depends on:** Task 2 (done), Task 4

**Purpose:** Extend the existing lifecycle to include `WATCHING → QUOTING` transitions, driven by Tier 2 analysis results.

**Inputs:** Tier 1 filter output (done), Tier 2 analysis output, P&L data

**Outputs:** Current status per market, transition events logged with full metric snapshots

**Key details:**
- The IGNORED → WATCHING → DEMOTED → BLACKLISTED states already exist. This task adds the `QUOTING` state.
- `WATCHING → QUOTING`: warm-up complete + all Tier 2 metrics pass thresholds
- `QUOTING → DEMOTED`: Tier 2 metrics fail N consecutive times, OR per-market P&L kill switch trips
- Quote sizing scales continuously with metrics (e.g., toxicity up → size down) before a hard demotion

---

### Task 6: Quoting Engine

**Depends on:** Task 5, order placement API docs

**Purpose:** For markets in QUOTING state, compute the desired bid and ask prices and sizes.

**Inputs:** `LiveOrderbook`, Tier 2 metrics, current inventory, market status

**Outputs:** Desired quotes per market (price + size for bid side, price + size for ask side)

**Key details:**
- Base strategy: `bid = best_bid + 1¢`, `ask = best_ask − 1¢` (penny-jump)
- Size scales with metric confidence, inventory exposure, and effective edge
- Inventory skew: if net long, reduce bid size and increase ask size to encourage rebalancing
- Hard position limit per market — never exceed regardless of metrics
- This component only *computes* desired quotes. It does not place orders — that's Task 7.

---

### Task 7: Execution Layer

**Depends on:** Task 6, order placement API docs (NOT YET AVAILABLE)

**Purpose:** Compare desired quotes to currently resting orders and send the minimal set of API calls to get the book into the desired state.

**Inputs:** Desired quotes from Task 6, current resting orders tracked locally

**Outputs:** Orders placed and cancelled on Kalshi

**Key details:**
- Requires order placement API documentation (not yet available)
- Track all resting orders locally in a dict
- Compare desired vs. actual, compute diff, send only necessary place/cancel calls
- Handle fill notifications from the WebSocket `fill` channel to update inventory and P&L
- Rate-limit API calls to stay within Kalshi's limits

---

### Task 8: Risk and Kill Switches

**Depends on:** Task 7

**Purpose:** Hard safety limits that override everything else. No hysteresis, no gradual scaling — immediate action.

**Inputs:** Fill data, position data, P&L calculations

**Outputs:** Cancel-all signals, per-market shutdown, global shutdown

**Key details:**
- Per-market realized P&L limit — if a single market loses more than X, cancel all orders and blacklist
- Per-market total (unrealized + realized) P&L limit
- Global total P&L limit across all markets — if breached, cancel everything and shut down
- Position hard limit per market — if exceeded (e.g., from a fill burst), cancel orders on that side
- Extended WebSocket disconnect (> N seconds) triggers global cancel-all
- Kill switch actions are immediate and logged

---

### ~~Task 9: Persistence and Logging~~ 🟡 PARTIAL

Raw WS messages and state transitions are logged. Still needed: metric snapshots per market, fill records, P&L attribution.

---

### ~~Task 10: Configuration System~~ ✅ DONE

Implemented in `kalshi_filter/config.py`. JSON config with typed dataclasses, validation, defaults, and auto-generation.

## Technical Notes

- The WebSocket module (`kalshi_ws/`) signs the handshake directly rather than using `KalshiAuth.sign()`, because that method prepends `base_url` which produces the wrong path for the WS endpoint (`/trade-api/ws/v2` vs `/trade-api/v2/…`).
- All WebSocket numeric values arrive as strings and are parsed to floats at the message boundary. Everything downstream works with floats.
- The REST ingester (`kalshi_ingest/`) is synchronous. The WebSocket module (`kalshi_ws/`) and filter (`kalshi_filter/`) are async. The metadata bootstrap wraps sync REST calls in `asyncio.to_thread`.
- The Tier 1 filter's activity rate is computed as the change in `update_count` between consecutive evaluations, divided by elapsed minutes. The first evaluation for any market skips the activity check (no baseline).
- Markets without REST metadata (new markets not yet fetched) are skipped entirely during evaluation — no pass or fail counted. They're picked up after the next metadata refresh.
- Orderbook deltas are additive: `delta_fp` is added to the current quantity at that price level. If the result is zero or negative, remove the level entirely.
- The `seq` field on orderbook messages must be tracked per subscription ID. A gap in sequence numbers means a missed message and requires resubscribing to get a fresh snapshot.
