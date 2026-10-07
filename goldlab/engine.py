"""Bar-by-bar trading engine shared by the backtester and the live/paper bot.

`Engine.step()` processes exactly one completed bar against an `EngineState`.
The state is plain data and round-trips through JSON, so the bot can persist
it between runs (e.g. one GitHub Actions run every 15 minutes) and still make
*exactly* the same decisions as `Backtester.run()`, which simply calls
`step()` for every bar in a loop.

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
* One position at a time. Daily loss limit, max trades per day and the
  optional "no new entries after HH:MM local" cut-off are enforced per trading
  day (17:00 New York rollover). Strategy `flat` bars force an exit at that
  bar's open and cancel pending orders. Positions are also closed at the close
  of the last bar before the weekend.
* Swap is charged for every rollover a position is held through (triple on the
  Wednesday->Thursday rollover).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, fields

import numpy as np
import pandas as pd

from .config import CostConfig, RiskConfig
from .data import is_market_closed
from .sessions import cost_session, minute_diffs

BLOCK_REASONS = ("daily_loss_limit", "max_trades", "after_cutoff", "size_zero", "invalid_levels")


# --------------------------------------------------------------------------- data types


def _ts(v):
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else pd.Timestamp(v)


@dataclass
class Order:
    side: int  # +1 long, -1 short
    kind: str = "market"  # "market" or "stop"
    price: float | None = None  # trigger price for stop orders
    stop_price: float | None = None  # absolute protective stop
    stop_dist: float | None = None  # or: stop distance from the fill price
    target_price: float | None = None  # absolute take-profit
    target_r: float | None = None  # or: take-profit as a multiple of the stop distance
    expires: pd.Timestamp | None = None  # may only fill on bars that open before this time
    group: str | None = None  # OCO group: a fill cancels the rest of the group
    tag: str = ""
    reason: str = ""  # one-line human explanation

    def __post_init__(self):
        if self.side not in (1, -1):
            raise ValueError("side must be +1 or -1")
        if self.kind not in ("market", "stop"):
            raise ValueError("kind must be 'market' or 'stop'")
        if self.kind == "stop" and self.price is None:
            raise ValueError("stop orders need a trigger price")
        if (self.stop_price is None) == (self.stop_dist is None):
            raise ValueError("every order needs exactly one of stop_price / stop_dist (hard stop)")
        if self.stop_dist is not None and not self.stop_dist > 0:
            raise ValueError("stop_dist must be positive")
        self.expires = _ts(self.expires)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["expires"] = None if self.expires is None else self.expires.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Order":
        return cls(**d)


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
    reason: str = ""
    exit_time: pd.Timestamp | None = None
    exit_price: float | None = None
    exit_reason: str = ""
    costs_usd: float = 0.0
    swap_usd: float = 0.0
    pnl_usd: float = 0.0
    r_multiple: float = 0.0
    equity_after: float = 0.0

    _TIME_FIELDS = ("trade_date", "entry_time", "exit_time")

    def to_dict(self) -> dict:
        d = {f.name: getattr(self, f.name) for f in fields(self)}
        for k in self._TIME_FIELDS:
            d[k] = None if d[k] is None else pd.Timestamp(d[k]).isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Trade":
        d = dict(d)
        for k in cls._TIME_FIELDS:
            d[k] = _ts(d.get(k))
        return cls(**d)


@dataclass
class Bar:
    time: pd.Timestamp  # bar open time, UTC
    open: float
    high: float
    low: float
    close: float
    session: str
    trade_date: pd.Timestamp
    flat: bool = False  # strategy (or kill switch) says: be flat from this bar's open
    pre_close: bool = False  # last bar before the weekend close


@dataclass
class Event:
    kind: str  # orders | opened | closed | blocked
    time: pd.Timestamp
    orders: list = field(default_factory=list)
    trade: Trade | None = None
    reason: str = ""


@dataclass
class EngineState:
    equity: float
    pending: list[Order] = field(default_factory=list)
    pos: Trade | None = None
    day: pd.Timestamp | None = None
    day_start_equity: float = 0.0
    day_pnl: float = 0.0
    day_trades: int = 0
    trades: list[Trade] = field(default_factory=list)
    blocked: dict = field(default_factory=lambda: dict.fromkeys(BLOCK_REASONS, 0))
    last_time: pd.Timestamp | None = None

    def to_dict(self) -> dict:
        return {
            "equity": self.equity,
            "pending": [o.to_dict() for o in self.pending],
            "pos": None if self.pos is None else self.pos.to_dict(),
            "day": None if self.day is None else self.day.isoformat(),
            "day_start_equity": self.day_start_equity,
            "day_pnl": self.day_pnl,
            "day_trades": self.day_trades,
            "trades": [t.to_dict() for t in self.trades],
            "blocked": dict(self.blocked),
            "last_time": None if self.last_time is None else self.last_time.isoformat(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "EngineState":
        blocked = dict.fromkeys(BLOCK_REASONS, 0) | d.get("blocked", {})
        return cls(
            equity=d["equity"],
            pending=[Order.from_dict(o) for o in d.get("pending", [])],
            pos=None if d.get("pos") is None else Trade.from_dict(d["pos"]),
            day=_ts(d.get("day")),
            day_start_equity=d.get("day_start_equity", 0.0),
            day_pnl=d.get("day_pnl", 0.0),
            day_trades=d.get("day_trades", 0),
            trades=[Trade.from_dict(t) for t in d.get("trades", [])],
            blocked=blocked,
            last_time=_ts(d.get("last_time")),
        )


@dataclass
class OrderPlan:
    """A fully resolved order: what would be traded if it filled at `entry_mid`."""

    side: int
    entry_mid: float
    entry_price: float  # incl. half spread + slippage
    stop: float
    target: float | None
    units: float
    risk_usd: float
    entry_cost: float  # per oz


# --------------------------------------------------------------------------- engine


class Engine:
    def __init__(self, costs: CostConfig | None = None, risk: RiskConfig | None = None):
        self.costs = costs or CostConfig()
        self.risk = risk or RiskConfig()

    # ---- costs

    def _half_spread(self, session: str) -> float:
        return self.costs.spread_for(cost_session(session)) / 2.0

    def entry_fill(self, side: int, mid: float, session: str) -> tuple[float, float]:
        cost = self._half_spread(session) + self.costs.slippage + self.costs.commission_per_oz
        return mid + side * cost, cost

    def exit_fill(self, side: int, mid: float, session: str, limit: bool) -> tuple[float, float]:
        cost = self._half_spread(session) + self.costs.commission_per_oz
        if not limit:
            cost += self.costs.slippage
        return mid - side * cost, cost

    # ---- risk

    def risk_budget(self, st: EngineState) -> tuple[float, float]:
        """(normal per-trade risk, remaining daily loss allowance) in USD."""
        normal = self.risk.risk_pct * st.day_start_equity
        allowance = self.risk.daily_loss_limit_pct * st.day_start_equity + st.day_pnl
        return normal, allowance

    def entry_block_reason(self, st: EngineState, time: pd.Timestamp) -> str | None:
        """Why a new entry at `time` is not allowed (None = allowed)."""
        normal, allowance = self.risk_budget(st)
        if allowance < 0.25 * normal:
            return "daily_loss_limit"
        if st.day_trades >= self.risk.max_trades_per_day:
            return "max_trades"
        if self.risk.no_entry_after is not None:
            local = time.tz_convert(self.risk.no_entry_tz)
            hh, mm = map(int, self.risk.no_entry_after.split(":"))
            if local.hour * 60 + local.minute >= hh * 60 + mm:
                return "after_cutoff"
        return None

    def plan(self, od: Order, mid_fill: float, risk_usd: float, equity: float,
             session: str) -> OrderPlan | str:
        """Resolve levels and size for an order filling at `mid_fill`; str = rejection reason."""
        side = od.side
        stop = od.stop_price if od.stop_price is not None else mid_fill - side * od.stop_dist
        if (mid_fill - stop) * side <= 0:
            return "invalid_levels"  # gapped through the stop before entry
        if od.target_price is not None:
            target = od.target_price
        elif od.target_r is not None:
            target = mid_fill + side * od.target_r * abs(mid_fill - stop)
        else:
            target = None
        if target is not None and (target - mid_fill) * side <= 0:
            return "invalid_levels"
        entry_eff, cost = self.entry_fill(side, mid_fill, session)
        stop_eff, _ = self.exit_fill(side, stop, session, limit=False)
        per_oz = (entry_eff - stop_eff) * side
        if per_oz <= 0:
            return "invalid_levels"
        units = min(risk_usd / per_oz, equity * self.risk.max_leverage / entry_eff)
        units = math.floor(units / self.risk.unit_step + 1e-9) * self.risk.unit_step
        if units < self.risk.min_units:
            return "size_zero"
        return OrderPlan(side, mid_fill, entry_eff, stop, target, units, per_oz * units, cost)

    # ---- position handling

    def _close(self, st: EngineState, b: Bar, mid: float, reason: str, limit: bool, ev: list) -> None:
        pos = st.pos
        px, cost = self.exit_fill(pos.side, mid, b.session, limit)
        gross = (px - pos.entry_price) * pos.side * pos.units
        pos.costs_usd += cost * pos.units
        pnl = gross + pos.swap_usd
        pos.exit_time, pos.exit_price, pos.exit_reason = b.time, px, reason
        pos.pnl_usd = pnl
        pos.r_multiple = pnl / pos.risk_usd
        st.equity += pnl
        st.day_pnl += pnl
        pos.equity_after = st.equity
        st.trades.append(pos)
        st.pos = None
        ev.append(Event("closed", b.time, trade=pos, reason=reason))

    def _manage(self, st: EngineState, b: Bar, entry_bar: bool, entry_kind: str, ev: list) -> None:
        pos = st.pos
        side, stop, tgt = pos.side, pos.stop, pos.target
        stop_hit = b.low <= stop if side == 1 else b.high >= stop
        tgt_hit = tgt is not None and (b.high >= tgt if side == 1 else b.low <= tgt)
        if not entry_bar:
            if (b.open <= stop) if side == 1 else (b.open >= stop):
                return self._close(st, b, b.open, "stop_gap", False, ev)
            if tgt is not None and ((b.open >= tgt) if side == 1 else (b.open <= tgt)):
                return self._close(st, b, b.open, "target_gap", True, ev)
        if stop_hit:
            return self._close(st, b, stop, "stop", False, ev)
        if tgt_hit:
            if entry_bar and entry_kind == "stop":
                if not (b.close >= tgt if side == 1 else b.close <= tgt):
                    return None
            return self._close(st, b, tgt, "target", True, ev)
        return None

    # ---- the step

    def step(self, st: EngineState, b: Bar, new_orders: list[Order] | None = None) -> tuple[list[Event], float]:
        """Process one completed bar. Returns (events, mark-to-market equity at bar close)."""
        ev: list[Event] = []

        # new trading day: swap for positions held through the rollover, reset counters
        if st.day is None or b.trade_date != st.day:
            if st.pos is not None and st.day is not None:
                nights = 3 if st.day.dayofweek == 2 else 1
                rate = self.costs.swap_long if st.pos.side == 1 else self.costs.swap_short
                st.pos.swap_usd += rate * st.pos.units * nights
            st.day = pd.Timestamp(b.trade_date)
            st.day_start_equity = st.equity
            st.day_pnl = 0.0
            st.day_trades = 0
            st.pending = []

        # forced flat at the open
        if b.flat:
            st.pending = []
            if st.pos is not None:
                self._close(st, b, b.open, "session_end", False, ev)

        if st.pos is not None:
            self._manage(st, b, False, "", ev)

        # pending entries (only when flat and not exited on this bar)
        exited = bool(st.trades) and st.trades[-1].exit_time == b.time
        if st.pos is None and not exited and st.pending and not b.flat:
            st.pending = [o for o in st.pending if o.expires is None or b.time < o.expires]
            triggered = []
            for o in st.pending:
                if o.kind == "market":
                    triggered.append((0.0, o, b.open))
                elif o.side == 1 and b.high >= o.price:
                    triggered.append((abs(o.price - b.open), o, max(b.open, o.price)))
                elif o.side == -1 and b.low <= o.price:
                    triggered.append((abs(o.price - b.open), o, min(b.open, o.price)))
            if triggered:
                triggered.sort(key=lambda t: t[0])  # nearest to the open fills first
                _, od, mid_fill = triggered[0]
                st.pending = [p for p in st.pending
                              if p is not od and (od.group is None or p.group != od.group)]
                why = self.entry_block_reason(st, b.time)
                if why is None:
                    normal, allowance = self.risk_budget(st)
                    p = self.plan(od, mid_fill, min(normal, allowance), st.equity, b.session)
                    if isinstance(p, str):
                        why = p
                    else:
                        st.pos = Trade(
                            trade_date=pd.Timestamp(b.trade_date), side=p.side, entry_time=b.time,
                            entry_price=p.entry_price, stop=p.stop, target=p.target, units=p.units,
                            risk_usd=p.risk_usd, tag=od.tag, reason=od.reason,
                            costs_usd=p.entry_cost * p.units,
                        )
                        st.day_trades += 1
                        ev.append(Event("opened", b.time, trade=st.pos, reason=od.reason))
                        self._manage(st, b, True, od.kind, ev)
                if why is not None:
                    st.blocked[why] = st.blocked.get(why, 0) + 1
                    ev.append(Event("blocked", b.time, orders=[od], reason=why))

        # never hold through the weekend
        if st.pos is not None and b.pre_close:
            self._close(st, b, b.close, "pre_close", False, ev)

        if st.pos is not None:
            px, _ = self.exit_fill(st.pos.side, b.close, b.session, limit=False)
            mtm = st.equity + (px - st.pos.entry_price) * st.pos.side * st.pos.units + st.pos.swap_usd
        else:
            mtm = st.equity

        # orders created at this bar's close
        if new_orders and st.pos is None:
            for o in new_orders:
                if o.group is not None:
                    st.pending = [p for p in st.pending if p.group != o.group]
            st.pending.extend(new_orders)
            ev.append(Event("orders", b.time, orders=list(new_orders)))

        st.last_time = b.time
        return ev, mtm


# --------------------------------------------------------------------------- backtester


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    equity: pd.Series  # mark-to-market equity at each bar close
    initial_equity: float
    blocked: dict = field(default_factory=dict)  # counts of rule-based rejections


def bar_minutes(index: pd.DatetimeIndex) -> int:
    d = minute_diffs(index[:2000])
    d = d[d > 0]
    return int(round(float(np.median(d)))) if len(d) else 0


def bars_from_frame(df: pd.DataFrame, flat: np.ndarray, minutes: int | None = None) -> list[Bar]:
    """Turn a cleaned, session-labelled frame into engine bars."""
    minutes = minutes or bar_minutes(df.index)
    pre_close = is_market_closed(df.index + pd.Timedelta(minutes=minutes))
    cols = zip(df.index, df["open"].to_numpy(float), df["high"].to_numpy(float),
               df["low"].to_numpy(float), df["close"].to_numpy(float), df["session"].to_numpy(),
               df["trade_date"].tolist(), np.asarray(flat, dtype=bool), pre_close)
    return [Bar(t, o, h, l, c, s, d, bool(f), bool(p)) for t, o, h, l, c, s, d, f, p in cols]


class Backtester:
    def __init__(self, costs: CostConfig | None = None, risk: RiskConfig | None = None):
        self.engine = Engine(costs, risk)
        self.costs, self.risk = self.engine.costs, self.engine.risk

    def run(self, df: pd.DataFrame, signals: StrategySignals, minutes: int | None = None) -> BacktestResult:
        if len(signals.flat) != len(df):
            raise ValueError("flat array length must match data")
        st = EngineState(equity=self.risk.initial_equity)
        equity = np.empty(len(df))
        for i, bar in enumerate(bars_from_frame(df, signals.flat, minutes)):
            _, equity[i] = self.engine.step(st, bar, signals.orders.get(i))
        trades = pd.DataFrame([t.to_dict() for t in st.trades])
        if not trades.empty:
            for k in Trade._TIME_FIELDS:
                trades[k] = pd.to_datetime(trades[k], utc=k != "trade_date")
        return BacktestResult(trades, pd.Series(equity, index=df.index, name="equity"),
                              self.risk.initial_equity, dict(st.blocked))
