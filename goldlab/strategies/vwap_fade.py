"""Session-VWAP fade (mean reversion).

VWAP is anchored at the London open each trade date, weighted by tick volume
(falls back to equal weights if the feed has no volume). When a bar closes more
than `dev_atr` x daily ATR away from VWAP *and* closes back toward it relative to
the previous bar (a minimal exhaustion filter), fade the move with a market
order at the next open. Target = VWAP at signal time, hard stop `stop_atr` x
daily ATR beyond entry. Entries between `start_hour` London and `end_hour` NY,
flat by `flat_hour` NY.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

import numpy as np
import pandas as pd

from ..engine import Order, StrategySignals
from ..features import daily_atr, day_groups, london_minutes, ny_minutes
from .base import Strategy

LONDON_OPEN = 8 * 60


@dataclass(frozen=True)
class VWAPFade(Strategy):
    dev_atr: float = 0.35
    stop_atr: float = 0.25
    start_hour: float = 10.0  # London time
    end_hour: float = 14.0  # NY time
    flat_hour: float = 16.0  # NY time
    min_reward_risk: float = 0.8

    name: ClassVar[str] = "vwap_fade"
    grid: ClassVar[dict] = {
        "dev_atr": [0.25, 0.30, 0.35, 0.45, 0.55],
        "stop_atr": [0.15, 0.20, 0.25, 0.35],
        "min_reward_risk": [0.5, 0.8, 1.2],
    }

    def signals(self, df: pd.DataFrame) -> StrategySignals:
        lon, ny = london_minutes(df), ny_minutes(df)
        datr = daily_atr(df)
        h, l, c = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
        vol = df["volume"].to_numpy(float)
        if not np.isfinite(vol).any() or np.nansum(vol) <= 0:
            vol = np.ones(len(df))
        vol = np.where(np.isfinite(vol) & (vol > 0), vol, 0.0)
        typical = (h + l + c) / 3.0

        start_min, end_min = int(self.start_hour * 60), int(self.end_hour * 60)
        flat_min = int(self.flat_hour * 60)
        active = (lon >= LONDON_OPEN) & (ny < flat_min) & (ny >= 2 * 60)
        flat = ~active
        orders: dict[int, list[Order]] = {}

        for idx in day_groups(df):
            sess = idx[active[idx]]
            if len(sess) < 3:
                continue
            w = vol[sess]
            if w.sum() <= 0:
                w = np.ones(len(sess))
            vwap = np.cumsum(typical[sess] * w) / np.maximum(np.cumsum(w), 1e-12)
            for pos in range(1, len(sess)):
                i = sess[pos]
                if not (lon[i] >= start_min and ny[i] < end_min):
                    continue
                a = datr[i]
                if not np.isfinite(a) or a <= 0:
                    continue
                dev = c[i] - vwap[pos]
                if abs(dev) < self.dev_atr * a:
                    continue
                side = -1 if dev > 0 else 1
                turned = (c[i] - c[sess[pos - 1]]) * side > 0
                if not turned:
                    continue
                stop_d = self.stop_atr * a
                if abs(dev) < self.min_reward_risk * stop_d:
                    continue
                orders[i] = [Order(side=side, kind="market", stop_dist=stop_d,
                                   target_price=vwap[pos], expires=i + 1,
                                   group="vwap", tag="vwap_fade")]
        return StrategySignals(orders=orders, flat=flat)
