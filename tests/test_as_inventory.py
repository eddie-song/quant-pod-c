from __future__ import annotations

from pathlib import Path

import pytest

from kalshi_as.inventory import inventory_for_ticker, load_inventory_by_ticker


def test_load_inventory_by_ticker_and_default_missing(tmp_path: Path):
    p = tmp_path / "inv.json"
    p.write_text('{"TICKER_A": 10, "TICKER_B": -2.5}', encoding="utf-8")
    inv = load_inventory_by_ticker(str(p))
    assert inv["TICKER_A"] == 10.0
    assert inv["TICKER_B"] == -2.5
    assert inventory_for_ticker(inv, "UNKNOWN") == 0.0


def test_load_inventory_rejects_non_numeric(tmp_path: Path):
    p = tmp_path / "inv_bad.json"
    p.write_text('{"TICKER_A": "not-a-number"}', encoding="utf-8")
    with pytest.raises(ValueError):
        load_inventory_by_ticker(str(p))
