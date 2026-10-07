"""Optional downloader for Dukascopy 1-minute BID candles.

NOTE: written against Dukascopy's public datafeed layout but NOT exercised in
CI (the build environment cannot reach datafeed.dukascopy.com). The price
divisor is auto-detected and sanity-checked; inspect the output before use.

Each day file is LZMA-compressed binary, 24 bytes per minute:
  uint32 seconds-from-midnight-UTC, uint32 open, close, low, high, float32 volume
"""

from __future__ import annotations

import lzma
import struct
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

URL = "https://datafeed.dukascopy.com/datafeed/{sym}/{y}/{m:02d}/{d:02d}/{side}_candles_min_1.bi5"
REC = struct.Struct(">5If")


def _decode(blob: bytes, day: pd.Timestamp) -> pd.DataFrame:
    if not blob:
        return pd.DataFrame()
    raw = lzma.decompress(blob)
    rows = [REC.unpack_from(raw, k) for k in range(0, len(raw) - len(raw) % REC.size, REC.size)]
    a = np.array(rows, dtype=float)
    if a.size == 0:
        return pd.DataFrame()
    idx = day + pd.to_timedelta(a[:, 0], unit="s")
    df = pd.DataFrame({"open": a[:, 1], "close": a[:, 2], "low": a[:, 3], "high": a[:, 4],
                       "volume": a[:, 5]}, index=idx)
    return df[df["volume"] > 0]  # Dukascopy pads closed minutes with zero-volume flat candles


def _scale(df: pd.DataFrame) -> pd.DataFrame:
    med = df["close"].median()
    for div in (1000.0, 100.0, 10000.0, 100000.0, 10.0):
        if 300 <= med / div <= 20000:
            df[["open", "high", "low", "close"]] /= div
            return df
    raise ValueError(f"Could not infer price scale (median raw price {med})")


def fetch(start: str, end: str, out: Path, symbol: str = "XAUUSD", side: str = "BID",
          sleep: float = 0.2) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    frames = []
    for day in pd.date_range(start, end, freq="D", tz="UTC", inclusive="left"):
        if day.dayofweek == 5:
            continue
        url = URL.format(sym=symbol, y=day.year, m=day.month - 1, d=day.day, side=side)
        for attempt in range(4):
            try:
                with urllib.request.urlopen(url, timeout=30) as r:
                    frames.append(_decode(r.read(), day))
                break
            except Exception:  # noqa: BLE001
                if attempt == 3:
                    raise
                time.sleep(2 ** (attempt + 1))
        time.sleep(sleep)
    df = _scale(pd.concat(frames)).sort_index()
    df.index.name = "timestamp"
    m5 = df.resample("5min", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna(subset=["open"])
    path = out / f"{symbol}_M5_{start}_{end}_dukascopy_{side.lower()}.csv"
    m5.to_csv(path, date_format="%Y-%m-%dT%H:%M:%SZ")
    return path
