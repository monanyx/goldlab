"""Loading and cleaning XAUUSD intraday bars.

Everything is converted to UTC. Missing bars are *never* forward-filled with
invented prices: gaps are measured, reported and flagged (`gap_before`), and the
engine fills stops at the next real bar's open if price gapped through them.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from .sessions import NY_TZ, label_sessions, minute_diffs

PRICE_COLS = ["open", "high", "low", "close"]


@dataclass
class DataQuality:
    source_rows: int = 0
    rows_after_clean: int = 0
    duplicates_removed: int = 0
    invalid_rows_removed: int = 0
    weekend_rows_removed: int = 0
    spikes_removed: int = 0
    ohlc_fixed: int = 0
    bar_minutes: int = 0
    first_bar: str = ""
    last_bar: str = ""
    intraday_gaps: int = 0  # gaps > 3 bars that are not weekends or the daily 17:00 NY break
    largest_gaps: list = field(default_factory=list)

    def to_json(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2))


# --------------------------------------------------------------------------- loading


def _detect_and_parse(path: Path, source_tz: str | None) -> pd.DataFrame:
    """Read one of the supported CSV layouts into a UTC-indexed OHLCV frame."""
    head = path.read_text(errors="replace")[:2000].splitlines()
    first = head[0] if head else ""

    # MetaTrader 5 export: <DATE>\t<TIME>\t<OPEN>...
    if first.startswith("<DATE>"):
        raw = pd.read_csv(path, sep="\t")
        raw.columns = [c.strip("<>").lower() for c in raw.columns]
        ts = pd.to_datetime(raw["date"] + " " + raw["time"], format="%Y.%m.%d %H:%M:%S")
        vol = raw["tickvol"] if "tickvol" in raw else raw.get("vol", 0)
        df = pd.DataFrame(
            {c: raw[c].to_numpy() for c in PRICE_COLS} | {"volume": np.asarray(vol)},
            index=ts,
        )
        if source_tz is None:
            raise ValueError(
                "MetaTrader exports are in broker server time; pass --source-tz "
                "(often 'Etc/GMT-2' or 'EET')."
            )
        return _to_utc(df, source_tz)

    # HistData.com generic ASCII: 20240102 000000;open;high;low;close;vol (EST, no DST)
    if ";" in first and first[:8].isdigit():
        raw = pd.read_csv(path, sep=";", header=None,
                          names=["ts", "open", "high", "low", "close", "volume"])
        ts = pd.to_datetime(raw["ts"], format="%Y%m%d %H%M%S")
        df = raw.drop(columns="ts").set_index(ts)
        return _to_utc(df, source_tz or "Etc/GMT+5")

    raw = pd.read_csv(path)
    cols = {c: c.strip().lower() for c in raw.columns}
    raw = raw.rename(columns=cols)

    # Dukascopy export: "Gmt time" or "Local time", day-first timestamps.
    for tcol in ("gmt time", "local time"):
        if tcol in raw.columns:
            s = raw[tcol].astype(str).str.strip()
            if tcol == "local time":
                # e.g. "02.01.2024 00:00:00.000 GMT+0200"
                ts = pd.to_datetime(s.str.replace("GMT", "", regex=False),
                                    format="%d.%m.%Y %H:%M:%S.%f %z", utc=True)
            else:
                ts = pd.to_datetime(s, format="%d.%m.%Y %H:%M:%S.%f").dt.tz_localize("UTC")
            df = raw[PRICE_COLS].copy()
            df["volume"] = raw["volume"] if "volume" in raw else 0.0
            df.index = pd.DatetimeIndex(ts)
            return df

    # Canonical: timestamp,open,high,low,close[,volume]
    tcol = next((c for c in ("timestamp", "time", "datetime", "date") if c in raw.columns), None)
    if tcol is None or not set(PRICE_COLS) <= set(raw.columns):
        raise ValueError(
            f"Unrecognised CSV layout in {path}. Expected header "
            "'timestamp,open,high,low,close,volume' (see README)."
        )
    ts = pd.to_datetime(raw[tcol], utc=False, format="ISO8601")
    df = raw[PRICE_COLS].copy()
    df["volume"] = raw["volume"] if "volume" in raw else 0.0
    df.index = pd.DatetimeIndex(ts)
    if df.index.tz is None:
        return _to_utc(df, source_tz or "UTC")
    return _to_utc(df, None)


def _to_utc(df: pd.DataFrame, source_tz: str | None) -> pd.DataFrame:
    idx = df.index
    if idx.tz is None:
        idx = idx.tz_localize(source_tz or "UTC", ambiguous="NaT", nonexistent="NaT")
    df = df.copy()
    df.index = idx.tz_convert("UTC")
    return df[df.index.notna()]


def load_raw(paths: list[Path] | Path, source_tz: str | None = None) -> pd.DataFrame:
    if isinstance(paths, (str, Path)):
        paths = [Path(paths)]
    frames = [_detect_and_parse(Path(p), source_tz) for p in paths]
    df = pd.concat(frames)
    df.index.name = "timestamp"
    for c in PRICE_COLS + ["volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


# --------------------------------------------------------------------------- cleaning


def _bar_minutes(idx: pd.DatetimeIndex) -> int:
    diffs = minute_diffs(idx).round().astype(int)
    diffs = diffs[diffs > 0]
    return int(pd.Series(diffs).mode().iloc[0]) if len(diffs) else 0


def is_market_closed(idx: pd.DatetimeIndex) -> np.ndarray:
    """Weekend closure: Friday 17:00 NY -> Sunday 17:00 NY (Sunday 18:00 NY for gold)."""
    ny = idx.tz_convert(NY_TZ)
    dow = ny.dayofweek.to_numpy()
    hour = ny.hour.to_numpy()
    return ((dow == 4) & (hour >= 17)) | (dow == 5) | ((dow == 6) & (hour < 17))


def resample(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    out = df[PRICE_COLS + ["volume"]].resample(f"{minutes}min", label="left", closed="left").agg(agg)
    return out.dropna(subset=["open"])


def clean(df: pd.DataFrame, target_minutes: int | None = None,
          spike_threshold: float | None = 0.05) -> tuple[pd.DataFrame, DataQuality]:
    """Sort, dedupe, drop invalid/weekend/spike bars, optionally resample, label sessions."""
    q = DataQuality(source_rows=len(df))
    df = df.sort_index()

    dup = df.index.duplicated(keep="last")
    q.duplicates_removed = int(dup.sum())
    df = df[~dup]

    bad = df[PRICE_COLS].isna().any(axis=1) | (df[PRICE_COLS] <= 0).any(axis=1)
    q.invalid_rows_removed = int(bad.sum())
    df = df[~bad].copy()
    df["volume"] = df["volume"].fillna(0.0)

    closed = is_market_closed(df.index)
    q.weekend_rows_removed = int(closed.sum())
    df = df[~closed]

    # Bad ticks: a bar whose close is far from both neighbours' closes and
    # snaps straight back. Real gaps (one-sided moves) are kept.
    c = df["close"] if spike_threshold else df["close"].iloc[:0]
    spike_threshold = spike_threshold or 1.0
    dev_prev = (c / c.shift(1) - 1).abs()
    dev_next = (c / c.shift(-1) - 1).abs()
    back = (c.shift(1) / c.shift(-1) - 1).abs()
    spike = (dev_prev > spike_threshold) & (dev_next > spike_threshold) & (back < spike_threshold / 5)
    q.spikes_removed = int(spike.sum())
    df = df[~df.index.isin(spike[spike].index)].copy()

    hi = df[PRICE_COLS].max(axis=1)
    lo = df[PRICE_COLS].min(axis=1)
    fix = (df["high"] != hi) | (df["low"] != lo)
    q.ohlc_fixed = int(fix.sum())
    df["high"], df["low"] = hi, lo

    if target_minutes:
        native = _bar_minutes(df.index)
        if target_minutes < native:
            raise ValueError(f"Cannot downsample {native}m bars to {target_minutes}m")
        if target_minutes != native:
            df = resample(df, target_minutes)
            df = df[~is_market_closed(df.index)]

    q.bar_minutes = _bar_minutes(df.index)
    gap_min = np.r_[0, minute_diffs(df.index)]
    df["gap_before"] = gap_min > q.bar_minutes
    df = label_sessions(df)

    # Report intraday gaps (ignore weekend closures and the 17:00-18:00 NY break).
    gaps = pd.Series(gap_min, index=df.index)
    big = gaps[(gaps > 3 * q.bar_minutes) & (gaps < 47 * 60)]
    ny_hour = big.index.tz_convert(NY_TZ).hour
    big = big[~((ny_hour == 18) & (big <= 90))]
    q.intraday_gaps = int(len(big))
    q.largest_gaps = [
        {"bar_after_gap": str(ts), "minutes": int(m)}
        for ts, m in big.sort_values(ascending=False).head(10).items()
    ]
    q.rows_after_clean = len(df)
    if len(df):
        q.first_bar, q.last_bar = str(df.index[0]), str(df.index[-1])
    return df, q


def load_clean(paths, source_tz=None, target_minutes=None):
    return clean(load_raw(paths, source_tz), target_minutes)


def find_data_files(data_dir: Path) -> list[Path]:
    files = sorted(p for p in data_dir.glob("*") if p.suffix.lower() in {".csv", ".txt"})
    return [p for p in files if not p.name.startswith(".")]
