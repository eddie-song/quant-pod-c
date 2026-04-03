from __future__ import annotations

from kalshi_as.model import ASConfig, compute_quotes
from kalshi_as.sample_orders import build_sample_order_record


def test_build_sample_order_record_shape():
    cfg = ASConfig(gamma=0.05, k=2.0, tau_hours=1.0, tick=0.01)
    q = compute_quotes(0.5, inventory_yes=0.0, sigma=0.1, config=cfg)
    r = build_sample_order_record(
        market_ticker="KXTEST-YES",
        model_quotes=q,
        sigma=0.1,
        book_bid=0.48,
        book_ask=0.52,
        sample_count_per_side=10.0,
        gamma=cfg.gamma,
        k=cfg.k,
        tau_hours=cfg.tau_hours,
        inventory_yes=0.0,
    )
    assert r["market_ticker"] == "KXTEST-YES"
    assert len(r["hypothetical_orders_yes_side"]) == 2
    assert r["hypothetical_orders_yes_side"][0]["action"] == "post_limit_buy_yes"
    assert r["hypothetical_orders_yes_side"][1]["action"] == "post_limit_sell_yes"
