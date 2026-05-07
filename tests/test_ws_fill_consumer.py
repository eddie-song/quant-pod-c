from __future__ import annotations

import asyncio

import pytest

from kalshi_as.ledger import PortfolioLedger
from kalshi_as.ws_fill_consumer import run_ws_fill_consumer
from kalshi_ws.models import Fill


def test_consume_fills_updates_ledger(monkeypatch, tmp_path):
    fills = [
        Fill(market_ticker="T", side="yes", action="buy", count=2.0, yes_price=0.4, no_price=0.6, ts=1),
        Fill(market_ticker="T", side="yes", action="sell", count=1.0, yes_price=0.6, no_price=0.4, ts=2),
    ]

    def _consume():
        nonlocal fills
        out = fills
        fills = []
        return out

    monkeypatch.setattr("kalshi_as.ws_fill_consumer.consume_fills", _consume)

    ledger = PortfolioLedger(out_path=str(tmp_path / "ledger.jsonl"))

    async def _run_once():
        # Run the consumer briefly; it should drain the test fills.
        task = asyncio.create_task(run_ws_fill_consumer(ledger=ledger, poll_s=0.01, out_path=str(tmp_path / "fills.jsonl")))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run_once())
    assert ledger.qty_for_ticker("T") == 1.0

