from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


class BrokerError(RuntimeError):
    pass


@dataclass(frozen=True)
class BrokerOrder:
    side: int  # +1 buy, -1 sell
    size: float  # ounces
    kind: str  # "market" or "stop"
    stop_level: float  # mandatory hard stop
    level: float | None = None  # trigger for stop-entry orders
    profit_level: float | None = None
    good_till: pd.Timestamp | None = None  # UTC expiry for working orders
    group: str | None = None

    def __post_init__(self):
        if self.stop_level is None or not self.stop_level > 0:
            raise BrokerError("refusing to build an order without a stop loss")
        if self.size <= 0:
            raise BrokerError("order size must be positive")
        if self.kind == "stop" and self.level is None:
            raise BrokerError("stop-entry orders need a level")
        ref = self.level if self.level is not None else None
        if ref is not None and (ref - self.stop_level) * self.side <= 0:
            raise BrokerError("stop loss is on the wrong side of the entry")


class Broker:
    """Minimal interface the bot needs from a broker."""

    name = "broker"

    def balance(self) -> float: ...
    def size_rules(self) -> tuple[float, float]: ...  # (min size, size step)
    def place(self, order: BrokerOrder) -> dict: ...
    def positions(self) -> list[dict]: ...  # [{deal_id, side, size, level}]
    def working_orders(self) -> list[dict]: ...  # [{deal_id, side, size, level}]
    def close_position(self, deal_id: str) -> None: ...
    def cancel_order(self, deal_id: str) -> None: ...

    def close_all(self) -> int:
        n = 0
        for o in self.working_orders():
            self.cancel_order(o["deal_id"])
            n += 1
        for p in self.positions():
            self.close_position(p["deal_id"])
            n += 1
        return n
