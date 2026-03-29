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
REST Bootstrap (kalshi_ingest/)
    GET /markets → initial market universe + metadata
            │
            ▼
WebSocket Connection (kalshi_ws/)                     ✅ BUILT
    ticker channel → all markets top-of-book
    trade channel  → all market trades
            │
            ▼
In-Memory State                                       ✅ BUILT
    market_states: Dict[str, MarketTicker]
    trade_buffers: Dict[str, deque[Trade]]
            │
            ▼
Tier 1 Filter                                         ⬜ NOT BUILT
    spread, volume, activity, basic flow imbalance
    → promotes candidates
            │
            ▼
Orderbook Subscriber                                  ⬜ NOT BUILT
    subscribe to orderbook_delta for candidates only
    maintain live orderbook per candidate
            │
            ▼
Tier 2 Analysis                                       ⬜ NOT BUILT
    flow toxicity, fill probability, autocorrelation,
    ADF stationarity, effective edge
    → approve or reject for quoting
            │
            ▼
Quoting Engine                                        ⬜ NOT BUILT
    penny-jump quotes, size based on metrics + inventory
    skew quotes away from inventory
            │
            ▼
Execution                                             ⬜ NOT BUILT
    place/cancel orders via REST API
    track resting orders
            │
            ▼
Risk / Kill Switch                                    ⬜ NOT BUILT
    per-market P&L limit, global P&L limit
    metric deterioration triggers demotion
            │
            ▼
Persistence                                           🟡 PARTIAL
    raw WS messages → JSONL                           ✅
    metrics, transitions, P&L                         ⬜
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

**Commands:**

```bash
# Download all open markets
python3 -m kalshi_ingest markets --status open --out-dir data/kalshi

# Download trades for a specific market
python3 -m kalshi_ingest trades --ticker KXNCAAMBGAME-26MAR10IDHOEWU-EWU --out-dir data/kalshi

# Check API connectivity and see sample ticker strings
python3 -m kalshi_ingest trades-sample --limit 100

# Download orderbook for specific markets
python3 -m kalshi_ingest orderbook --tickers TICK1,TICK2 --depth 10 --out-dir data/kalshi
```

Options: `--env-file PATH`, `--out-dir PATH`, `--limit N`. Use exact ticker strings from the API (uppercase, with suffix).

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

**Run standalone:**

```bash
python3 -m kalshi_ws
```

**Environment overrides:**

| Variable | Default | Purpose |
|----------|---------|---------|
| `KALSHI_WS_URL` | derived from `KALSHI_BASE_URL` | WebSocket endpoint (wss://…) |
| `KALSHI_WS_OUT_DIR` | `data/kalshi/ws` | Directory for raw JSONL stream files |
| `KALSHI_WS_TRADE_BUFFER` | `5000` | Max trades retained per market in memory |

**Access state from downstream code:**

```python
from kalshi_ws.stream import get_market_states, get_trade_buffer, get_subscription_ids

states = get_market_states()           # Dict[str, MarketTicker]
trades = get_trade_buffer("KXBTC-…")   # deque[Trade]
sids   = get_subscription_ids()        # {"ticker": 1, "trade": 2}
```

---

### Tests

- 8 unit tests covering WebSocket data model parsing, string-to-float conversion, spread derivation, partial updates, and deque bounds
- Run: `python3 -m pytest tests/test_ws_models.py -v`

## Task List

Ordered by dependency. Each task builds on what comes before it.

---

### Task 1: REST Metadata Bootstrap

**Depends on:** nothing (`kalshi_ingest/` already exists)

**Purpose:** On startup, pull all active markets via REST so the system knows expiration times, event tickers, and market status before the WebSocket stream begins.

**Inputs:** Kalshi REST API via existing `ingest_markets(status="open")`

**Outputs:** `Dict[str, MarketMetadata]` in memory — ticker, expiration\_time, status, event\_ticker, result for every active market

**Key details:**
- Run once at startup before the WebSocket connects
- Re-run every ~5 minutes in the background to catch newly listed or settled markets
- Must not block the async WebSocket loop (use `asyncio.to_thread` to wrap the sync REST calls)
- `MarketMetadata` is a new dataclass separate from `MarketTicker` — it holds static/slow-changing fields from the REST API, while `MarketTicker` holds fast-changing fields from the WebSocket

---

### Task 2: Tier 1 Filter

**Depends on:** Task 1, `kalshi_ws/` (built)

**Purpose:** Continuously evaluate every market using ticker and trade data and identify candidates worth subscribing to orderbook data for.

**Inputs:** `market_states` dict, `trade_buffers` dict, `MarketMetadata` dict

**Outputs:** A live list of candidate tickers that pass all filters

**Key details:**
- Hard filters (all thresholds from config, nothing hardcoded):
  - Spread > 3¢ and < 40¢
  - Market not effectively decided (bid < $0.95 and ask > $0.05)
  - Time to expiry above minimum
  - Volume above minimum
  - Update frequency (update\_count rate) above minimum
- Flow check: directional imbalance from trade buffer — ratio of yes-taker to no-taker trades. Extreme imbalance = informed flow = skip.
- Consistency requirement: a market must pass N consecutive evaluations before promotion (avoids flickering)
- Run evaluation every ~60 seconds
- Markets previously demoted have escalating cooldowns: 15 min → 2 hours → permanent blacklist
- All thresholds loaded from the configuration system (Task 10)

---

### Task 3: Orderbook Subscriber + State Manager

**Depends on:** Task 2

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

### Task 5: Market Lifecycle State Machine

**Depends on:** Task 2, Task 4

**Purpose:** Manage each market's state transitions with proper hysteresis and cooldowns so the system doesn't flip-flop.

**Inputs:** Tier 1 filter output, Tier 2 analysis output, P&L data

**Outputs:** Current status per market, transition events logged with full metric snapshots

**Key details:**
- States: `IGNORED` → `WATCHING` → `QUOTING` → `DEMOTED`
- `IGNORED → WATCHING`: market passes Tier 1 filter N consecutive times
- `WATCHING → QUOTING`: warm-up complete + all Tier 2 metrics pass thresholds
- `QUOTING → DEMOTED`: Tier 2 metrics fail N consecutive times, OR per-market P&L kill switch trips
- `DEMOTED → IGNORED`: observation window expires, cooldown starts
- Escalating cooldowns: 15 min → 2 hours → permanent blacklist
- Quote sizing scales continuously with metrics (e.g., toxicity up → size down) before a hard demotion — gradual degradation, not cliff edges
- Every transition logged with full metric snapshot for post-session analysis

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

### Task 9: Persistence and Logging

**Depends on:** built incrementally alongside Tasks 2–8

**Purpose:** Record everything for post-session analysis, debugging, and threshold tuning.

**Inputs:** All events, metrics, transitions, P&L

**Outputs:** JSONL files (or SQLite, TBD)

**Key details:**
- Metric snapshots every ~30 seconds per market
- Every state transition with full context (metrics at time of transition, reason)
- Every fill with P&L attribution
- Must not block the trading loop — use async writes or background thread
- Append-only format, analyzed after session ends

---

### Task 10: Configuration System

**Depends on:** nothing, but should be built before or alongside Task 2

**Purpose:** Externalize all thresholds, window sizes, and parameters so they can be tuned without code changes.

**Inputs:** YAML or JSON config file

**Outputs:** Typed config object accessible by all components

**Key details:**
- Tier 1 filter thresholds (spread bounds, volume min, activity min, imbalance max)
- Tier 2 metric thresholds (toxicity max, fill probability min, autocorrelation bounds, edge min)
- Warm-up durations, evaluation intervals
- Cooldown durations and escalation schedule
- Quote sizing parameters and position limits
- Kill switch limits (per-market, global)
- All parameters should have sensible defaults so the system can run without a config file

## Running the System

**Prerequisites:**
- Python 3.10+
- Kalshi API key — set `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH` in `.env` (see `.env.example`)
- `pip install -r requirements.txt`

**Current commands:**

```bash
# Stream all market data in real time (WebSocket)
python3 -m kalshi_ws

# Batch download: all open markets
python3 -m kalshi_ingest markets --status open --out-dir data/kalshi

# Batch download: trades for a specific market
python3 -m kalshi_ingest trades --ticker SOMETICKER --out-dir data/kalshi

# Batch download: orderbook snapshots
python3 -m kalshi_ingest orderbook --tickers TICK1,TICK2 --out-dir data/kalshi
```

## Technical Notes

- The WebSocket module (`kalshi_ws/`) signs the handshake directly rather than using `KalshiAuth.sign()`, because that method prepends `base_url` which produces the wrong path for the WS endpoint (`/trade-api/ws/v2` vs `/trade-api/v2/…`).
- All WebSocket numeric values arrive as strings and are parsed to floats at the message boundary. Everything downstream works with floats.
- The REST ingester (`kalshi_ingest/`) is synchronous. The WebSocket module (`kalshi_ws/`) is async. They coexist as separate packages and don't share a runtime.
- Orderbook deltas are additive: `delta_fp` is added to the current quantity at that price level. If the result is zero or negative, remove the level entirely.
- The `seq` field on orderbook messages must be tracked per subscription ID. A gap in sequence numbers means a missed message and requires resubscribing to get a fresh snapshot.
