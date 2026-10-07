"""Look-ahead-free features shared by strategies."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .sessions import LONDON_TZ, NY_TZ, local_minutes


def daily_atr(df: pd.DataFrame, n: int = 14) -> np.ndarray:
    """Average trading-day range over the previous `n` completed days, per bar.

    Uses only days strictly before the bar's trade date, so it is known at the
    start of each day.
    """
    rng = df.groupby("trade_date").agg(hi=("high", "max"), lo=("low", "min"))
    atr = (rng["hi"] - rng["lo"]).rolling(n, min_periods=max(3, n // 2)).mean().shift(1)
    return df["trade_date"].map(atr).to_numpy(float)


def bar_atr(df: pd.DataFrame, n: int = 14) -> np.ndarray:
    """Rolling mean true range over the last `n` bars, inclusive of the current bar."""
    prev_close = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev_close).abs(),
                    (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=n).mean().to_numpy(float)


def london_minutes(df: pd.DataFrame) -> np.ndarray:
    return local_minutes(df.index, LONDON_TZ)


def ny_minutes(df: pd.DataFrame) -> np.ndarray:
    return local_minutes(df.index, NY_TZ)


def day_groups(df: pd.DataFrame) -> list[np.ndarray]:
    """Positional indices for each trade date, in order."""
    codes = pd.factorize(df["trade_date"])[0]
    bounds = np.flatnonzero(np.diff(codes)) + 1
    return np.split(np.arange(len(df)), bounds)
