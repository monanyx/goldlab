"""London breakout of the Asian range.

Asia range = high/low of the trade date's bars from 00:00 UTC up to the London
open (08:00 London time). At the London open, OCO buy-stop / sell-stop orders
are placed `buffer_atr` x daily ATR beyond the range. The first to trigger
cancels the other. Hard stop `stop_frac` x range width (+ buffer) from entry,
target `target_r` x stop. Entries only during the first `entry_hours` of London;
everything is flat by `flat_hour` London time.

Optional switches (used by the pre-registered variants, docs/improvement-tests.md):
  trend_days > 0   only trade in the direction of the previous day's close vs its
                   `trend_days`-day average of daily closes
  confirm_close    instead of stop orders on a touch, wait for a bar to *close* beyond
                   the range and enter at the next bar's open
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

import numpy as np
import pandas as pd

from ..engine import Order, StrategySignals, bar_minutes
from ..features import daily_atr, daily_trend, day_groups, london_minutes
from .base import Strategy

LONDON_OPEN = 8 * 60


@dataclass(frozen=True)
class LondonBreakout(Strategy):
    entry_hours: float = 3.0
    buffer_atr: float = 0.05
    stop_frac: float = 1.0
    target_r: float = 1.5
    min_range_atr: float = 0.15
    max_range_atr: float = 0.8
    flat_hour: float = 16.0
    trend_days: int = 0
    confirm_close: bool = False

    name: ClassVar[str] = "london_breakout"
    grid: ClassVar[dict] = {
        "entry_hours": [2.0, 3.0, 4.0],
        "buffer_atr": [0.0, 0.05, 0.10],
        "stop_frac": [0.5, 0.75, 1.0],
        "target_r": [1.0, 1.5, 2.0, 2.5],
    }

    def required_history_days(self) -> int:
        return max(15, self.trend_days + 2)

    def signals(self, df: pd.DataFrame) -> StrategySignals:
        lon = london_minutes(df)
        trend = daily_trend(df, self.trend_days) if self.trend_days else None
        close = df["close"].to_numpy()
        times = df.index
        utc_min = (df.index.hour * 60 + df.index.minute).to_numpy()
        datr = daily_atr(df)
        hi, lo = df["high"].to_numpy(), df["low"].to_numpy()
        bar_min = bar_minutes(df.index) or 15
        flat_min = int(self.flat_hour * 60)
        flat = ~((lon >= LONDON_OPEN) & (lon < flat_min) & (utc_min >= 6 * 60))
        orders: dict[int, list[Order]] = {}

        for idx in day_groups(df):
            l_day, u_day = lon[idx], utc_min[idx]
            asia = idx[(u_day < 13 * 60) & (l_day < LONDON_OPEN)]
            if len(asia) < 12:
                continue
            k = asia[-1]
            # Orders are placed at the close of the last bar before the London open,
            # so this works identically in a backtest and in a live run at that moment.
            if lon[k] + bar_min != LONDON_OPEN:
                continue
            a = datr[k]
            if not np.isfinite(a) or a <= 0:
                continue
            r_hi, r_lo = hi[asia].max(), lo[asia].min()
            width = r_hi - r_lo
            if not (self.min_range_atr * a <= width <= self.max_range_atr * a):
                continue
            sides = (1, -1)
            note = ""
            if trend is not None:
                if trend[k] == 0:
                    continue
                sides = (int(trend[k]),)
                note = (f"; trend filter: {'long' if trend[k] > 0 else 'short'} only "
                        f"(yesterday's close {'above' if trend[k] > 0 else 'below'} its "
                        f"{self.trend_days}-day average)")
            buf = self.buffer_atr * a
            dist = self.stop_frac * width + buf
            london_open = df.index[k] + pd.Timedelta(minutes=bar_min)
            expires = london_open + pd.Timedelta(hours=self.entry_hours)
            grp = f"lb{london_open:%Y%m%d}"
            rng = f"Asia range {r_lo:.2f}-{r_hi:.2f} (width {width:.2f} = {width / a:.2f}x daily ATR)"

            if self.confirm_close:
                # The first 15-minute close outside the range (+ buffer) decides the day.
                # (by timestamp: evening bars that open the trade date are not "after the open")
                window = idx[(times[idx] >= london_open) & (times[idx] < expires)]
                for j in window:
                    side = 1 if close[j] > r_hi + buf else -1 if close[j] < r_lo - buf else 0
                    if side == 0:
                        continue
                    if side in sides:
                        orders[j] = [Order(
                            side=side, kind="market", stop_dist=dist, target_r=self.target_r, group=grp,
                            tag="long_confirm" if side == 1 else "short_confirm",
                            reason=(f"{'Close above' if side == 1 else 'Close below'} {rng} at "
                                    f"{times[j] + pd.Timedelta(minutes=bar_min):%H:%M} UTC; "
                                    f"confirmed London breakout{note}"))]
                    break
                continue

            why = f"{rng}; London-open breakout, valid until {expires:%H:%M} UTC{note}"
            day_orders = []
            if 1 in sides:
                day_orders.append(Order(side=1, kind="stop", price=r_hi + buf, stop_dist=dist,
                                        target_r=self.target_r, expires=expires, group=grp,
                                        tag="long_break", reason="Break above " + why))
            if -1 in sides:
                day_orders.append(Order(side=-1, kind="stop", price=r_lo - buf, stop_dist=dist,
                                        target_r=self.target_r, expires=expires, group=grp,
                                        tag="short_break", reason="Break below " + why))
            orders[k] = day_orders
        return StrategySignals(orders=orders, flat=flat)


# Pre-registered variants (docs/improvement-tests.md). Each changes exactly one thing.


@dataclass(frozen=True)
class LBTrend(LondonBreakout):
    trend_days: int = 20
    name: ClassVar[str] = "lb_trend"


@dataclass(frozen=True)
class LBConfirm(LondonBreakout):
    confirm_close: bool = True
    name: ClassVar[str] = "lb_confirm"


@dataclass(frozen=True)
class LBEarlyExit(LondonBreakout):
    flat_hour: float = 13.0
    name: ClassVar[str] = "lb_early_exit"


@dataclass(frozen=True)
class LBTightRange(LondonBreakout):
    max_range_atr: float = 0.5
    name: ClassVar[str] = "lb_tight_range"
