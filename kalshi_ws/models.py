from __future__ import annotations

from dataclasses import dataclass, field


def _parse_float(val: object) -> float:
    """Parse a numeric value that may arrive as a string."""
    if isinstance(val, str):
        return float(val)
    if isinstance(val, (int, float)):
        return float(val)
    return 0.0


@dataclass
class MarketTicker:
    market_ticker: str
    yes_bid: float = 0.0
    yes_ask: float = 0.0
    spread: float = 0.0
    last_price: float = 0.0
    volume: float = 0.0
    open_interest: float = 0.0
    dollar_volume: int = 0
    dollar_open_interest: int = 0
    last_update_ts: int = 0
    update_count: int = 0

    @classmethod
    def from_msg(cls, msg: dict) -> "MarketTicker":
        yes_bid = _parse_float(msg.get("yes_bid_dollars", 0))
        yes_ask = _parse_float(msg.get("yes_ask_dollars", 0))
        return cls(
            market_ticker=msg.get("market_ticker", ""),
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            spread=round(yes_ask - yes_bid, 6),
            last_price=_parse_float(msg.get("price_dollars", 0)),
            volume=_parse_float(msg.get("volume_fp", 0)),
            open_interest=_parse_float(msg.get("open_interest_fp", 0)),
            dollar_volume=int(msg.get("dollar_volume", 0)),
            dollar_open_interest=int(msg.get("dollar_open_interest", 0)),
            last_update_ts=int(msg.get("ts", 0)),
            update_count=1,
        )

    def update(self, msg: dict) -> None:
        """Merge a partial ticker update into the current state."""
        if "yes_bid_dollars" in msg:
            self.yes_bid = _parse_float(msg["yes_bid_dollars"])
        if "yes_ask_dollars" in msg:
            self.yes_ask = _parse_float(msg["yes_ask_dollars"])
        self.spread = round(self.yes_ask - self.yes_bid, 6)

        if "price_dollars" in msg:
            self.last_price = _parse_float(msg["price_dollars"])
        if "volume_fp" in msg:
            self.volume = _parse_float(msg["volume_fp"])
        if "open_interest_fp" in msg:
            self.open_interest = _parse_float(msg["open_interest_fp"])
        if "dollar_volume" in msg:
            self.dollar_volume = int(msg["dollar_volume"])
        if "dollar_open_interest" in msg:
            self.dollar_open_interest = int(msg["dollar_open_interest"])
        if "ts" in msg:
            self.last_update_ts = int(msg["ts"])
        self.update_count += 1


@dataclass
class Trade:
    trade_id: str
    market_ticker: str
    yes_price: float
    no_price: float
    size: float
    taker_side: str
    ts: int

    @classmethod
    def from_msg(cls, msg: dict) -> "Trade":
        return cls(
            trade_id=msg.get("trade_id", ""),
            market_ticker=msg.get("market_ticker", ""),
            yes_price=_parse_float(msg.get("yes_price_dollars", 0)),
            no_price=_parse_float(msg.get("no_price_dollars", 0)),
            size=_parse_float(msg.get("count_fp", 0)),
            taker_side=msg.get("taker_side", ""),
            ts=int(msg.get("ts", 0)),
        )
