# quant-pod-c: Kalshi Avellaneda–Stoikov Market-Making System

An automated market-making system for [Kalshi](https://kalshi.com) prediction markets. The system streams real-time market data via WebSocket, estimates per-market volatility from rolling mid-price history, computes inventory-adjusted Avellaneda–Stoikov bid/ask quotes, and can place, track, and cancel resting limit orders via the Kalshi REST API.

**No opinions about event outcomes.** The strategy is purely microstructure: quote markets with favorable spread and flow characteristics, let the model determine optimal placement, and collect the spread while managing inventory risk.

---

## How the System Works — A Complete Walkthrough

This section explains the full pipeline in plain English, from raw API data all the way to resting orders on the exchange. No prior market-making experience is assumed.

---

### Background: What Is Market Making?

A **market maker** is a participant who simultaneously posts a **bid** (offer to buy) and an **ask** (offer to sell) for an asset. If another trader buys from you at the ask and a different trader sells to you at the bid, you have completed a **round trip**: you collected the difference between the two prices — the **spread** — as profit, and ended up with zero net position.

On Kalshi, the assets are **prediction market contracts**. Each contract pays out $1 if a specific event occurs (e.g., "will the Fed cut rates in June?") and $0 if it does not. The **YES price** at any moment represents the market's consensus probability of that event (e.g., 0.55 = 55% implied chance). The **NO price** is always `1 − YES price`.

Market making on prediction markets works the same way as on stocks or crypto: post a bid slightly below the current mid-price and an ask slightly above it, wait for other traders to fill your orders, and profit from the spread. The risk is **adverse selection** — a better-informed trader fills your order right before the price moves against you.

This system tries to solve two problems:
1. **Which markets are worth quoting?** — Markets need a wide enough spread to be profitable, but not so wide that no one will trade.
2. **Where exactly should the quotes sit?** — The optimal placement depends on your current inventory (how many contracts you already hold), how volatile the market is, and how much time is left before expiry.

---

### Stage 1 — Bootstrap: Pulling Static Market Data (REST)

Before quoting anything, the system needs to know basic facts about every available market: what is it about, when does it expire, is it still open? This information does not change second-to-second, so it is fetched in bulk via Kalshi's **REST API** (a standard HTTP request-response interface).

**`kalshi_ingest/`** handles this. It fetches:

- **`GET /markets`** — returns a list of every market Kalshi has ever created, with metadata: ticker symbol, event description, current status (open/settled/closed), close time. This is the "universe" of markets.
- **`GET /markets/trades`** — returns a history of every executed trade across all markets (or filtered by ticker), with timestamps, prices, and sizes.
- **`GET /markets/{ticker}/orderbook`** — returns a snapshot of the current order book for a specific market: all resting bids and asks and their sizes.

All responses are saved to disk in two formats: raw **JSONL** (one JSON object per line, exactly as Kalshi returned it) and flattened **CSV** (one row per record, useful for analysis in pandas or Excel). These files are for offline research and backtesting — not consumed by the live strategy.

The `kalshi_as` strategy also performs a lightweight version of this bootstrap on startup: it fetches all open markets to extract their **close times** so it can compute, per market, how many hours remain until expiry. This feeds directly into the quoting model (explained in Stage 3).

---

### Stage 2 — Live Data: The WebSocket Stream

REST is slow — you request data and wait for a response. For a live trading system, you need prices updating in real time. Kalshi provides a **WebSocket** connection: a persistent two-way channel where Kalshi pushes every market update to you the instant it happens, without you having to ask.

**`kalshi_ws/`** manages this connection. It subscribes to three data channels:

#### The Ticker Channel
Every time a market's top-of-book changes — a new bid, a new ask, a change in volume or open interest — Kalshi sends a **ticker message** for that market. These messages arrive continuously for all active markets simultaneously.

The system processes each message and updates an **in-memory dictionary** called `market_states`. This is the live "snapshot" of every market: current best bid, current best ask, computed spread (`ask − bid`), last trade price, volume, open interest, and a timestamp of the last update.

Think of `market_states` as a table in RAM, constantly being updated, that tells the strategy "here is what every market looks like right now."

#### The Trade Channel
Every time a trade executes on Kalshi (any trader buys or sells anything), a **trade message** is broadcast. The system appends each trade to a per-market rolling buffer called `trade_buffers` (capped at 5,000 trades per market by default).

These trade histories are used to analyze **flow**: who is trading, in which direction, and how frequently. Heavy one-sided flow (e.g., everyone is buying YES) can signal that informed traders know something — which is dangerous for a market maker.

#### The Fill Channel (Optional)
When the system is actively placing orders, it needs to know when *its own* orders get filled. The **fill channel** delivers these notifications in real time. This is opt-in (requires setting `KALSHI_WS_ENABLE_FILLS=1`) because it requires authenticated user-scoped access.

Fills are stored in a separate buffer and periodically drained by the inventory tracking pipeline (Stage 5).

#### Persistence
Everything is written to disk in the background — ticker updates, trades, and fills each get their own daily JSONL file. This write is **buffered and non-blocking**: messages are collected in memory and flushed to disk every ~2 seconds or every 200 messages, whichever comes first. This ensures the live message processing loop is never slowed down by disk I/O.

---

### Stage 3 — The Quoting Model: Avellaneda–Stoikov

Once you have live market data, you need to decide *where* to place your bid and ask. A naïve approach ("always bid 1 cent below mid, ask 1 cent above") ignores two critical factors:

1. **Inventory risk**: If you keep buying YES contracts and the event resolves NO, you lose money on every contract you hold. The model should make you less eager to buy when you are already long.
2. **Market volatility**: If the YES price is jumping around unpredictably, your quotes are more likely to be "picked off" before you can react. The model should widen your spread in volatile markets to compensate.

The **Avellaneda–Stoikov (AS) model** addresses both. It was developed in a 2008 academic paper for continuous-time equity market making and has since become one of the standard frameworks for algorithmic quoting. This section explains the original model, every departure this implementation makes from it, and why those departures were chosen.

---

#### The Original Avellaneda–Stoikov Model

The 2008 paper by Marco Avellaneda and Sasha Stoikov solves an optimization problem: given a market maker who wants to maximize their expected utility of wealth at a future terminal time T, what bid and ask prices should they post at every moment in time?

The model makes several explicit assumptions about the world:

- The **mid-price** follows a continuous-time random walk (Brownian motion with no drift): `dS = σ dW`
- **Order arrivals** follow a Poisson process — orders hit your bid and ask at random times with a rate that decreases exponentially the further your quote is from the mid-price: `λ(δ) = A · e^(−k·δ)`, where δ is the distance from mid
- The market maker has **exponential utility** with risk aversion γ — meaning they care about both expected wealth and the variance of that wealth (they are not risk-neutral)
- The market maker can quote continuously and prices can be any real number

Under these assumptions, Avellaneda and Stoikov derive closed-form solutions for the optimal bid and ask:

**Reservation price** (the mid-price adjusted for inventory risk):
```
r(s, q, t) = s − q · γ · σ² · (T − t)
```

**Optimal spread** (the total bid-ask spread, not the half-spread):
```
δ_bid + δ_ask = γ · σ² · (T − t) + (2/γ) · ln(1 + γ/k)
```

**Full variable definitions (original model):**

| Symbol | Name | Meaning |
|---|---|---|
| `s` | mid-price | The current fair value of the asset (midpoint of best bid and ask) |
| `q` | inventory | Number of shares/contracts held; positive = long, negative = short |
| `t` | current time | Current time in the trading session |
| `T` | terminal time | Time at which the session ends; the market maker cares about wealth at T |
| `T − t` | time to horizon | Hours (or seconds) remaining until the session ends |
| `γ` | risk aversion | How much the market maker penalizes inventory variance; γ > 0 |
| `σ` | volatility | Standard deviation of mid-price per unit time (e.g., per second) |
| `σ²` | variance | Square of volatility; appears because wealth variance grows with it |
| `A` | arrival intensity | Baseline rate at which orders arrive when quotes are at the mid-price |
| `k` | intensity decay | How quickly order arrival rate falls as you move away from mid; larger k = steeper falloff |
| `δ_bid` | bid distance | How far below the reservation price to place the bid |
| `δ_ask` | ask distance | How far above the reservation price to place the ask |
| `r` | reservation price | The mid-price adjusted for inventory; the "fair value to you" given your position |

The key insight is that the reservation price `r` shifts *away from mid* as inventory builds. If you are long (q > 0), r falls below mid — you value the asset less because you already have too much of it. This makes your bid less aggressive (you don't want to buy more) and your ask cheaper (you want to sell).

---

#### Our Reduced-Form Adaptation

This implementation uses a **reduced symmetric form** of the AS model — a common simplification used in practice. Here is every departure from the original and the reasoning for each.

**Our reservation price** (identical to the original):
```
v = mid − Q · γ · σ² · τ
```

**Our half-spread** (simplified from the original full-spread formula):
```
h = (1/γ) · ln(1 + γ/k) + 0.5 · γ · σ² · τ
```

**Our quote construction:**
```
bid = floor_to_tick(v − h)
ask = ceil_to_tick(v + h)
```

**Full variable definitions (our implementation):**

| Symbol | Name | Value/Source | Meaning |
|---|---|---|---|
| `mid` | mid-price | `0.5 × (yes_bid + yes_ask)` from WebSocket | Current fair-value estimate from live order book |
| `Q` | inventory | `ledger.qty_for_ticker()` if live, else `--inventory` | YES contracts currently held; positive = long YES |
| `γ` | risk aversion | `--gamma`, default 0.05 | Controls how strongly inventory pushes the reservation price away from mid; higher = more aggressive skew |
| `σ` | volatility | EWMA of log-returns from mid history | Per-√hour standard deviation of the YES price; estimated fresh each cycle |
| `σ²` | variance | `σ × σ` | Variance; enters the formula because risk scales with variance, not standard deviation |
| `τ` | time to horizon | Hours until market close from `market_meta` | How long until the contract settles and inventory must be zero; shrinks continuously |
| `k` | intensity decay | `--k`, default 1.5 | Shape of the order-arrival rate curve; larger k = orders arrive less frequently far from mid |
| `A` | arrival intensity | `--A`, default 1.0 (placeholder) | Baseline arrival rate at mid; stored and logged but not yet applied to the spread formula |
| `h` | half-spread | computed | Distance from reservation price to each side of the quote |
| `v` | reservation price | computed | The "fair value to you" adjusted for inventory and time |
| `bid` | model bid | `floor_to_tick(v − h)` | Price at which you offer to buy YES; floored so you never accidentally overpay |
| `ask` | model ask | `ceil_to_tick(v + h)` | Price at which you offer to sell YES; ceiled so you never accidentally undersell |

---

#### Departure 1 — Symmetric Spread

**Original model:** The optimal bid distance (δ_bid) and ask distance (δ_ask) are in general different, especially when inventory Q ≠ 0. The full solution produces an asymmetric spread — if you are long, the bid is pulled further from mid than the ask.

**Our implementation:** We use a **symmetric half-spread h** applied equally to both sides. The asymmetry from inventory is captured entirely by the reservation price shift (v < mid when long). Both sides then sit h away from this already-shifted v.

**Why:** The symmetric form is mathematically equivalent to the original in continuous time under the same Brownian motion assumptions. In practice, Kalshi ticks are $0.01 and quote refresh cycles are 5 seconds — continuous adjustment is not meaningful. The symmetric form is also simpler to implement and test correctly. The net result is the same: when you are long YES, your bid moves further below the current market mid and your ask moves closer to it, making you cheaper to buy from (encouraging inventory reduction).

---

#### Departure 2 — Discrete Time Horizon vs. Continuous

**Original model:** Time-to-horizon `T − t` is a continuous value in seconds that decreases smoothly. The model was derived assuming the market maker can adjust quotes at any instant.

**Our implementation:** We sample time-to-horizon `τ` once per cycle (every `--interval` seconds, default 5). It is measured in **hours** for numerical stability with typical Kalshi market lifetimes (hours to days). Volatility σ is also expressed per-√hour so the units are consistent: `σ² × τ` has units of `(price²/hour) × hours = price²`, which is dimensionally correct for a price adjustment.

**Why:** The 5-second update frequency is already much faster than any meaningful change in τ for markets with hours remaining. The hours unit avoids the numerical issue of very small τ values (seconds) producing near-zero spreads that could be misinterpreted.

---

#### Departure 3 — Volatility Estimation from Ticker Mids, Not Trade Prices

**Original model:** σ is assumed to be the true continuous-time volatility of the underlying asset price, treated as a known constant.

**Our implementation:** σ is **estimated online** from a rolling window of mid-prices sampled every `--interval` seconds. The estimation procedure is:
1. Take the last `--mid-history` (default 80) mid-prices for this market
2. Apply EWMA smoothing to the mid-price series (α = 0.25) to reduce microstructure noise
3. Compute log-returns between consecutive smoothed mids
4. Apply EWMA to the squared log-returns to get a running variance estimate
5. Scale from per-sample to per-√hour: `σ_hour = σ_tick × √(3600 / dt)`
6. Apply floor (2%/√hr) and cap (500%/√hr) to reject outliers

**Why:** True volatility is unobservable and non-stationary. The rolling EWMA estimator is fast to compute (no matrix operations), naturally down-weights old data, and handles the irregular nature of prediction market price discovery. Using mid-prices rather than trade prices is more robust — trades may arrive sporadically and at non-representative prices, while the mid-price reflects the continuous consensus of resting orders.

**Implication:** σ is a coarse estimate, especially early in a market's life when only a few samples exist. The system refuses to quote until at least `--sigma-min-samples` (default 12) samples have accumulated, preventing quotes based on a single noisy observation.

---

#### Departure 4 — The `A` Parameter Is Not Yet Calibrated

**Original model:** The arrival intensity parameter A is calibrated from real fill data — you measure how often orders arrive at various distances from mid and fit the exponential curve `λ(δ) = A · e^(−k·δ)`. A and k together fully determine the optimal spread.

**Our implementation:** A is stored (`--A`, default 1.0) and logged in calibration records, but **does not currently appear in the spread formula**. The half-spread formula reduces to:

```
h = (1/γ) · ln(1 + γ/k) + 0.5 · γ · σ² · τ
```

In the original derivation, A appears inside the logarithm: `(2/γ) · ln(1 + γ/k)` is the spread when A → 1 (normalized). We omit A because we have not yet fitted it from real fill data — using an arbitrary A would produce a spread that is not grounded in the actual market's order arrival statistics.

The `ExecutionMonitor`'s `ArrivalFitter` class is already collecting the data needed to fit A and k from real fills as the system runs. Once enough fills have accumulated, the calibrated values can be fed into `ASConfig` and the formula updated to incorporate A correctly.

**Implication:** The current spread formula is well-behaved and risk-aware, but the "fill-rate" component (the first term) is based on assumed, not measured, order arrival statistics. The spread may be wider or narrower than truly optimal. This is a known limitation.

---

#### Departure 5 — Bounded Price Domain [0.01, 0.99]

**Original model:** Derived for an unbounded price process on ℝ — the mid-price can take any real value. Bids and asks can be any positive price.

**Our implementation:** Kalshi YES prices live strictly in [0.01, 0.99] (expressed in dollars, i.e., 1–99 cents). A YES price of 0 would mean the event is certainly false and the contract is worthless; a price of 1 would mean certainly true. Prices at these extremes indicate that the market has effectively resolved.

The system clamps all quotes to [tick, 1 − tick] = [$0.01, $0.99]. If the model computes a bid below $0.01 or an ask above $0.99, it is clamped. If the model computes a bid ≥ ask after clamping (possible in extreme cases where σ or inventory skew is very large), the quotes collapse to one tick either side of the reservation price — the minimum valid spread.

**Why:** Unclamped quotes would be rejected by the Kalshi API. The clamping is also economically correct: quoting a bid of $0.00 is meaningless on a binary contract.

---

#### Complete Worked Example

**Inputs:** mid = 0.55, Q = +8 (long 8 YES contracts), σ = 0.18/√hr, τ = 3.0 hr, γ = 0.05, k = 1.5

**Step 1 — Reservation price:**
```
v = 0.55 − 8 × 0.05 × (0.18²) × 3.0
v = 0.55 − 8 × 0.05 × 0.0324 × 3.0
v = 0.55 − 0.03888
v = 0.5111
```
The reservation price has shifted 3.9 cents *below* mid because you are long. You value the asset less — you are already exposed to it.

**Step 2 — Half-spread:**
```
h = (1/0.05) × ln(1 + 0.05/1.5) + 0.5 × 0.05 × (0.18²) × 3.0
h = 20 × ln(1.0333) + 0.5 × 0.05 × 0.0324 × 3.0
h = 20 × 0.03279 + 0.00243
h = 0.6558 + 0.00243
h ≈ 0.0658 + 0.00243 ≈ 0.0682
```
Wait — let me redo with careful arithmetic:
```
(1/γ) = 1/0.05 = 20
γ/k   = 0.05/1.5 = 0.03333
ln(1 + 0.03333) = ln(1.03333) ≈ 0.03279
intensity term  = 20 × 0.03279 ≈ 0.6558

σ²    = 0.18² = 0.0324
risk term = 0.5 × 0.05 × 0.0324 × 3.0 = 0.00243

h ≈ 0.06558 + 0.00243 ≈ 0.068
```
*(Note: the intensity term dominates at default parameters.)*

**Step 3 — Raw quotes:**
```
bid_raw = 0.5111 − 0.068 = 0.4431  →  floor to $0.44
ask_raw = 0.5111 + 0.068 = 0.5791  →  ceil  to $0.58
```

**Result:** Post **Buy YES @ $0.44 / Sell YES @ $0.58**.

Versus a flat inventory (Q = 0) the quotes would have been centered on $0.55 instead of $0.51 — the long inventory pushed both legs 3.9 cents downward, making you a cheaper seller and a less aggressive buyer. If both legs fill, gross profit = $0.14 per contract.

---




### Stage 4 — Candidate Selection: Which Markets to Quote

The system does not quote every market on Kalshi. Most are too illiquid, effectively settled, or have spreads too narrow to be profitable after fees. Before computing AS quotes, each cycle applies filters:

1. **Spread filter**: `spread ≥ --min-spread` (default $0.02). Markets where bid and ask are already very close together leave little room for the market maker to profit.
2. **Valid mid filter**: The market must have a real bid and ask (both > 0) and the ask must be above the bid. Markets at $0.01 bid / $0.01 ask (effectively resolved NO) or $0.99/$0.99 (effectively resolved YES) are excluded.
3. **Whitelist** (optional): If `--whitelist-tickers` is set, only those specific markets are considered. This is useful for canary testing.
4. **Sigma warmup**: A market is skipped until it has accumulated enough mid-price history to produce a reliable σ estimate.

The remaining candidates are sorted by spread (widest first) and the top `--max-markets` (default 12) are processed each cycle. The widest-spread markets are prioritized because they offer the most gross profit per round trip.

---

### Stage 5 — Execution: Placing and Managing Orders

This is where the system interacts with the exchange. The execution engine (`execution.py`) manages the lifecycle of resting limit orders.

#### Three Modes of Operation

- **`off`** (default): No orders are ever placed. The system only computes and logs quotes. This is safe for observation.
- **`dry-run`**: The system goes through all the logic — checking resting orders, computing differences — but only *logs* what it *would* do. No actual API calls.
- **`live`**: Real orders are placed and cancelled via the Kalshi REST API.

#### The Per-Cycle Sync Logic

Each cycle, for each market the strategy wants to quote, the execution engine:

1. **Fetches all resting orders** it owns for that market (identified by a `"as:"` prefix in the `client_order_id` field). This lets the system manage only its own orders and never accidentally cancel a manually placed order.
2. **Checks the buy side**: Is there already a resting buy-YES order at exactly the model's target bid price with the right size? If yes, leave it alone ("keep"). If not, cancel any stale buy order and place a new one at the correct price.
3. **Checks the sell side**: Same logic for the ask.
4. The net result: after each cycle, the system's resting orders match the model's desired quotes. Nothing more, nothing less.

#### Safety: Max Resting Orders Cap

A hard cap (`--max-tagged-resting-orders`, default 20) limits how many of the system's orders can be resting across the entire portfolio at once. If this cap is reached, new markets are skipped that cycle. This prevents runaway order accumulation during bugs or unexpected behaviour.

#### Panic Cancel

The `--panic-cancel-all` flag, when passed at startup, immediately cancels every resting order tagged with `"as:"` across the entire portfolio and exits. This is the emergency stop if something goes wrong.

---

### Stage 6 — Fill Tracking: Knowing When Orders Execute

A resting limit order sits on the exchange until another trader decides to trade against it. When this happens, the order (or part of it) **fills** — you have bought or sold contracts. The system needs to know about fills immediately to:

1. Update inventory (you now hold more or fewer contracts)
2. Feed the updated inventory back into the AS model (so the next cycle's quotes reflect your real position)
3. Track P&L

There are two ways fills are detected:

#### WebSocket Fill Consumer (default, `--monitor-mode ws`)

When `KALSHI_WS_ENABLE_FILLS=1` is set, the WebSocket layer subscribes to the `fill` channel. Kalshi pushes a fill notification the instant an order executes. The `ws_fill_consumer` coroutine runs continuously alongside the strategy loop, draining these fill events every 0.5 seconds and feeding them into the ledger.

#### REST Poll Monitor (alternative, `--monitor-mode rest`)

Without the fill channel, the system polls `GET /portfolio/orders` every few seconds to check the status of each tracked resting order. If an order's `remaining_count` has decreased, a partial fill occurred. If the order has disappeared from "resting" status, it either fully filled or was cancelled.

This approach uses more API calls but works without the fill channel environment variable. It also enables the `ArrivalFitter` — a statistical tool that fits the relationship between price-distance-from-mid and fill rate, which will eventually be used to calibrate the `k` and `A` model parameters from real data.

---

### Stage 7 — Inventory and P&L: The Ledger

Every fill gets recorded in the **portfolio ledger** (`ledger.py`). The ledger tracks, per market:

- **`qty_yes`**: How many YES contracts you currently hold (positive = long, negative = short / you've sold more than you've bought)
- **`cash_cents`**: The running cash from fills. Each buy subtracts `price × count` from cash; each sell adds it.
- **`mark-to-mid P&L`**: `cash_cents + qty_yes × current_mid_cents`. This is your hypothetical P&L if you could instantly close your position at the current mid-price. It includes both realized cash from completed round trips and unrealized value of any open inventory.

Every fill triggers a snapshot to `as_ledger.jsonl`, giving a complete audit trail of how inventory and P&L evolved over the session.

Critically, **the ledger feeds back into the AS model**. At the start of each quoting cycle, if the execution engine is active, `q_inventory = ledger.qty_for_ticker(ticker)` replaces any static inventory setting. This closes the loop: fills update the ledger → the ledger updates Q → the AS model adjusts reservation price → new quotes better reflect your actual exposure.

---

### Stage 8 — Risk Controls

The system has several guards against runaway losses or runaway exposure:

#### WebSocket Staleness Kill Switch
If the most recent ticker update across all markets is older than `--ws-stale-s` seconds (default 10), the strategy loop treats the data as stale — the WebSocket may have disconnected. In this case, **all tagged resting orders are immediately cancelled** and the strategy pauses until fresh data resumes. This prevents quoting on outdated prices that may no longer reflect the market.

#### Max Resting Orders Cap
As described in Stage 5: no more than `--max-tagged-resting-orders` (default 20) of the system's orders can rest on the exchange simultaneously. If the cap is hit, no new quotes are placed.

#### Near-Expiry Terminal Logic
As a market approaches expiry (closer than `--terminal-tau-minutes`, default 5 minutes), the AS model's time-horizon τ approaches zero. Very small τ produces unreliable, extremely tight quotes. The system detects this and:
1. Stops computing or placing new quotes for that market
2. Cancels all of the system's resting orders for that market
3. Logs a **terminal action record** to `as_terminal_actions.jsonl`. If inventory is non-zero at that point, the record is flagged as `flatten_intent` — a signal that the position needs to be manually closed. Automatic flattening is not yet implemented.

#### What Is NOT Yet Built
There is currently no **hard dollar P&L kill switch**: if a market loses $X, there is no automatic system-wide halt or per-market blacklist. Monitoring P&L via the ledger and the dashboard is currently manual. A future kill switch would watch `mark_to_mid_pnl_cents` and trigger `cancel_all_quotes_events()` on breach.

---

### Stage 9 — Monitoring and the Dashboard

The Streamlit dashboard (`ws_dashboard/app.py`) provides a real-time window into everything the system is doing. It reads the JSONL files the strategy produces — no direct process communication — and refreshes every 2 seconds.

**What it shows:**

- **WebSocket status**: Are the trade/ticker files being updated? If the last write was more than 10 seconds ago (configurable), the status shows "Stale" — the WebSocket may be down.
- **Viable markets panel**: For every market the system is tracking, it computes a **composite market-making score** from five factors:
  1. *Spread quality* — Is the quoted spread wide enough to be profitable after fees? Is it stable (not collapsing every tick)?
  2. *Fill / flow* — Are there enough trades per hour to get filled? Is flow two-sided (both YES and NO takers), or one-directional (informed flow)?
  3. *Adverse selection* — Do prices tend to revert after trades (good for market makers) or continue in the same direction (bad)?
  4. *Volume / depth* — Higher volume and open interest mean more counterparties.
  5. *Volatility stability* — Moderate volatility is ideal; extreme volatility widens required spreads beyond what is profitable.
  
  These factors are combined with configurable weights (adjusted via sidebar sliders) into a single score, sorted descending. This is not a filter — it is a relative ranking within the current candidate set.

- **AS sample orders table**: If `--sample-contracts` is set, every quoting cycle outputs one hypothetical order per market to JSONL. The dashboard displays these: ticker, mid, σ, book bid/ask, model reservation, half-spread, model bid, model ask, and **gross profit if both legs fill** = `(ask − bid) × contracts`. Note: this is gross profit before fees and ignores the probability of both legs filling.

- **Trade execution panel** (collapsible): A form to manually place limit or market orders directly via the Kalshi API. Accepts a market ticker or market ID, side (YES/NO), action (buy/sell), price in cents, and time-in-force. Useful for manual intervention — e.g., closing a position flagged as `flatten_intent` by the terminal logic.

- **Active orders panel** (collapsible): Shows all currently resting orders on the account. Each row has a **Cancel** button (sends `DELETE /portfolio/orders/{id}`) and a **Sell market** button (places a market sell order to immediately close the position). The dashboard hard-caps manual orders at 1 contract by default to prevent accidental large executions.

---

### Putting It All Together: One Complete Cycle

Here is what happens every 5 seconds (default `--interval`) when the system is running in `live` mode:

1. **Market metadata refresh** (rate-limited to every 5 minutes): Re-fetch all open markets from REST to get updated close times → recompute τ for every ticker.
2. **WS staleness check**: Is the newest ticker update fresh? If not, cancel all tagged orders and skip this cycle.
3. **Read live state**: `get_market_states()` returns the current bid, ask, spread, volume for every market.
4. **Candidate selection**: Filter by min spread, whitelist, valid mid. Sort by spread, take top 12.
5. **Per-market loop** (for each of the 12 candidates):
   - Append current mid to the 80-sample rolling history for this market
   - Compute σ from EWMA of log-returns on that history (skip if < 12 samples)
   - Get τ from market metadata (or global fallback)
   - Get Q from the live ledger (or static setting if execution is off)
   - **Terminal check**: if τ < 5 minutes → cancel orders, log terminal action, skip quoting
   - **Compute AS quotes**: reservation price, half-spread, bid, ask
   - **Sync resting orders**: check resting order count cap → fetch current resting orders → keep/cancel/place to match model bid and ask
   - Feed execution events to the fill monitor
   - Update ledger mid-price mark
   - Append calibration row + sample order row (if enabled)
6. **Flush output**: Write JSONL rows to `as_calibration.jsonl`, `as_sample_orders.jsonl`
7. **Log to console**: Print one line per market showing mid, σ, book, reservation, bid, ask, and execution actions
8. **Fill consumer** (running in parallel): Every 0.5 seconds, drain WS fill events → update ledger → update inventory fed to next cycle

---

## System Architecture

```
┌──────────────────────────────────────────────────┐
│ kalshi_ingest/  —  REST Batch Ingestion          │ ✅ Built
│  • GET /markets, /markets/trades, /markets/{t}/  │
│    orderbook  →  raw JSONL + flattened CSV        │
└──────────────────────────────────────────────────┘
                        ↓
┌──────────────────────────────────────────────────┐
│ kalshi_ws/  —  WebSocket Stream                  │ ✅ Built
│  • ticker channel  (top-of-book, all markets)    │
│  • trade channel   (all trades)                  │
│  • fill channel    (user fills, opt-in via env)  │
│  • In-memory: market_states, trade_buffers,      │
│    fill_buffers                                  │
│  • Non-blocking JSONL persistence (3 channels)   │
│  • Exponential backoff reconnect (1 s → 60 s)    │
└──────────────────────────────────────────────────┘
                        ↓
┌──────────────────────────────────────────────────┐
│ kalshi_as/  —  AS Strategy Engine                │ ✅ Built
│                                                  │
│  market_meta.py   per-ticker tau from REST       │
│  sigma.py         EWMA volatility estimation     │
│  model.py         AS reservation + half-spread   │
│  inventory.py     static inventory from JSON     │
│  ledger.py        live inventory + MTM P&L       │
│  execution.py     place/cancel orders (3 modes)  │
│  execution_monitor.py  fill detection + A,k fit  │
│  ws_fill_consumer.py   WS fills → ledger         │
│  terminal_actions.py   near-expiry cancel logic  │
│  strategy_loop.py      main async orchestrator   │
│  sample_orders.py      hypothetical JSONL output │
│  calibration_log.py    model params per cycle    │
└──────────────────────────────────────────────────┘
                        ↓
┌──────────────────────────────────────────────────┐
│ ws_dashboard/app.py  —  Streamlit UI             │ ✅ Built
│  • Live market state, recent trades              │
│  • AS sample orders table                        │
│  • Viable-market MM scoring (5-factor)           │
│  • Manual order placement panel                  │
│  • Active orders panel (cancel / sell-market)    │
└──────────────────────────────────────────────────┘

❌ Not yet built:
  • Per-market hard dollar P&L kill switch
  • Automatic position flattening at expiry
    (intent is logged; actual flatten order not sent)
  • Global session P&L shutdown limit

⚠️  Orphaned (no code reads it):
  • config.json  (legacy Tier-1/2 filter parameters)
  • kalshi_filter/  (empty directory)
```

---

## Prerequisites

- Python 3.10+
- Kalshi API credentials

```bash
pip install -r requirements.txt
```

Copy `.env.example` to `.env` in the **repo root** and fill in:

```
KALSHI_API_KEY_ID=your-key-id
KALSHI_PRIVATE_KEY_PATH=secrets.key      # relative to working directory
KALSHI_BASE_URL=https://demo-api.kalshi.co/trade-api/v2   # omit for prod
```

Run all commands from the repo root so `.env` loads correctly.

---

## Quick Start

**Option 1 — Collect data only (no quoting)**

```bash
python3 -m kalshi_ws
```

Writes `data/kalshi/ws/ticker_stream_YYYYMMDD.jsonl` and `trade_stream_YYYYMMDD.jsonl`.

**Option 2 — AS quoting in dry-run (logs actions, no real orders)**

```bash
python3 -m kalshi_as --interval 5 --execution-mode dry-run --min-spread 0.03
```

Logs `would place / would cancel` lines. Writes calibration and sample-order JSONL. No API calls.

**Option 3 — Full stack with dashboard**

Terminal 1:
```bash
python3 -m kalshi_as --interval 5 --sample-contracts 10 --execution-mode dry-run
```

Terminal 2:
```bash
streamlit run ws_dashboard/app.py
```

Open `http://localhost:8501`.

**Option 4 — Live trading (real orders)**

```bash
python3 -m kalshi_as \
  --interval 5 \
  --execution-mode live \
  --execution-contracts 1 \
  --max-markets 5 \
  --min-spread 0.04 \
  --ws-stale-s 10
```

> **Safety**: Always test with `--execution-mode dry-run` first. Use `--whitelist-tickers TICK1,TICK2` to limit exposure during canary runs. Use `--panic-cancel-all` to cancel all tagged resting orders and exit immediately.

---

## Modules

### `kalshi_ingest/` — REST Batch Ingestion

Synchronous CLI tool for pulling historical data.

```bash
# Download all open market metadata
python3 -m kalshi_ingest markets --status open --out-dir data/kalshi

# Download trades for a specific market
python3 -m kalshi_ingest trades --ticker KXNBA-26-LAL --out-dir data/kalshi

# Download orderbook snapshots
python3 -m kalshi_ingest orderbook --tickers TICK1,TICK2 --depth 10 --out-dir data/kalshi

# Check API connectivity and see sample ticker strings
python3 -m kalshi_ingest trades-sample --limit 20
```

Output: `*_raw_{timestamp}.jsonl` (raw API pages) + `*_flat_{timestamp}.csv` (one row per record). Auth via RSA-PSS signed headers.

---

### `kalshi_ws/` — WebSocket Stream

Async client that streams real-time data for all active markets.

**Channels subscribed automatically:**
- `ticker` — top-of-book bid/ask/spread/volume/OI for every market
- `trade` — every executed trade across all markets

**Optional fill channel** (requires `KALSHI_WS_ENABLE_FILLS=1`):
- `fill` — authenticated user's own fill notifications
- Persisted to `fill_stream_YYYYMMDD.jsonl`
- Drained by `consume_fills()` for the ledger pipeline

**In-memory state** (thread-safe by convention; read-only from strategy):
```python
from kalshi_ws.stream import get_market_states, get_trade_buffer, consume_fills

states = get_market_states()         # Dict[str, MarketTicker]
trades = get_trade_buffer("TICKER")  # deque[Trade]
fills  = consume_fills()             # list[Fill]  — drains the buffer
```

**Environment variables:**

| Variable | Default | Purpose |
|---|---|---|
| `KALSHI_WS_URL` | derived from `KALSHI_BASE_URL` | WebSocket endpoint |
| `KALSHI_WS_OUT_DIR` | `data/kalshi/ws` | JSONL output directory |
| `KALSHI_WS_TRADE_BUFFER` | `5000` | Max trades per market in memory |
| `KALSHI_WS_ENABLE_FILLS` | `` (disabled) | Set to `1` to subscribe to fill channel |

**Note:** The WS module signs the handshake directly at path `/trade-api/ws/v2` rather than using `KalshiAuth.sign()`, because `sign()` prepends the REST base URL which produces the wrong path for the WS endpoint.

---

### `kalshi_as/` — Avellaneda–Stoikov Strategy Engine

The main strategy package. All components are wired together in `__main__.py` and `strategy_loop.py`.

#### Model (`model.py`)

Reduced-form Avellaneda–Stoikov with inventory skew:

**Reservation price** (inventory adjustment):
```
v = mid − Q × γ × σ² × τ
```

**Optimal half-spread:**
```
h = (1/γ) × ln(1 + γ/k) + 0.5 × γ × σ² × τ
```

**Quote construction:**
- `bid = floor_to_tick(v − h)`, `ask = ceil_to_tick(v + h)`
- Market-maker-safe rounding: bids are never rounded up, asks never rounded down
- Clamped to `[0.01, 0.99]`
- Degenerate case (bid ≥ ask after clamping) collapses to one tick inside the reservation

**Config dataclass:**
```python
from kalshi_as import ASConfig, compute_quotes

cfg = ASConfig(gamma=0.05, k=1.5, tau_hours=4.0, tick=0.01, A=1.0)
q = compute_quotes(0.55, inventory_yes=10.0, sigma=0.12, config=cfg, tau_hours=2.5)
# q.bid, q.ask, q.reservation, q.half_spread, q.mid
```

`A` is a calibration placeholder (stored and logged; not yet applied to the spread formula — will be estimated from fill data via `ArrivalFitter`).

#### Volatility Estimation (`sigma.py`)

EWMA-smoothed log-return standard deviation, scaled to per-√hour units:

```
σ_hour = ewma_std(log_returns(smoothed_mids)) × √(3600 / dt)
```

- `dt` = sampling interval in seconds (default 5 s)
- Floor: 2% / √hour, cap: 500% / √hour
- Requires `min_samples` (default 12) before returning a value

#### Market Metadata Cache (`market_meta.py`)

Fetches all open markets from `GET /markets` at startup and every `refresh_s` seconds (default 300). Provides `tau_hours_for_ticker(ticker)` = hours until close. Falls back to global `--tau-hours` if ticker not found.

#### Inventory Management (`inventory.py`)

Loads per-ticker YES inventory from a JSON file:
```json
{"KXBTC-26APR01-T100000": 50, "KXNBA-26-LAL": -10}
```

If a live ledger is wired (execution mode ≠ off), **the ledger's live position overrides this file** each cycle.

#### Portfolio Ledger (`ledger.py`)

Tracks per-ticker inventory and cash from fills:
- `apply_fill(ticker, action, price_cents, count)` — updates `qty_yes` and `cash_cents`
- `qty_for_ticker(ticker)` — returns current inventory (fed back to AS model as Q)
- `snapshot_row(ticker)` — returns `mark_to_mid_pnl_cents = cash_cents + qty_yes × mid_cents`
- Persists every fill as a snapshot to `data/kalshi/as_ledger.jsonl`

#### Execution Engine (`execution.py`)

Manages resting limit orders on Kalshi. Three modes:

| Mode | Behavior |
|---|---|
| `off` | No API calls (default) |
| `dry-run` | Logs `would place / would cancel`; no API calls |
| `live` | Real `POST /portfolio/orders` and `DELETE /portfolio/orders/{id}` |

Per-cycle logic (`sync_quotes(intent)`):
1. Fetch all resting YES limit orders for the ticker tagged with `"as:"` prefix
2. If buy-side order already exists at target price with correct size → keep it
3. Otherwise cancel stale orders and place a new one
4. Same for sell side

Orders are tagged via `client_order_id` prefix `"as:"` so the engine never touches manually-placed orders.

```python
from kalshi_as.execution import ExecutionEngine, QuoteIntent

engine = ExecutionEngine(client, mode="dry-run", max_contracts=10)
msgs = engine.sync_quotes(QuoteIntent(
    market_ticker="KXNBA-26-LAL",
    yes_bid_cents=47,
    yes_ask_cents=53,
    contracts=2,
))
```

#### Execution Monitor (`execution_monitor.py`)

REST-poll-based fill detection (used when `--monitor-mode rest`):
- Polls `GET /portfolio/orders` every `--execution-monitor-poll-s` seconds (default 2 s)
- Detects **partial fills** from `remaining_count` decreasing while still resting
- Detects **full fills/cancels** when order no longer appears as resting
- Updates `PortfolioLedger` on each fill event
- Implements `ArrivalFitter`: fits `λ(δ) = A × exp(−k × δ)` from real fill data to estimate calibration parameters A and k

#### WebSocket Fill Consumer (`ws_fill_consumer.py`)

Alternative fill pipeline (used when `--monitor-mode ws`, the default):
- Polls `consume_fills()` from the WS layer every 0.5 s
- Processes YES-side buy/sell fills → ledger updates
- Persists to `data/kalshi/as_ws_fills.jsonl`
- Requires `KALSHI_WS_ENABLE_FILLS=1` to actually receive fills from the WS

#### Terminal Actions (`terminal_actions.py`)

When `tau_hours × 60 ≤ terminal_tau_minutes` (default 5 min):
- Stops computing and placing new quotes for that market
- Cancels all tagged resting orders via execution engine
- Logs a terminal record to `data/kalshi/as_terminal_actions.jsonl`:
  - `stop_quoting` — if inventory is flat
  - `flatten_intent` — if inventory is non-zero (signals manual action needed; auto-flatten not implemented)

#### Strategy Loop (`strategy_loop.py`)

Main async orchestrator. Per-cycle (`--interval`, default 5 s):

1. Optionally refresh market metadata (rate-limited)
2. Fetch live `market_states` from WebSocket
3. **WS staleness check**: if newest ticker update is older than `--ws-stale-s` (default 10 s), cancel all tagged orders and pause this cycle
4. **Whitelist filter**: skip tickers not in `--whitelist-tickers` (if set)
5. Filter by `spread ≥ min_spread`, sort descending, take top candidates
6. For each candidate (up to `--max-markets`, default 12):
   - Append mid to rolling history (deque of `--mid-history` length, default 80)
   - Estimate σ (skip if not enough samples)
   - Resolve inventory: ledger (if live) > inventory file > `--inventory` global
   - Resolve τ: `market_meta` per-ticker or `--tau-hours` global
   - **Terminal check**: cancel + log if near expiry
   - Compute AS quotes
   - **Execution sync**: if mode ≠ off and resting order count < `--max-tagged-resting-orders` (default 20), sync resting orders to model bid/ask
   - Feed execution events to monitor
   - Append calibration + sample-order records
7. Log all quote lines to console

---

### `ws_dashboard/` — Streamlit Dashboard

```bash
streamlit run ws_dashboard/app.py   # opens at http://localhost:8501
```

Reads JSONL files written by the WebSocket and AS strategy. Refreshes every 2 s (configurable).

**Panels:**

| Panel | Description |
|---|---|
| WebSocket status | Trade/ticker file freshness (Receiving / Stale) |
| Viable markets (MM) | 5-factor market-making score for all tracked markets |
| AS sample orders | Per-market model quotes, σ, γ, k, τ, inventory, gross edge |
| Recent trades | Parsed from `trade_stream_*.jsonl` |
| Trade execution | Manual limit/market order placement form |
| Active orders | All resting orders; Cancel and Sell-market buttons per order |

**MM scoring factors** (weights configurable in sidebar):
1. Spread quality — mean book spread vs assumed round-trip fee + spread CV stability
2. Fill / flow — trades-per-hour, boosted for two-sided taker flow
3. Adverse selection — lag-1 return autocorrelation proxy (negative = mean-reverting)
4. Volume / depth — log-scaled contracts, dollar volume, open interest
5. Vol stability — prefers moderate short-horizon volatility; blends in AS σ when available

**Sidebar controls:** min spread, mid bounds (filter settled markets), fee assumption, max rows, auto-refresh interval, score weights, ticker file tail size.

---

## CLI Reference — `kalshi_as`

```bash
python3 -m kalshi_as [OPTIONS]
```

### Core model parameters

| Flag | Default | Description |
|---|---|---|
| `--interval` | 5.0 | Cycle frequency (seconds) |
| `--gamma` | 0.05 | Risk aversion coefficient |
| `--k` | 1.5 | Order-arrival intensity |
| `--tau-hours` | 4.0 | Global time horizon fallback |
| `--A` | 1.0 | Calibration parameter A (placeholder) |
| `--tick` | 0.01 | Price tick size (dollars) |

### Market selection

| Flag | Default | Description |
|---|---|---|
| `--min-spread` | 0.02 | Minimum YES spread to consider |
| `--max-markets` | 12 | Max markets per cycle |
| `--whitelist-tickers` | `` | Comma-separated allowlist (empty = all) |
| `--market-meta-refresh-s` | 300.0 | REST refresh interval for tau |

### Volatility

| Flag | Default | Description |
|---|---|---|
| `--mid-history` | 80 | Rolling mid buffer per market |
| `--sigma-min-samples` | 12 | Warmup samples before σ is valid |

### Inventory

| Flag | Default | Description |
|---|---|---|
| `--inventory` | 0.0 | Global YES inventory (contracts) |
| `--inventory-file` | `` | Per-ticker JSON file |

### Execution

| Flag | Default | Description |
|---|---|---|
| `--execution-mode` | `off` | `off` / `dry-run` / `live` |
| `--execution-contracts` | 1 | Contracts per side |
| `--execution-max-contracts` | 10 | Hard cap per order |
| `--max-tagged-resting-orders` | 20 | Safety cap on total resting orders |
| `--panic-cancel-all` | — | Cancel all tagged orders and exit |

### Monitoring & fill tracking

| Flag | Default | Description |
|---|---|---|
| `--monitor-mode` | `ws` | `ws` (WS fills) or `rest` (REST poll) |
| `--execution-monitor-poll-s` | 2.0 | REST poll interval (rest mode) |
| `--ws-stale-s` | 10.0 | WS staleness threshold for kill switch |
| `--assumed-fee-cents` | 0.0 | Fee per contract for cash accounting |

### Terminal / expiry

| Flag | Default | Description |
|---|---|---|
| `--terminal-tau-minutes` | 5.0 | Stop quoting threshold before expiry |

### Output paths

| Flag | Default |
|---|---|
| `--sample-contracts` | 0 (disabled) |
| `--sample-orders-file` | `data/kalshi/as_sample_orders.jsonl` |
| `--calibration-log-file` | `data/kalshi/as_calibration.jsonl` |
| `--execution-events-file` | `data/kalshi/as_execution_events.jsonl` |
| `--ledger-file` | `data/kalshi/as_ledger.jsonl` |
| `--terminal-actions-file` | `data/kalshi/as_terminal_actions.jsonl` |

---

## Data Files

### WebSocket output (`data/kalshi/ws/`)

| File | Contents |
|---|---|
| `ticker_stream_YYYYMMDD.jsonl` | Top-of-book updates for all markets |
| `trade_stream_YYYYMMDD.jsonl` | All executed trades |
| `fill_stream_YYYYMMDD.jsonl` | User fills (only with `KALSHI_WS_ENABLE_FILLS=1`) |

### Strategy output (`data/kalshi/`)

| File | Contents |
|---|---|
| `as_sample_orders.jsonl` | Hypothetical orders at model bid/ask (if `--sample-contracts > 0`) |
| `as_calibration.jsonl` | Model params + mid + bid/ask per market per cycle |
| `as_execution_events.jsonl` | place/cancel/keep events from execution engine |
| `as_ledger.jsonl` | Per-ticker inventory + MTM P&L snapshots on each fill |
| `as_ws_fills.jsonl` | WS fill events consumed by the ledger |
| `as_terminal_actions.jsonl` | Near-expiry cancel + flatten-intent records |

### REST ingestion output (`data/kalshi/`)

| File | Contents |
|---|---|
| `markets_raw_{ts}.jsonl` + `markets_flat_{ts}.csv` | Market metadata |
| `trades_raw_{ts}.jsonl` + `trades_flat_{ts}.csv` | Historical trades |
| `orderbook_raw_{ts}.jsonl` + `orderbook_flat_{ts}.csv` | Orderbook snapshots |

---

## Tests

**15 test files, ~50 tests.** Run with:

```bash
python3 -m pytest tests/ -v
```

| File | Coverage |
|---|---|
| `test_ws_models.py` | MarketTicker / Trade / Fill parsing, spreads, deque bounds |
| `test_ws_fill_model.py` | Fill model parsing edge cases |
| `test_ws_fill_consumer.py` | WS fill → ledger pipeline |
| `test_as_ws_stale.py` | WS staleness detection |
| `test_as_market_meta.py` | Tau calculation, refresh throttling |
| `test_as_sigma.py` | EWMA volatility, warmup, floor/cap |
| `test_avellaneda_stoikov.py` | Quote computation, inventory skew, edge cases |
| `test_as_inventory.py` | Inventory file loading |
| `test_as_ledger.py` | Inventory + cash + MTM P&L accounting |
| `test_as_execution.py` | Dry-run vs live, keep/cancel/place logic |
| `test_as_execution_monitor.py` | Fill tracking, partial fills, terminal status |
| `test_as_cancel_all_quotes.py` | Tagged order cancel (live + dry-run) |
| `test_as_terminal_actions.py` | Terminal record format |
| `test_sample_orders.py` | Sample order JSONL format |
| `test_calibration_log.py` | Calibration record format |

Not tested (infrastructure):
- WebSocket connection and backoff
- REST client pagination
- Disk I/O persistence
- Strategy loop integration (end-to-end)

---

## Project Structure

```
quant-pod-c/
├── README.md / UPDATED_README.md
├── config.json              ← ORPHANED (Tier-1/2 params, no code reads this)
├── requirements.txt
├── .env / .env.example
├── secrets.key
│
├── kalshi_ingest/
│   ├── __main__.py
│   ├── cli.py
│   ├── auth.py
│   ├── client.py            ← also implements create_order, get_orders, cancel_order
│   ├── ingest.py
│   └── save.py
│
├── kalshi_ws/
│   ├── __main__.py
│   ├── stream.py            ← ticker + trade + fill channels, consume_fills()
│   ├── models.py            ← MarketTicker, Trade, Fill dataclasses
│   └── __init__.py
│
├── kalshi_as/
│   ├── __main__.py          ← CLI entrypoint (~30 args)
│   ├── model.py             ← ASConfig, compute_quotes()
│   ├── sigma.py             ← EWMA volatility estimation
│   ├── market_meta.py       ← per-ticker tau cache
│   ├── inventory.py         ← static inventory load
│   ├── ledger.py            ← live inventory + MTM P&L
│   ├── execution.py         ← place/cancel engine (off/dry-run/live)
│   ├── execution_monitor.py ← REST fill poll + ArrivalFitter
│   ├── ws_fill_consumer.py  ← WS fill → ledger
│   ├── terminal_actions.py  ← near-expiry cancel logic
│   ├── strategy_loop.py     ← main async orchestrator
│   ├── sample_orders.py     ← hypothetical order JSONL
│   ├── calibration_log.py   ← model params JSONL
│   └── __init__.py
│
├── kalshi_filter/           ← EMPTY (ghost directory, unused)
│
├── ws_dashboard/
│   └── app.py               ← Streamlit dashboard + trading panel
│
├── tests/                   ← 15 test files, ~50 tests
│
├── data/kalshi/
│   ├── ws/                  ← ticker/trade/fill JSONL streams
│   └── *.jsonl              ← strategy output files
│
└── docs/
    ├── FUNCTIONS.md
    └── PROJECT.md
```

---

## Technical Notes

- **WS auth**: The handshake is signed directly at path `/trade-api/ws/v2`, bypassing `KalshiAuth.sign()` which prepends the REST base URL and produces the wrong path.
- **Numeric parsing**: All WebSocket and REST values arrive as strings. `_parse_float()` handles `str | int | float`; downstream code works with floats throughout.
- **Async/sync split**: `kalshi_ingest` is synchronous. `kalshi_ws` and `kalshi_as` are async. REST calls from the strategy loop use `asyncio.to_thread()` to avoid blocking.
- **Concurrent access**: `_market_states` is returned by reference with no locks. The strategy loop reads only; safe by convention. Do not write from multiple threads.
- **Stale data guard**: If the WS disconnects, `last_update_ts` on `MarketTicker` ages out. The staleness check in the strategy loop (`--ws-stale-s`) cancels tagged orders and pauses quoting until fresh data resumes.
- **No sequence gap detection**: The `seq` field on WS messages is not currently tracked. Gaps indicating missed messages are silently ignored.
- **No graceful shutdown**: The WS stream runs until `Ctrl+C`. No signal handlers clean up resting orders on exit — use `--panic-cancel-all` on the next startup if needed.
- **fill channel activation**: The `fill` channel subscription in `kalshi_ws` is gated by `KALSHI_WS_ENABLE_FILLS=1`. Without this, `consume_fills()` always returns empty and `ws_fill_consumer` does nothing. REST monitor mode (`--monitor-mode rest`) works without this env var.
