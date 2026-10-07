"""London breakout of the Asian range.

Asia range = high/low of the trade date's bars from 00:00 UTC up to the London
open (08:00 London time). At the London open, OCO buy-stop / sell-stop orders
are placed `buffer_atr` x daily ATR beyond the range. The first to trigger
cancels the other. Hard stop `stop_frac` x range width (+ buffer) from entry,
target `target_r` x stop. Entries only during the first `entry_hours` of London;
everything is flat by `flat_hour` London time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

import numpy as np
import pandas as pd

from ..engine import Order, StrategySignals
from ..features import daily_atr, day_groups, london_minutes
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

    name: ClassVar[str] = "london_breakout"
    grid: ClassVar[dict] = {
        "entry_hours": [2.0, 3.0, 4.0],
        "buffer_atr": [0.0, 0.05, 0.10],
        "stop_frac": [0.5, 0.75, 1.0],
        "target_r": [1.0, 1.5, 2.0, 2.5],
    }

    def signals(self, df: pd.DataFrame) -> StrategySignals:
        lon = london_minutes(df)
        utc_min = (df.index.hour * 60 + df.index.minute).to_numpy()
        datr = daily_atr(df)
        hi, lo = df["high"].to_numpy(), df["low"].to_numpy()
        flat_min = int(self.flat_hour * 60)
        entry_end = LONDON_OPEN + int(self.entry_hours * 60)
        flat = ~((lon >= LONDON_OPEN) & (lon < flat_min) & (utc_min >= 6 * 60))
        orders: dict[int, list[Order]] = {}

        for idx in day_groups(df):
            l_day, u_day = lon[idx], utc_min[idx]
            in_range = (u_day < 13 * 60) & (l_day < LONDON_OPEN)
            asia = idx[in_range]
            opens = idx[(l_day >= LONDON_OPEN) & (l_day < entry_end) & (u_day >= 6 * 60)]
            if len(asia) < 12 or len(opens) == 0:
                continue
            k = opens[0]
            a = datr[k]
            if not np.isfinite(a) or a <= 0 or k - 1 != asia[-1]:
                continue
            r_hi, r_lo = hi[asia].max(), lo[asia].min()
            width = r_hi - r_lo
            if not (self.min_range_atr * a <= width <= self.max_range_atr * a):
                continue
            buf = self.buffer_atr * a
            dist = self.stop_frac * width + buf
            grp = f"lb{k}"
            orders[k - 1] = [
                Order(side=1, kind="stop", price=r_hi + buf, stop_dist=dist,
                      target_r=self.target_r, expires=opens[-1], group=grp, tag="long_break"),
                Order(side=-1, kind="stop", price=r_lo - buf, stop_dist=dist,
                      target_r=self.target_r, expires=opens[-1], group=grp, tag="short_break"),
            ]
        return StrategySignals(orders=orders, flat=flat)
