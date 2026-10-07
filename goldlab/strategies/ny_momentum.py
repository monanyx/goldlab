"""New York open momentum.

Measure the move from the open of the first bar at `open_hour` New York time to
the close `obs_minutes` later. If it exceeds `thresh_atr` x daily ATR, enter in
the same direction at the next bar's open. Hard stop `stop_atr` x daily ATR,
target `target_r` x stop. One signal per day; flat by `flat_hour` NY time
(an hour before the 17:00 NY rollover).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

import numpy as np
import pandas as pd

from ..engine import Order, StrategySignals, bar_minutes
from ..features import daily_atr, day_groups, ny_minutes
from .base import Strategy


@dataclass(frozen=True)
class NYMomentum(Strategy):
    open_hour: float = 8.5
    obs_minutes: int = 30
    thresh_atr: float = 0.15
    stop_atr: float = 0.25
    target_r: float = 2.0
    flat_hour: float = 16.0

    name: ClassVar[str] = "ny_momentum"
    grid: ClassVar[dict] = {
        "obs_minutes": [15, 30, 45, 60],
        "thresh_atr": [0.10, 0.15, 0.20],
        "stop_atr": [0.15, 0.25, 0.35],
        "target_r": [1.0, 1.5, 2.0, 3.0],
    }

    def signals(self, df: pd.DataFrame) -> StrategySignals:
        ny = ny_minutes(df)
        datr = daily_atr(df)
        o, c = df["open"].to_numpy(), df["close"].to_numpy()
        start = int(round(self.open_hour * 60))
        flat_min = int(self.flat_hour * 60)
        flat = ~((ny >= start) & (ny < flat_min))
        bar_min = bar_minutes(df.index) or 15
        orders: dict[int, list[Order]] = {}

        for idx in day_groups(df):
            n_day = ny[idx]
            sess = idx[(n_day >= start) & (n_day < flat_min)]
            if len(sess) == 0 or ny[sess[0]] != start:
                continue
            obs_end = start + self.obs_minutes - bar_min  # start minute of the last observed bar
            last = sess[ny[sess] == obs_end]
            if len(last) == 0:
                continue
            j = last[0]
            a = datr[j]
            if not np.isfinite(a) or a <= 0:
                continue
            move = c[j] - o[sess[0]]
            if abs(move) < self.thresh_atr * a:
                continue
            side = 1 if move > 0 else -1
            why = (f"NY open momentum: {move:+.2f} in first {self.obs_minutes} min "
                   f"({abs(move) / a:.2f}x daily ATR >= {self.thresh_atr})")
            orders[j] = [Order(side=side, kind="market", stop_dist=self.stop_atr * a,
                               target_r=self.target_r, tag="ny_mom", reason=why)]
        return StrategySignals(orders=orders, flat=flat)
