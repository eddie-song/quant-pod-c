## Project overview

`quant-pod-c` is a small Kalshi REST ingestion utility meant to download data locally for exploratory analysis.

It currently supports:
- `GET /markets` to collect market metadata
- `GET /markets/trades` to collect executed trades

Outputs are written to a local folder (default `data/kalshi/`) as:
- raw paginated responses in `*.jsonl`
- flattened tables in `*.csv`

## Folder layout

- `requirements.txt`
  - Python dependencies.
- `.env.example`
  - Template for environment variables required for authentication.
- `kalshi_ingest/`
  - Python package implementing auth, HTTP client, ingestion logic, and CLI.
- `docs/`
  - Documentation for the project and function reference.

## How it works (module interactions)

- `kalshi_ingest.auth.KalshiAuth`
  - Reads configuration from environment variables.
  - Loads the Kalshi RSA private key.
  - Creates request signatures used for authenticated REST calls.

- `kalshi_ingest.client.KalshiClient`
  - Wraps `requests` with Kalshi authentication headers.
  - Provides `get()` for single REST calls.
  - Provides `paginate()` to iterate cursor-based endpoints and yield full pages.

- `kalshi_ingest.ingest`
  - Calls `KalshiClient.paginate()` for specific endpoints.
  - Writes raw pages to JSONL and flattens list payloads into CSV.

- `kalshi_ingest.save`
  - Small filesystem helpers used by ingestion functions.

- `kalshi_ingest.cli`
  - Command-line entry point.
  - Loads `.env` (or a provided env file), constructs the client, runs ingestion, prints paths.

## Setup

From `quant-pod-c/`:

```bash
pip install -r requirements.txt
```

Create `.env` in `quant-pod-c/` (copy from `.env.example`) and set:
- `KALSHI_API_KEY_ID`
- `KALSHI_PRIVATE_KEY_PATH`
- `KALSHI_BASE_URL` (demo or prod base URL ending with `/trade-api/v2`)

## Run

From `quant-pod-c/`:

```bash
python -m kalshi_ingest markets --out-dir data/kalshi
python -m kalshi_ingest trades --out-dir data/kalshi
```

Common options:
- `--env-file PATH`: load env vars from a specific file
- `--out-dir PATH`: output folder for saved data
- `markets`: `--status`, `--series-ticker`, `--event-ticker`, `--tickers`, `--limit`
- `trades`: `--ticker`, `--min-ts`, `--max-ts`, `--limit`

