## Function reference

This document describes the main functions/classes exposed by each package and what they return.

---

### `kalshi_ingest.auth`

#### `KalshiAuth.from_env() -> KalshiAuth`
- **Purpose**: Build auth/config from environment variables.
- **Reads**:
  - `KALSHI_API_KEY_ID` (required)
  - `KALSHI_PRIVATE_KEY_PATH` (required)
  - `KALSHI_BASE_URL` (optional, defaults to demo)
- **Returns**: `KalshiAuth`
- **Raises**: `ValueError` if required vars are missing.

#### `KalshiAuth.load_private_key() -> Any`
- **Purpose**: Load the RSA private key from `private_key_path`.
- **Returns**: A `cryptography` private key object.

#### `KalshiAuth.sign(private_key, timestamp_ms: str, method: str, endpoint_path: str) -> str`
- **Purpose**: Create the base64 signature used by Kalshi REST auth headers.
- **Inputs**:
  - `timestamp_ms`: milliseconds since epoch as a string
  - `method`: HTTP method (e.g. `GET`)
  - `endpoint_path`: endpoint path relative to `base_url` (e.g. `/markets`)
- **Returns**: base64-encoded signature string.
- **Note**: This method prepends `base_url` to the path before signing. For the WebSocket endpoint (`/trade-api/ws/v2`), this produces the wrong path. `kalshi_ws` signs directly instead.

### `kalshi_ingest.client`

#### `KalshiClient.get(endpoint_path: str, params: dict | None = None) -> dict`
- **Purpose**: Perform a single authenticated `GET` request.
- **Returns**: Parsed JSON response as a Python dict.
- **Raises**: `requests.HTTPError` for non-2xx responses.

#### `KalshiClient.paginate(...) -> Iterator[tuple[dict, str]]`
- **Purpose**: Iterate a cursor-paginated endpoint.
- **Yields**:
  - `page_json`: the response JSON dict for the page
  - `cursor_used`: the cursor value that was sent for that request (empty string for first page)
- **Stops**: when the response cursor field is empty.
- **Key parameters**:
  - `limit`: page size (default `1000`)
  - `cursor_field`: response field containing the next cursor (default `cursor`)

#### `KalshiClient.now_utc_iso() -> str`
- **Purpose**: Convenience timestamp for output records.
- **Returns**: ISO 8601 UTC timestamp string.

### `kalshi_ingest.save`

#### `ensure_dir(path: str | Path) -> Path`
- **Purpose**: Create a directory (and parents) if needed.
- **Returns**: `Path` object for the directory.

#### `write_jsonl(path: str | Path, rows: Iterable[dict]) -> None`
- **Purpose**: Write newline-delimited JSON.

#### `try_write_csv(path: str | Path, records: list[dict]) -> str | None`
- **Purpose**: Write a CSV via pandas if available.
- **Returns**:
  - `None` on success
  - error string if pandas import/write failed

#### `atomic_write_text(path: str | Path, text: str) -> None`
- **Purpose**: Write a text file atomically using a temporary file + replace.

### `kalshi_ingest.ingest`

#### `ingest_markets(client: KalshiClient, out_dir: str | Path, ...) -> IngestResult`
- **Purpose**: Download all pages from `GET /markets`, save raw JSONL and flattened CSV.
- **Output files**:
  - `markets_raw_<timestamp>.jsonl`
  - `markets_flat_<timestamp>.csv` (or a `.txt` note if CSV write fails)
- **Filters** (optional): `status`, `series_ticker`, `event_ticker`, `tickers`
- **Used by**: `kalshi_filter/metadata.py` calls this with `status="open"` to bootstrap market metadata.

#### `ingest_trades(client: KalshiClient, out_dir: str | Path, ...) -> IngestResult`
- **Purpose**: Download all pages from `GET /markets/trades`, save raw JSONL and flattened CSV.
- **Output files**:
  - `trades_raw_<timestamp>.jsonl`
  - `trades_flat_<timestamp>.csv` (or a `.txt` note if CSV write fails)
- **Filters** (optional): `ticker`, `min_ts`, `max_ts`
- **Ticker**: Use the exact ticker from the API (uppercase, including suffix e.g. `-EWU`). Use `trades-sample` to see the format.

#### `fetch_trades_sample(client: KalshiClient, limit: int = 10) -> dict`
- **Purpose**: Fetch one page of trades with no ticker filter. Used to check the API and to see sample ticker strings.
- **Returns**: Dict with `count`, `cursor`, `tickers_sample` (list of ticker strings), and `raw_page`.

#### `ingest_orderbook(client: KalshiClient, out_dir: str | Path, tickers: List[str], *, depth: Optional[int] = None) -> IngestResult`
- **Purpose**: Download orderbook snapshots from `GET /markets/{ticker}/orderbook` for each ticker in the list.
- **Output files**:
  - `orderbook_raw_<timestamp>.jsonl` — one JSON object per ticker with the full API response
  - `orderbook_flat_<timestamp>.csv` — one row per price level (or a `.txt` note if CSV write fails)
- **Flat CSV columns**: `fetched_at`, `ticker`, `side` (`yes`/`no`), `price_cents`, `quantity`, `level_index`
- **Parameters**:
  - `tickers` (required): list of market ticker strings
  - `depth` (optional): number of price levels per side; omit for full depth
- **Error handling**: If a ticker fails, it is skipped and listed in `IngestResult.note`. The rest continue.

#### `IngestResult`
- **Fields**:
  - `raw_jsonl_path`: path to the JSONL file
  - `flat_csv_path`: path to the CSV file (or `None` if CSV write failed)
  - `note`: optional error/note string

### `kalshi_ingest.cli`

#### `main() -> int`
- **Purpose**: Parse CLI args and run a subcommand (`markets`, `trades`, `trades-sample`, `orderbook`).
- **Returns**: process exit code.

---

### `kalshi_ws.models`

#### `MarketTicker`
- **Purpose**: In-memory representation of a market's latest ticker state from the WebSocket.
- **Fields**:
  - `market_ticker: str` — unique market identifier
  - `yes_bid: float` — best bid price (parsed from `yes_bid_dollars` string)
  - `yes_ask: float` — best ask price (parsed from `yes_ask_dollars` string)
  - `spread: float` — derived: `yes_ask - yes_bid`
  - `last_price: float` — last traded price
  - `volume: float` — total contracts traded
  - `open_interest: float` — open interest
  - `dollar_volume: int` — total dollar volume
  - `dollar_open_interest: int` — dollar open interest
  - `last_update_ts: int` — unix timestamp of last update
  - `update_count: int` — incremented on every ticker update (useful for activity measurement)
- **Class methods**:
  - `from_msg(msg: dict) -> MarketTicker` — construct from a raw WS ticker message
- **Instance methods**:
  - `update(msg: dict) -> None` — merge a partial ticker update, recalculate spread

#### `Trade`
- **Purpose**: A single trade from the WebSocket trade channel.
- **Fields**:
  - `trade_id: str`, `market_ticker: str`
  - `yes_price: float`, `no_price: float` — parsed from dollar strings
  - `size: float` — contracts traded (parsed from `count_fp` string)
  - `taker_side: str` — `"yes"` or `"no"`
  - `ts: int` — unix timestamp
- **Class methods**:
  - `from_msg(msg: dict) -> Trade` — construct from a raw WS trade message

#### `_parse_float(val) -> float`
- **Purpose**: Parse a value that may be a string, int, or float to float. Returns `0.0` for unrecognized types.
- **Used at**: the message boundary to convert all WS numeric strings.

### `kalshi_ws.stream`

#### `run_ws_stream(base_url: str | None, out_dir: str, trade_buffer_size: int) -> None`
- **Purpose**: Main async entry point. Connects to Kalshi WS, subscribes to ticker + trade channels, processes messages indefinitely.
- **Parameters**:
  - `base_url`: WebSocket URL or REST base URL. Falls back to `KALSHI_BASE_URL` env var.
  - `out_dir`: directory for raw JSONL stream files (default `data/kalshi/ws`)
  - `trade_buffer_size`: max trades per market in memory (default 5000)
- **Behavior**: Reconnects with exponential backoff (1s → 60s cap). Resubscribes on reconnect.
- **Used by**: `kalshi_filter/__main__.py` starts this as an `asyncio.create_task`.

#### `get_market_states() -> Dict[str, MarketTicker]`
- **Purpose**: Return the live market-state dict. Updated on every ticker message.
- **Thread safety**: Read-only by convention. Single writer (WS message loop), multiple readers (filter, analysis).

#### `get_trade_buffer(market_ticker: str) -> deque[Trade]`
- **Purpose**: Return the trade deque for a given market. Empty deque if the market hasn't been seen.
- **Max length**: Configurable via `trade_buffer_size` (default 5000).

#### `get_subscription_ids() -> Dict[str, int]`
- **Purpose**: Return the mapping of channel name → server-assigned subscription ID (sid).
- **Used later**: For `update_subscription` commands when adding/removing orderbook tickers.

---

### `kalshi_filter.config`

#### `load_config(path: str | Path = "config.json") -> Config`
- **Purpose**: Load configuration from a JSON file into typed dataclasses.
- **Behavior**:
  - File missing → use all defaults, write default config file for reference
  - Key missing → use default for that key, log a warning
  - Wrong type → raise `TypeError` immediately with a clear message
- **Returns**: `Config` with sections: `tier1`, `cooldowns`, `evaluation`, `metadata`, `websocket`, `paths`

#### `write_default_config(path: str | Path = "config.json") -> None`
- **Purpose**: Write a JSON file containing all default parameter values.

#### `config_summary(cfg: Config) -> str`
- **Purpose**: Return a human-readable multi-line string of all config values (for startup logging).

#### Config dataclasses

| Class | Key fields |
|-------|-----------|
| `Tier1Config` | `min_spread`, `max_spread`, `max_confidence_threshold`, `min_confidence_threshold`, `min_dollar_volume`, `min_update_rate`, `min_expiry_seconds`, `max_imbalance_deviation`, `min_trades_for_imbalance`, `consecutive_passes_required`, `consecutive_fails_allowed` |
| `CooldownConfig` | `first_demotion_seconds`, `second_demotion_seconds`, `max_demotions_before_blacklist` |
| `EvalConfig` | `interval_seconds` |
| `MetadataConfig` | `refresh_interval_seconds` |
| `WebSocketConfig` | `trade_buffer_size` |
| `PathsConfig` | `ws_out_dir`, `transition_log_dir`, `metadata_out_dir` |

### `kalshi_filter.metadata`

#### `MarketMetadata`
- **Purpose**: Static/slow-changing market info from the REST API. Separate from `MarketTicker` (fast-changing WS data).
- **Fields**:
  - `ticker: str` — market identifier
  - `expiration_time: float` — unix timestamp of market expiration
  - `status: str` — `"open"`, `"closed"`, `"settled"`, etc.
  - `event_ticker: str` — parent event identifier
  - `result: str` — empty string if unresolved, `"yes"`/`"no"` if settled
  - `close_time: float` — unix timestamp of market close
- **Class methods**:
  - `from_api_dict(d: dict) -> MarketMetadata` — construct from a raw Kalshi API market dict (parses ISO timestamps to unix floats)

#### `refresh_metadata(config: Config) -> int`
- **Purpose**: Fetch all open markets via REST (in a background thread), replace the global metadata dict.
- **Returns**: Number of markets loaded.
- **Async**: Yes — wraps sync `ingest_markets` in `asyncio.to_thread`.

#### `get_market_metadata() -> Dict[str, MarketMetadata]`
- **Purpose**: Return the full metadata dict (read-only by convention).

#### `get_metadata(ticker: str) -> Optional[MarketMetadata]`
- **Purpose**: Return metadata for a single ticker, or `None` if not in the dict.

### `kalshi_filter.filter`

#### `MarketTracker`
- **Purpose**: Per-market lifecycle tracking state.
- **Fields**:
  - `ticker: str`, `status: str` — one of `IGNORED`, `WATCHING`, `DEMOTED`, `BLACKLISTED`
  - `consecutive_passes: int`, `consecutive_fails: int`
  - `demoted_at: float | None`, `demotion_count: int`, `cooldown_until: float | None`
  - `last_eval_result: str`, `last_eval_time: float`, `promoted_at: float | None`
  - `prev_update_count: int`, `prev_eval_time: float` — for computing update rate between evals

#### `evaluate_market(market_state, trade_buffer, metadata, config, update_rate, now) -> tuple[bool, str]`
- **Purpose**: Evaluate a single market against all Tier 1 criteria. Pure logic, no side effects.
- **Returns**: `(True, "PASS")` or `(False, "reason string")`
- **Checks** (in order):
  1. No quotes (bid=0 or ask=0)
  2. Spread bounds
  3. Decided (bid too high or ask too low)
  4. Expiry (skipped if no metadata)
  5. Volume
  6. Activity (skipped if no rate available — first eval)
  7. Imbalance (skipped if fewer trades than `min_trades_for_imbalance`)

#### `run_evaluation(config: Config, on_transition=None) -> dict`
- **Purpose**: Run one full evaluation cycle across all markets in `market_states`.
- **Parameters**:
  - `on_transition`: optional callback `(ticker, old_status, new_status, tracker, metrics)` — called on every status change
- **Returns**: Summary dict `{"total": N, "IGNORED": N, "WATCHING": N, "DEMOTED": N, "BLACKLISTED": N}`
- **Side effects**: Updates the global `_trackers` dict.

#### `compute_metrics(market_state, trade_buffer, metadata, update_rate, now) -> dict`
- **Purpose**: Build a summary metrics dict for transition logging.
- **Returns**: Dict with keys `spread`, `dollar_volume`, `yes_bid`, `yes_ask`, `imbalance`, `update_rate`, `time_to_expiry`.

#### `get_candidates() -> List[str]`
- **Purpose**: Return tickers with status `WATCHING` — these are the Tier 1 candidates.

#### `get_market_status(ticker: str) -> Optional[str]`
- **Purpose**: Return the lifecycle status for a ticker.

#### `get_all_trackers() -> Dict[str, MarketTracker]`
- **Purpose**: Return the full tracker dict (for debugging/logging).

### `kalshi_filter.transitions`

#### `TransitionLogger(log_dir: str | Path)`
- **Purpose**: Logs market status transitions to console (formatted) and to a daily JSONL file.
- **File**: `{log_dir}/transitions_YYYYMMDD.jsonl`

#### `TransitionLogger.log_transition(ticker, old_status, new_status, tracker, metrics) -> None`
- **Purpose**: Log a single status change. Prints formatted line to console, appends JSONL record to disk (via `asyncio.to_thread`).
- **JSONL record fields**: `ts`, `ticker`, `old_status`, `new_status`, `consecutive_passes`, `demotion_count`, `metrics`

#### `TransitionLogger.log_eval_summary(summary: dict) -> None`
- **Purpose**: Print end-of-cycle summary. Example: `EVAL: 347 markets | 12 WATCHING | 3 DEMOTED | 2 BLACKLISTED | 330 IGNORED`
