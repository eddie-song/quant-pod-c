from __future__ import annotations

from kalshi_as.ledger import PortfolioLedger


def test_ledger_updates_qty_and_cash_and_mtm():
    led = PortfolioLedger(out_path="data/kalshi/_test_ledger.jsonl", assumed_fee_cents_per_contract=0.0)
    led.set_mid("T", 50)
    led.apply_fill(ticker="T", action="buy", price_cents=40, count=2)
    assert led.qty_for_ticker("T") == 2.0
    # cash = -80, mtm = -80 + 2*50 = 20
    row = led.snapshot_row("T")
    assert row["cash_cents"] == -80.0
    assert row["mark_to_mid_pnl_cents"] == 20.0

    led.apply_fill(ticker="T", action="sell", price_cents=60, count=1)
    assert led.qty_for_ticker("T") == 1.0
    row2 = led.snapshot_row("T")
    # cash = -80 + 60 = -20, mtm = -20 + 1*50 = 30
    assert row2["cash_cents"] == -20.0
    assert row2["mark_to_mid_pnl_cents"] == 30.0

