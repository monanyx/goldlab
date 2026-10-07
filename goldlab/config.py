"""Configuration objects for costs and risk. All prices are USD per troy ounce."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CostConfig:
    """Trading costs.

    Defaults are deliberately conservative for a retail XAUUSD CFD account:
    liquid-hours spreads at good brokers are often $0.10-0.25/oz, but they widen
    in Asia, around the 17:00 New York rollover and on news. We charge $0.35/oz
    as the base round-trip spread and widen it further in Asia.
    """

    spread: float = 0.35  # full bid/ask spread in USD/oz, paid once per round trip
    session_spread_mult: dict = field(
        default_factory=lambda: {"Asia": 1.5, "Off": 2.0}
    )
    slippage: float = 0.05  # USD/oz per side on market and stop fills (not on limit targets)
    commission_per_oz: float = 0.0  # per side; most CFD brokers bake this into the spread
    # Overnight swap in USD/oz per night, charged when a position is open across
    # the 17:00 New York rollover. Gold swaps are usually negative on longs and
    # shorts at retail brokers. Wednesday rollover is charged triple.
    swap_long: float = -0.60
    swap_short: float = -0.20

    def spread_for(self, session: str) -> float:
        return self.spread * self.session_spread_mult.get(session, 1.0)


@dataclass(frozen=True)
class RiskConfig:
    risk_pct: float = 0.01  # fraction of current equity risked per trade (entry to stop)
    daily_loss_limit_pct: float = 0.03  # stop trading for the day once realised day loss hits this
    max_trades_per_day: int = 3
    initial_equity: float = 100_000.0
    min_units: float = 1.0  # 0.01 lot = 1 oz
    unit_step: float = 1.0  # position size granularity in oz
    max_leverage: float = 20.0  # notional cap: units * price <= equity * max_leverage
