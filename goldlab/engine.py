"""Bar-by-bar backtest engine.

Conventions (all deliberately conservative):

* Bar prices are treated as mid prices. Every fill pays half the spread
  (spread depends on the session of the fill bar), so a round trip pays one
  full spread. Market and stop fills also pay `slippage` per side; limit
  targets do not.
* Orders created from bar ``i`` (using data up to bar ``i``'s close) can only
  fill from bar ``i + 1``. Market orders fill at the next bar's open.
* If a bar touches both the stop and the target of an open position, the stop
  is assumed to have been hit first. If price gaps through a stop, the fill is
  at the (worse) open.
* On the bar a stop-entry order triggers, the stop can be hit in the same bar,
  but the target only counts if the bar also *closes* beyond it.
* Position size: ``risk_pct`` of the equity at the start of the trading day is
  lost if the stop is hit (including spread and slippage), i.e. a clean
  stop-out is exactly -1R. Risk is also capped at what is left of the daily
  loss allowance, so the daily limit can only be overshot by gaps.
* One position at a time. Daily loss limit and max trades per day are enforced
  per trading day (17:00 New York rollover). Strategy `flat` bars force an exit
  at that bar's open and cancel pending orders. Positions are also closed on the
  last bar before a weekend/holiday gap.
* Swap is charged for every rollover a position is held through (triple on the
  Wednesday->Thursday rollover).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .config import CostConfig, RiskConfig
from .sessions import cost_session, minute_diffs

WEEKEND_GAP_HOURS = 6


@dataclass
class Order:
    side: int  # +1 long, -1 short
    kind: str = "market"  # "market" or "stop"
    price: float | None = None  # trigger price for stop orders
    stop_price: float | None = None  # absolute protective stop
    stop_dist: float | None = None  # or: stop distance from the fill price
    target_price: float | None = None  # absolute take-profit
    target_r: float | None = None  # or: take-profit as a multiple of the stop distance
    expires: int | None = None  # last bar index on which the order may fill
    group: str | None = None  # OCO group: a fill cancels the rest of the group
    tag: str = ""

    def __post_init__(self):
        if self.side not in (1, -1):
            raise ValueError("side must be +1 or -1")
        if self.kind not in ("market", "stop"):
            raise ValueError("kind must be 'market' or 'stop'")
        if self.kind == "stop" and self.price is None:
            raise ValueError("stop orders need a trigger price")
        if (self.stop_price is None) == (self.stop_dist is None):
            raise ValueError("every order needs exactly one of stop_price / stop_dist (hard stop)")


@dataclass
class StrategySignals:
    """What a strategy hands the engine."""

    orders: dict[int, list[Order]]  # bar index -> orders created at that bar's close
    flat: np.ndarray  # bool per bar: must be flat at this bar's open; no new entries


@dataclass
class Trade:
    trade_date: pd.Timestamp
    side: int
    entry_time: pd.Timestamp
    entry_price: float  # effective fill incl. spread/slippage
    stop: float
    target: float | None
    units: float
    risk_usd: float
    tag: str
    exit_time: pd.Timestamp | None = None
    exit_price: float | None = None
    exit_reason: str = ""
    costs_usd: float = 0.0
    swap_usd: float = 0.0
    pnl_usd: float = 0.0
    r_multiple: float = 0.0
    equity_after: float = 0.0


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    equity: pd.Series  # mark-to-market equity at each bar close
    initial_equity: float
    blocked: dict = field(default_factory=dict)  # counts of rule-based rejections


class Backtester:
    def __init__(self, costs: CostConfig | None = None, risk: RiskConfig | None = None):
        self.costs = costs or CostConfig()
        self.risk = risk or RiskConfig()

    # ------------------------------------------------------------------ helpers

    def _half_spread(self, i: int) -> float:
        return self.costs.spread_for(cost_session(self._session[i])) / 2.0

    def _entry_fill(self, side: int, mid: float, i: int) -> tuple[float, float]:
        """Effective entry price and per-oz cost."""
        cost = self._half_spread(i) + self.costs.slippage + self.costs.commission_per_oz
        return mid + side * cost, cost

    def _exit_fill(self, side: int, mid: float, i: int, limit: bool) -> tuple[float, float]:
        cost = self._half_spread(i) + self.costs.commission_per_oz
        if not limit:
            cost += self.costs.slippage
        return mid - side * cost, cost

    def _size(self, risk_usd: float, equity: float, entry_eff: float, stop: float, side: int,
              i: int) -> float:
        # Loss per oz if stopped: entry_eff -> stop mid, minus exit spread/slippage.
        stop_eff, _ = self._exit_fill(side, stop, i, limit=False)
        per_oz = (entry_eff - stop_eff) * side
        if per_oz <= 0:
            return 0.0
        units = risk_usd / per_oz
        units = min(units, equity * self.risk.max_leverage / entry_eff)
        units = math.floor(units / self.risk.unit_step) * self.risk.unit_step
        return units if units >= self.risk.min_units else 0.0

    # ------------------------------------------------------------------ main loop

    def run(self, df: pd.DataFrame, signals: StrategySignals) -> BacktestResult:
        n = len(df)
        o = df["open"].to_numpy(float)
        h = df["high"].to_numpy(float)
        l = df["low"].to_numpy(float)
        c = df["close"].to_numpy(float)
        self._session = df["session"].to_numpy()
        tdate = df["trade_date"].to_numpy()
        times = df.index
        flat = np.asarray(signals.flat, dtype=bool)
        if len(flat) != n:
            raise ValueError("flat array length must match data")
        gap_after = np.r_[minute_diffs(times) / 60.0 > WEEKEND_GAP_HOURS, True]

        equity = self.risk.initial_equity
        equity_curve = np.empty(n)
        trades: list[Trade] = []
        blocked = {"daily_loss_limit": 0, "max_trades": 0, "size_zero": 0, "invalid_levels": 0}

        pending: list[Order] = []
        pos: Trade | None = None
        day = None
        day_start_equity = equity
        day_pnl = 0.0
        day_trades = 0

        def close(i: int, mid: float, reason: str, limit: bool) -> None:
            nonlocal pos, equity, day_pnl
            px, cost = self._exit_fill(pos.side, mid, i, limit)
            gross = (px - pos.entry_price) * pos.side * pos.units
            pos.costs_usd += cost * pos.units
            pnl = gross + pos.swap_usd
            pos.exit_time, pos.exit_price, pos.exit_reason = times[i], px, reason
            pos.pnl_usd = pnl
            pos.r_multiple = pnl / pos.risk_usd
            equity += pnl
            day_pnl += pnl
            pos.equity_after = equity
            trades.append(pos)
            pos = None

        def manage(i: int, entry_bar: bool, entry_kind: str) -> None:
            """Check stop/target on bar i for the open position."""
            side, stop, tgt = pos.side, pos.stop, pos.target
            lo_hit = l[i] <= stop if side == 1 else h[i] >= stop
            tgt_hit = tgt is not None and (h[i] >= tgt if side == 1 else l[i] <= tgt)
            if not entry_bar:
                if (o[i] <= stop) if side == 1 else (o[i] >= stop):
                    close(i, o[i], "stop_gap", limit=False)
                    return
                if tgt is not None and ((o[i] >= tgt) if side == 1 else (o[i] <= tgt)):
                    close(i, o[i], "target_gap", limit=True)
                    return
            if lo_hit:
                close(i, stop, "stop", limit=False)
                return
            if tgt_hit:
                if entry_bar and entry_kind == "stop":
                    beyond = c[i] >= tgt if side == 1 else c[i] <= tgt
                    if not beyond:
                        return
                close(i, tgt, "target", limit=True)

        for i in range(n):
            # ---- new trading day bookkeeping and swap
            if tdate[i] != day:
                if pos is not None and day is not None:
                    nights = 3 if pd.Timestamp(day).dayofweek == 2 else 1
                    rate = self.costs.swap_long if pos.side == 1 else self.costs.swap_short
                    pos.swap_usd += rate * pos.units * nights
                day = tdate[i]
                day_start_equity = equity
                day_pnl = 0.0
                day_trades = 0
                pending = []

            # ---- forced flat at open
            if flat[i]:
                pending = []
                if pos is not None:
                    close(i, o[i], "session_end", limit=False)

            # ---- manage open position
            if pos is not None:
                manage(i, entry_bar=False, entry_kind="")

            # ---- try to fill pending orders (only when flat, not just exited)
            exited_this_bar = bool(trades) and trades[-1].exit_time == times[i]
            if pos is None and not exited_this_bar and pending and not flat[i]:
                pending = [od for od in pending if od.expires is None or od.expires >= i]
                triggered = []
                for od in pending:
                    if od.kind == "market":
                        triggered.append((0.0, od, o[i]))
                    elif od.side == 1 and h[i] >= od.price:
                        triggered.append((abs(od.price - o[i]), od, max(o[i], od.price)))
                    elif od.side == -1 and l[i] <= od.price:
                        triggered.append((abs(od.price - o[i]), od, min(o[i], od.price)))
                if triggered:
                    triggered.sort(key=lambda t: t[0])  # nearest to the open fills first
                    _, od, mid_fill = triggered[0]
                    pending = [p for p in pending if p is not od and (od.group is None or p.group != od.group)]
                    normal_risk = self.risk.risk_pct * day_start_equity
                    allowance = self.risk.daily_loss_limit_pct * day_start_equity + day_pnl
                    if allowance < 0.25 * normal_risk:
                        blocked["daily_loss_limit"] += 1
                    elif day_trades >= self.risk.max_trades_per_day:
                        blocked["max_trades"] += 1
                    else:
                        self._open(od, i, mid_fill, min(normal_risk, allowance), equity,
                                   tdate[i], times[i], blocked)
                        if self._new is not None:
                            pos, self._new = self._new, None
                            day_trades += 1
                            manage(i, entry_bar=True, entry_kind=od.kind)

            # ---- weekend / holiday gap: never hold through it
            if pos is not None and gap_after[i]:
                close(i, c[i], "pre_gap", limit=False)

            # ---- mark to market
            if pos is not None:
                px, _ = self._exit_fill(pos.side, c[i], i, limit=False)
                equity_curve[i] = equity + (px - pos.entry_price) * pos.side * pos.units + pos.swap_usd
            else:
                equity_curve[i] = equity

            # ---- collect new orders created at this bar's close
            new = signals.orders.get(i)
            if new and pos is None:
                for od in new:
                    if od.group is not None:
                        pending = [p for p in pending if p.group != od.group]
                pending.extend(new)

        trades_df = pd.DataFrame([t.__dict__ for t in trades])
        return BacktestResult(trades_df, pd.Series(equity_curve, index=times, name="equity"),
                              self.risk.initial_equity, blocked)

    _new: Trade | None = None

    def _open(self, od: Order, i: int, mid_fill: float, risk_budget: float, equity: float,
              tdate, ts, blocked) -> None:
        self._new = None
        side = od.side
        stop = od.stop_price if od.stop_price is not None else mid_fill - side * od.stop_dist
        if (mid_fill - stop) * side <= 0:
            blocked["invalid_levels"] += 1  # gapped through the stop before entry
            return
        if od.target_price is not None:
            target = od.target_price
        elif od.target_r is not None:
            target = mid_fill + side * od.target_r * abs(mid_fill - stop)
        else:
            target = None
        if target is not None and (target - mid_fill) * side <= 0:
            blocked["invalid_levels"] += 1
            return
        entry_eff, cost = self._entry_fill(side, mid_fill, i)
        units = self._size(risk_budget, equity, entry_eff, stop, side, i)
        if units <= 0:
            blocked["size_zero"] += 1
            return
        stop_eff, _ = self._exit_fill(side, stop, i, limit=False)
        risk_usd = (entry_eff - stop_eff) * side * units
        self._new = Trade(
            trade_date=pd.Timestamp(tdate), side=side, entry_time=ts, entry_price=entry_eff,
            stop=stop, target=target, units=units, risk_usd=risk_usd, tag=od.tag,
            costs_usd=cost * units,
        )
