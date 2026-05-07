from __future__ import annotations

from kalshi_as.terminal_actions import build_terminal_record


def test_build_terminal_record_shape():
    r = build_terminal_record(market_ticker="T", tau_hours=0.01, qty_yes=3.0, action="flatten_intent", note="x")
    assert r["type"] == "terminal_action"
    assert r["market_ticker"] == "T"
    assert r["action"] == "flatten_intent"
    assert r["qty_yes"] == 3.0

