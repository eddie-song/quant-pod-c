## Function reference

This document describes the main functions/classes exposed by the package and what they return.

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

