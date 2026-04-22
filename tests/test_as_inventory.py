from __future__ import annotations

from kalshi_as.inventory import InventoryState


def test_apply_fill_updates_yes_inventory_and_dedupes():
    state = InventoryState()

    applied = state.apply_fill(
        {
            "fill_id": "fill-1",
            "market_ticker": "KXBTC-TEST",
            "side": "yes",
            "action": "buy",
            "count_fp": "7.00",
        }
    )

    applied_again = state.apply_fill(
        {
            "fill_id": "fill-1",
            "market_ticker": "KXBTC-TEST",
            "side": "yes",
            "action": "buy",
            "count_fp": "7.00",
        }
    )

    assert applied is True
    assert applied_again is False
    assert state.get_position("KXBTC-TEST") == 7.0


def test_apply_fill_handles_no_side_as_negative_yes_inventory():
    state = InventoryState()

    state.apply_fill(
        {
            "fill_id": "fill-2",
            "market_ticker": "FED-TEST",
            "side": "no",
            "action": "buy",
            "count_fp": "3.50",
        }
    )

    assert state.get_position("FED-TEST") == -3.5


def test_apply_fill_prefers_post_position_when_present():
    state = InventoryState()
    state.set_position("CPITEST", 2.0)

    state.apply_fill(
        {
            "fill_id": "fill-3",
            "market_ticker": "CPITEST",
            "side": "yes",
            "action": "buy",
            "count_fp": "5.00",
            "post_position_fp": "9.00",
        }
    )

    assert state.get_position("CPITEST") == 9.0


def test_set_from_positions_replaces_existing_snapshot():
    state = InventoryState()
    state.set_position("OLD", 4.0)

    state.set_from_positions(
        [
            {"ticker": "NEW1", "position_fp": "10.00"},
            {"market_ticker": "NEW2", "position_fp": "-2.00"},
            {"ticker": "ZERO", "position_fp": "0.00"},
        ]
    )

    assert state.snapshot() == {"NEW1": 10.0, "NEW2": -2.0}
