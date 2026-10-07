"""Downloader for Dukascopy 1-minute BID candles (free, no account needed).

Used by the `research` GitHub Actions workflow. Days without a file (holidays,
today, not yet published) are skipped. The price divisor is auto-detected and
sanity-checked; `python -m goldlab check-data` shows what was downloaded.

Each day file is LZMA-compressed binary, 24 bytes per minute:
  uint32 seconds-from-midnight-UTC, uint32 open, close, low, high, float32 volume
"""

from __future__ import annotations

import lzma
import struct
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

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


def _get(url: str, timeout: float = 30) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (goldlab research)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None  # no file for that day
        raise


def fetch(start: str, end: str, out: Path, symbol: str = "XAUUSD", side: str = "BID",
          sleep: float = 0.1, get: Callable[[str], bytes | None] = _get, log=print) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    days = [d for d in pd.date_range(start, end, freq="D", tz="UTC", inclusive="left") if d.dayofweek != 5]
    frames = []
    for n, day in enumerate(days):
        url = URL.format(sym=symbol, y=day.year, m=day.month - 1, d=day.day, side=side)
        for attempt in range(4):
            try:
                blob = get(url)
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 3:
                    raise RuntimeError(f"Dukascopy download failed for {day:%Y-%m-%d}: {e}") from e
                time.sleep(2 ** (attempt + 1))
        if blob:
            frames.append(_decode(blob, day))
        if n % 50 == 0 or n == len(days) - 1:
            log(f"[dukascopy] {day:%Y-%m-%d} ({n + 1}/{len(days)} days)", flush=True)
        if sleep:
            time.sleep(sleep)
    frames = [f for f in frames if not f.empty]
    if not frames:
        raise RuntimeError("Dukascopy returned no data for the requested period")
    df = _scale(pd.concat(frames)).sort_index()
    df = df[~df.index.duplicated(keep="last")]
    df.index.name = "timestamp"
    m5 = df.resample("5min", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna(subset=["open"])
    path = out / f"{symbol}_M5_{start}_{end}_dukascopy_{side.lower()}.csv"
    m5.to_csv(path, date_format="%Y-%m-%dT%H:%M:%SZ")
    log(f"[dukascopy] wrote {len(m5):,} M5 bars from {m5.index[0]} to {m5.index[-1]} -> {path}", flush=True)
    return path
