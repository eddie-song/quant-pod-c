from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import websockets

from kalshi_as.inventory import get_inventory_state
from kalshi_ingest.auth import KalshiAuth

from .models import MarketTicker, Trade

logger = logging.getLogger(__name__)

# ── WebSocket URL helpers ─────────────────────────────────────────────
_WS_URLS = {
    "demo": "wss://demo-api.kalshi.co/trade-api/ws/v2",
    "prod": "wss://api.elections.kalshi.com/trade-api/ws/v2",
}
_WS_SIGN_PATH = "/trade-api/ws/v2"

_RECONNECT_ERRORS = {9, 17}  # auth required, internal error


def _ws_url_from_env_or_param(base_url: Optional[str] = None) -> str:
    """Derive the WebSocket URL.

    Accepts either an explicit URL (``wss://…``) or the REST base URL stored in
    ``KALSHI_BASE_URL`` and converts it to the corresponding WS endpoint.
    """
    url = (base_url or "").strip()
    if url.startswith("wss://"):
        return url
    if "demo" in url:
        return _WS_URLS["demo"]
    if "elections" in url or "api.kalshi.com" in url:
        return _WS_URLS["prod"]
    # Default to demo
    return _WS_URLS["demo"]


# ── Auth header builder ───────────────────────────────────────────────

def _build_auth_headers(auth: KalshiAuth) -> Dict[str, str]:
    """Build the three auth headers required for the WS handshake.

    We call ``KalshiAuth.load_private_key()`` and the low-level RSA sign
    directly because ``KalshiAuth.sign()`` prepends ``self.base_url`` to the
    path, which produces the wrong signature for the WebSocket endpoint
    (``/trade-api/ws/v2`` vs ``/trade-api/v2/…``).
    """
    import base64
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding as _padding

    private_key = auth.load_private_key()
    timestamp_ms = str(int(time.time() * 1000))
    message = f"{timestamp_ms}GET{_WS_SIGN_PATH}".encode("utf-8")

    signature = private_key.sign(
        message,
        _padding.PSS(
            mgf=_padding.MGF1(hashes.SHA256()),
            salt_length=_padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    sig_b64 = base64.b64encode(signature).decode("utf-8")

    return {
        "KALSHI-ACCESS-KEY": auth.api_key_id,
        "KALSHI-ACCESS-SIGNATURE": sig_b64,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
    }


# ── Persistence (buffered, non-blocking) ──────────────────────────────

class _DiskWriter:
    """Buffers raw JSON messages and flushes to disk periodically.

    Writes happen in a background thread via ``asyncio.to_thread`` so the
    message-processing loop is never blocked by I/O.
    """

    def __init__(self, out_dir: str | Path, flush_interval: float = 2.0, flush_size: int = 200) -> None:
        self._out_dir = Path(out_dir)
        self._out_dir.mkdir(parents=True, exist_ok=True)
        self._flush_interval = flush_interval
        self._flush_size = flush_size

        date_tag = datetime.now(timezone.utc).strftime("%Y%m%d")
        self._paths: Dict[str, Path] = {
            "ticker": self._out_dir / f"ticker_stream_{date_tag}.jsonl",
            "trade": self._out_dir / f"trade_stream_{date_tag}.jsonl",
        }
        self._buffers: Dict[str, list] = {"ticker": [], "trade": []}
        self._lock = asyncio.Lock()
        self._running = False

    async def start(self) -> None:
        self._running = True
        asyncio.get_event_loop().create_task(self._flush_loop())

    async def stop(self) -> None:
        self._running = False
        await self._flush_all()

    def enqueue(self, channel: str, raw_message: str) -> None:
        buf = self._buffers.get(channel)
        if buf is not None:
            buf.append(raw_message)

    async def _flush_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._flush_interval)
            await self._flush_all()

    async def _flush_all(self) -> None:
        async with self._lock:
            for channel in list(self._buffers):
                buf = self._buffers[channel]
                if not buf:
                    continue
                lines = buf.copy()
                buf.clear()
                path = self._paths[channel]
                try:
                    await asyncio.to_thread(self._write_lines, path, lines)
                except Exception:
                    logger.exception("Disk write failed for %s (%d lines dropped)", channel, len(lines))

    @staticmethod
    def _write_lines(path: Path, lines: list) -> None:
        with path.open("a", encoding="utf-8") as f:
            for line in lines:
                f.write(line if line.endswith("\n") else line + "\n")


# ── In-memory state ──────────────────────────────────────────────────

_market_states: Dict[str, MarketTicker] = {}
_trade_buffers: Dict[str, deque] = {}
_subscription_ids: Dict[str, int] = {}
_trade_buffer_maxlen: int = 5000


def get_market_states() -> Dict[str, MarketTicker]:
    """Return the live market-state dict (read-only by convention)."""
    return _market_states


def get_trade_buffer(market_ticker: str) -> deque:
    """Return the trade deque for a given market (empty deque if unseen)."""
    return _trade_buffers.get(market_ticker, deque(maxlen=_trade_buffer_maxlen))


def get_subscription_ids() -> Dict[str, int]:
    """Return the mapping of channel name → server-assigned subscription id."""
    return _subscription_ids


# ── Message handlers ─────────────────────────────────────────────────

def _handle_ticker(msg: dict) -> None:
    ticker = msg.get("market_ticker", "")
    if not ticker:
        return
    existing = _market_states.get(ticker)
    if existing is None:
        _market_states[ticker] = MarketTicker.from_msg(msg)
    else:
        existing.update(msg)


def _handle_trade(msg: dict) -> None:
    trade = Trade.from_msg(msg)
    ticker = trade.market_ticker
    if not ticker:
        return
    buf = _trade_buffers.get(ticker)
    if buf is None:
        buf = deque(maxlen=_trade_buffer_maxlen)
        _trade_buffers[ticker] = buf
    buf.append(trade)


def _handle_fill(msg: dict) -> None:
    get_inventory_state().apply_fill(msg)


def _handle_subscribed(data: dict) -> None:
    msg = data.get("msg", {})
    channel = msg.get("channel", "")
    sid = msg.get("sid")
    if channel and sid is not None:
        _subscription_ids[channel] = sid
        logger.info("Subscribed to %s (sid=%s)", channel, sid)


def _handle_error(data: dict) -> None:
    msg = data.get("msg", {})
    code = msg.get("code", -1)
    text = msg.get("msg", "")
    logger.error("WS error code=%s: %s", code, text)


# ── Subscribe helper ─────────────────────────────────────────────────

async def _subscribe(ws: Any, cmd_id_start: int = 1) -> int:
    """Send subscribe commands for ticker and trade channels.  Returns next cmd id."""
    cmd_id = cmd_id_start
    for channel in ("ticker", "trade", "fill"):
        payload = {"id": cmd_id, "cmd": "subscribe", "params": {"channels": [channel]}}
        await ws.send(json.dumps(payload))
        logger.info("Sent subscribe cmd id=%d channel=%s", cmd_id, channel)
        cmd_id += 1
    return cmd_id


# ── Core connection + message loop ───────────────────────────────────

async def run_ws_stream(
    base_url: Optional[str] = None,
    out_dir: str = "data/kalshi/ws",
    trade_buffer_size: int = 5000,
) -> None:
    """Connect to Kalshi WS, subscribe, and stream messages indefinitely.

    Parameters
    ----------
    base_url:
        WebSocket URL (``wss://…``) or REST base URL from env.  Falls back to
        ``KALSHI_BASE_URL`` env var via ``KalshiAuth.from_env()``.
    out_dir:
        Directory for raw JSONL stream files.
    trade_buffer_size:
        Max trades retained per market in the in-memory deque.
    """
    global _trade_buffer_maxlen
    _trade_buffer_maxlen = trade_buffer_size

    auth = KalshiAuth.from_env()
    ws_url = _ws_url_from_env_or_param(base_url or auth.base_url)

    writer = _DiskWriter(out_dir)
    await writer.start()

    backoff = 1.0
    max_backoff = 60.0
    cmd_id = 1

    while True:
        try:
            headers = _build_auth_headers(auth)
            logger.info("Connecting to %s …", ws_url)

            async with websockets.connect(ws_url, additional_headers=headers) as ws:
                logger.info("Connected.")
                backoff = 1.0  # reset on success
                cmd_id = await _subscribe(ws, cmd_id)

                async for raw_message in ws:
                    try:
                        data = json.loads(raw_message)
                    except json.JSONDecodeError:
                        logger.warning("Non-JSON message: %s", raw_message[:200])
                        continue

                    msg_type = data.get("type")

                    if msg_type == "ticker":
                        try:
                            _handle_ticker(data.get("msg", {}))
                        except Exception:
                            logger.exception("Failed to process ticker msg: %s", raw_message[:300])
                        writer.enqueue("ticker", raw_message)

                    elif msg_type == "trade":
                        try:
                            _handle_trade(data.get("msg", {}))
                        except Exception:
                            logger.exception("Failed to process trade msg: %s", raw_message[:300])
                        writer.enqueue("trade", raw_message)

                    elif msg_type == "fill":
                        try:
                            _handle_fill(data.get("msg", {}))
                        except Exception:
                            logger.exception("Failed to process fill msg: %s", raw_message[:300])

                    elif msg_type == "subscribed":
                        _handle_subscribed(data)

                    elif msg_type == "error":
                        _handle_error(data)
                        error_code = (data.get("msg") or {}).get("code", -1)
                        if error_code in _RECONNECT_ERRORS:
                            logger.warning("Reconnectable error (code=%s), breaking to reconnect.", error_code)
                            break
                        # error code 6 = already subscribed → harmless
                    else:
                        logger.debug("Unknown message type=%s: %s", msg_type, raw_message[:200])

        except (websockets.ConnectionClosed, websockets.InvalidStatusCode, OSError) as exc:
            logger.warning("Disconnected: %s", exc)
        except Exception:
            logger.exception("Unexpected error in WS loop")

        # Exponential backoff before reconnecting
        logger.info("Reconnecting in %.1fs …", backoff)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, max_backoff)
