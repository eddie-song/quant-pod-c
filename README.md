# quant-pod-c: Kalshi data ingestion (simple)

This folder contains a small Kalshi REST ingestion utility to download data locally for EDA.

## Setup

Create a virtualenv, then:

```bash
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill:

- `KALSHI_API_KEY_ID`
- `KALSHI_PRIVATE_KEY_PATH` (path to the downloaded `.key`)
- `KALSHI_BASE_URL` (defaults to demo)

## Commands

All commands save:

- **raw** pages as `.jsonl` (one JSON object per line)
- **flat** tables as `.csv` for quick EDA

### Download markets

```bash
python -m kalshi_ingest.cli markets --out-dir data/kalshi
```

Optional filters (passed through to Kalshi `GET /markets`):

```bash
python -m kalshi_ingest.cli markets --status open --limit 500 --out-dir data/kalshi
```

### Download trades

```bash
python -m kalshi_ingest.cli trades --out-dir data/kalshi
```

Filter to a ticker and/or time window (unix seconds):

```bash
python -m kalshi_ingest.cli trades --ticker SOME-TICKER --min-ts 1700000000 --max-ts 1710000000 --out-dir data/kalshi
```

## Notes

- Auth follows Kalshi docs: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP` (ms), `KALSHI-ACCESS-SIGNATURE` (RSA-PSS SHA256, base64).
- This is intentionally minimal; we can add richer schemas/Parquet later once you’re happy with the basics.

