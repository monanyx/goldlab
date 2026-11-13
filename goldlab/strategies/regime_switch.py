"""Regime switch: read the day at the London open, then trade the strategy that fits.

Pre-registered in docs/regime-switch-test.md (2026-11-13). Everything below is
decided at the close of the last Asia bar before the London open (08:00 London),
from data known at that moment:

  ATR               average daily range of the previous 14 trade days (daily_atr)
  trend distance    yesterday's close minus its `trend_days`-day average, in ATRs
  yesterday range   yesterday's high minus low, in ATRs
  Asia range        high/low from 00:00 UTC to the London open (as london_breakout)

Day types, checked in this order:

  chaos   yesterday's range >= `chaos_atr` ATR, or the Asia range is outside
          [`min_range_atr`, `max_range_atr`] ATR             -> no trade
  trend   |trend distance| >= `trend_atr` ATR                 -> London breakout in
          the trend's direction only (one stop order, baseline levels/stop/target)
  range   otherwise                                           -> fade a false break:
          within `entry_hours` of the open, price trades more than the buffer
          beyond one edge of the Asia range and a bar then *closes* back inside.
          Enter at the next bar's open the other way; stop `buffer_atr` ATR beyond
          the furthest point of the false break; target the opposite range edge;
          skip if target / stop < `fade_min_rr`. The first close back inside
          decides the day.

Everything is flat by `flat_hour` London time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

import numpy as np
import pandas as pd

from ..engine import Order, StrategySignals, bar_minutes
from ..features import daily_atr, daily_trend_distance, day_groups, london_minutes, prev_day_range
from .base import Strategy

LONDON_OPEN = 8 * 60

# Day-type labels (also the prefix of every trade tag).
CHAOS, TREND, RANGE, NO_DATA = "chaos", "trend", "range", "no_data"


@dataclass(frozen=True)
class RegimeSwitch(Strategy):
    entry_hours: float = 3.0
    fade_min_rr: float = 1.0
    target_r: float = 1.5
    trend_atr: float = 1.0
    trend_days: int = 20
    chaos_atr: float = 2.0
    buffer_atr: float = 0.05
    stop_frac: float = 1.0
    min_range_atr: float = 0.15
    max_range_atr: float = 0.8
    flat_hour: float = 16.0

    name: ClassVar[str] = "regime_switch"
    # Order matters: the sensitivity heatmap plots the last two keys.
    grid: ClassVar[dict] = {
        "entry_hours": [2.0, 3.0, 4.0],
        "fade_min_rr": [0.5, 1.0, 1.5],
        "target_r": [1.0, 1.5, 2.0],
        "trend_atr": [0.5, 1.0, 1.5],
    }
    # Adoption also needs the untouched holdout (docs/regime-switch-test.md).
    holdout_required: ClassVar[bool] = True

    def required_history_days(self) -> int:
        return max(15, self.trend_days + 2)

    def signals(self, df: pd.DataFrame) -> StrategySignals:
        orders, flat, _ = self._scan(df)
        return StrategySignals(orders=orders, flat=flat)

    def day_types(self, df: pd.DataFrame) -> pd.Series:
        """Day type decided at each London open, indexed by trade date."""
        return self._scan(df)[2]

    def classify(self, trend_dist: float, prev_range: float, width: float, atr: float) -> str:
        """Day type from the London-open inputs (all in price units)."""
        if not all(np.isfinite(v) for v in (trend_dist, prev_range, width, atr)) or atr <= 0:
            return NO_DATA
        if prev_range >= self.chaos_atr * atr:
            return CHAOS
        if not (self.min_range_atr * atr <= width <= self.max_range_atr * atr):
            return CHAOS
        if abs(trend_dist) >= self.trend_atr * atr:
            return TREND
        return RANGE

    # ------------------------------------------------------------------ internals

    def _scan(self, df: pd.DataFrame):
        lon = london_minutes(df)
        times = df.index
        utc_min = (times.hour * 60 + times.minute).to_numpy()
        datr = daily_atr(df)
        tdist = daily_trend_distance(df, self.trend_days)
        prng = prev_day_range(df)
        hi, lo, close = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
        bar_min = bar_minutes(df.index) or 15
        flat_min = int(self.flat_hour * 60)
        flat = ~((lon >= LONDON_OPEN) & (lon < flat_min) & (utc_min >= 6 * 60))
        orders: dict[int, list[Order]] = {}
        types: dict[pd.Timestamp, str] = {}

        for idx in day_groups(df):
            l_day, u_day = lon[idx], utc_min[idx]
            asia = idx[(u_day < 13 * 60) & (l_day < LONDON_OPEN)]
            if len(asia) < 12:
                continue
            k = asia[-1]
            # Decided at the close of the last bar before the London open, exactly
            # as in london_breakout, so backtest and live run agree.
            if lon[k] + bar_min != LONDON_OPEN:
                continue
            a = datr[k]
            r_hi, r_lo = hi[asia].max(), lo[asia].min()
            width = r_hi - r_lo
            day = self.classify(tdist[k], prng[k], width, a)
            types[pd.Timestamp(df["trade_date"].iloc[k])] = day
            if day in (CHAOS, NO_DATA):
                continue

            buf = self.buffer_atr * a
            london_open = times[k] + pd.Timedelta(minutes=bar_min)
            expires = london_open + pd.Timedelta(hours=self.entry_hours)
            grp = f"rs{london_open:%Y%m%d}"
            rng = f"Asia range {r_lo:.2f}-{r_hi:.2f} (width {width:.2f} = {width / a:.2f}x daily ATR)"
            dist_atr = tdist[k] / a

            if day == TREND:
                side = 1 if dist_atr > 0 else -1
                why = (f"Trend day (yesterday's close {abs(dist_atr):.2f}x ATR "
                       f"{'above' if side == 1 else 'below'} its {self.trend_days}-day average): "
                       f"{'break above' if side == 1 else 'break below'} {rng}; "
                       f"{'long' if side == 1 else 'short'} only, valid until {expires:%H:%M} UTC")
                orders[k] = [Order(
                    side=side, kind="stop", price=r_hi + buf if side == 1 else r_lo - buf,
                    stop_dist=self.stop_frac * width + buf, target_r=self.target_r,
                    expires=expires, group=grp,
                    tag="trend_long_break" if side == 1 else "trend_short_break", reason=why)]
                continue

            # RANGE day: fade the first false break that closes back inside.
            window = idx[(times[idx] >= london_open) & (times[idx] < expires)]
            hi_since, lo_since = -np.inf, np.inf
            last_poke = 0  # +1 = poked above the range, -1 = below, 0 = none / both in one bar
            for j in window:
                hi_since, lo_since = max(hi_since, hi[j]), min(lo_since, lo[j])
                up, down = hi[j] > r_hi + buf, lo[j] < r_lo - buf
                if up or down:
                    last_poke = 0 if (up and down) else (1 if up else -1)
                if last_poke == 1 and close[j] < r_hi:
                    side, stop, target = -1, hi_since + buf, r_lo
                elif last_poke == -1 and close[j] > r_lo:
                    side, stop, target = 1, lo_since - buf, r_hi
                else:
                    continue
                risk = (close[j] - stop) * side
                reward = (target - close[j]) * side
                if risk > 0 and reward >= self.fade_min_rr * risk:
                    edge = "above" if side == -1 else "below"
                    orders[j] = [Order(
                        side=side, kind="market", stop_price=stop, target_price=target, group=grp,
                        tag="range_fade_long" if side == 1 else "range_fade_short",
                        reason=(f"Range day (yesterday's close {abs(dist_atr):.2f}x ATR from its "
                                f"{self.trend_days}-day average): false break {edge} the {rng}, "
                                f"closed back inside at {times[j] + pd.Timedelta(minutes=bar_min):%H:%M} UTC; "
                                f"fade to the {'high' if side == 1 else 'low'} of the range"))]
                break  # the first close back inside decides the day
        return orders, flat, pd.Series(types, dtype=object, name="day_type")
