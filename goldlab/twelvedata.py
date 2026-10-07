"""Historical XAU/USD bars from Twelve Data (fallback research data source).

Pages backwards through `time_series` 5,000 bars at a time. The free plan allows
8 requests per minute, so requests are spaced out. How far back the free plan
goes for intraday data is set by Twelve Data; `check-data` shows what arrived.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

import pandas as pd

URL = "https://api.twelvedata.com/time_series"
PAGE = 5000


def _get(params: dict) -> dict:
    url = f"{URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "goldlab research"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def fetch(start: str, end: str, out: Path, api_key: str | None = None, symbol: str = "XAU/USD",
          minutes: int = 15, sleep: float = 8.0, get: Callable[[dict], dict] = _get, log=print) -> Path:
    api_key = api_key or os.environ.get("TWELVEDATA_API_KEY", "")
    if not api_key:
        raise RuntimeError("TWELVEDATA_API_KEY is not set")
    out.mkdir(parents=True, exist_ok=True)
    start_ts = pd.Timestamp(start, tz="UTC")
    cursor = pd.Timestamp(end, tz="UTC")
    frames = []
    while cursor > start_ts:
        data = get({"symbol": symbol, "interval": f"{minutes}min", "outputsize": PAGE, "timezone": "UTC",
                    "order": "desc", "start_date": start_ts.strftime("%Y-%m-%d %H:%M:%S"),
                    "end_date": cursor.strftime("%Y-%m-%d %H:%M:%S"), "apikey": api_key})
        if data.get("status") != "ok":
            msg = str(data.get("message", data))
            if "no data" in msg.lower() and frames:
                break  # reached the start of what the plan provides
            raise RuntimeError(f"Twelve Data error: {msg}")
        values = data.get("values") or []
        if not values:
            break
        page = pd.DataFrame(values)
        idx = pd.DatetimeIndex(pd.to_datetime(page["datetime"])).tz_localize("UTC")
        frames.append(pd.DataFrame({c: pd.to_numeric(page[c]).to_numpy(float)
                                    for c in ("open", "high", "low", "close")}, index=idx))
        oldest = idx.min()
        log(f"[twelvedata] got {len(values)} bars back to {oldest}", flush=True)
        if len(values) < PAGE or oldest <= start_ts:
            break
        cursor = oldest - pd.Timedelta(seconds=1)
        if sleep:
            time.sleep(sleep)
    if not frames:
        raise RuntimeError("Twelve Data returned no data for the requested period")
    df = pd.concat(frames).sort_index()
    df = df[~df.index.duplicated(keep="last")]
    df["volume"] = 0.0
    df.index.name = "timestamp"
    path = out / f"XAUUSD_M{minutes}_{start}_{end}_twelvedata.csv"
    df.to_csv(path, date_format="%Y-%m-%dT%H:%M:%SZ")
    log(f"[twelvedata] wrote {len(df):,} bars from {df.index[0]} to {df.index[-1]} -> {path}", flush=True)
    return path
