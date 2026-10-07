"""Trading-session definitions. All timestamps are UTC; sessions are defined in
local exchange time so they follow daylight-saving shifts automatically.

- Asia:     00:00-07:00 UTC (Tokyo/Singapore/HK; no DST)
- London:   08:00-16:30 Europe/London
- New York: 08:00-17:00 America/New_York (17:00 NY is the daily FX/metals rollover)

The *trading day* runs from one 17:00 New York rollover to the next, so the
Sunday-evening open belongs to Monday's trading day.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

LONDON_TZ = "Europe/London"
NY_TZ = "America/New_York"

ASIA_UTC = (0, 7 * 60)  # minutes after UTC midnight
LONDON_LOCAL = (8 * 60, 16 * 60 + 30)
NY_LOCAL = (8 * 60, 17 * 60)


def minute_diffs(idx: pd.DatetimeIndex) -> np.ndarray:
    """Minutes between consecutive timestamps (independent of the index's time unit)."""
    return np.diff(idx.as_unit("s").asi8) / 60.0


def _minutes(idx: pd.DatetimeIndex) -> np.ndarray:
    return (idx.hour * 60 + idx.minute).to_numpy()


def local_minutes(idx: pd.DatetimeIndex, tz: str) -> np.ndarray:
    """Minutes after local midnight for each UTC timestamp."""
    return _minutes(idx.tz_convert(tz))


def trading_day(idx: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Trading date: NY time shifted +7h, so 17:00 NY rolls into the next day."""
    ny = idx.tz_convert(NY_TZ) + pd.Timedelta(hours=7)
    return pd.DatetimeIndex(ny.date)


def label_sessions(df: pd.DataFrame) -> pd.DataFrame:
    """Add is_asia/is_london/is_ny flags, a combined `session` label and `trade_date`."""
    idx = df.index
    utc_min = _minutes(idx)
    lon_min = local_minutes(idx, LONDON_TZ)
    ny_min = local_minutes(idx, NY_TZ)

    out = df.copy()
    out["is_asia"] = (utc_min >= ASIA_UTC[0]) & (utc_min < ASIA_UTC[1])
    out["is_london"] = (lon_min >= LONDON_LOCAL[0]) & (lon_min < LONDON_LOCAL[1])
    out["is_ny"] = (ny_min >= NY_LOCAL[0]) & (ny_min < NY_LOCAL[1])

    session = np.full(len(out), "Off", dtype=object)
    session[out["is_asia"].to_numpy()] = "Asia"
    session[out["is_london"].to_numpy()] = "London"
    session[out["is_ny"].to_numpy()] = "NewYork"
    session[(out["is_london"] & out["is_ny"]).to_numpy()] = "London+NewYork"
    out["session"] = session
    out["trade_date"] = trading_day(idx)
    return out


def cost_session(label: str) -> str:
    """Map a session label onto the key used for spread multipliers."""
    if label == "Asia":
        return "Asia"
    if label == "Off":
        return "Off"
    return "Liquid"
