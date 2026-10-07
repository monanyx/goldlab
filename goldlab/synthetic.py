"""Synthetic XAUUSD-like bars for tests and pipeline smoke runs.

This is a driftless random walk: any 'edge' a strategy shows on it is noise
plus costs. NEVER use it to judge a strategy.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .data import is_market_closed
from .sessions import NY_TZ


def random_walk_bars(start="2023-01-02", end="2023-03-01", minutes=5, seed=0,
                     price=1900.0) -> pd.DataFrame:
    idx = pd.date_range(start, end, freq=f"{minutes}min", tz="UTC", inclusive="left")
    idx = idx[~is_market_closed(idx)]
    ny_h = idx.tz_convert(NY_TZ).hour
    idx = idx[ny_h != 17]  # daily maintenance break
    rng = np.random.default_rng(seed)
    utc_h = idx.hour.to_numpy()
    vol = np.where((utc_h >= 7) & (utc_h < 17), 0.0009, 0.0004) * np.sqrt(minutes / 5)
    rets = rng.normal(0.0, vol)
    close = price * np.exp(np.cumsum(rets))
    open_ = np.r_[price, close[:-1]]
    wick = np.abs(rng.normal(0, vol * 0.6)) * close
    high = np.maximum(open_, close) + wick
    low = np.minimum(open_, close) - np.abs(rng.normal(0, vol * 0.6)) * close
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                         "volume": rng.integers(50, 500, len(idx)).astype(float)}, index=idx)
