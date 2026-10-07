"""Price feeds. Every feed returns *completed* bars only, UTC-indexed OHLC(V)."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pandas as pd

from goldlab.data import find_data_files, load_raw

from . import http


class FeedError(RuntimeError):
    pass


class Feed:
    name = "feed"

    def bars(self, now: pd.Timestamp) -> pd.DataFrame:  # pragma: no cover
        raise NotImplementedError


def completed(df: pd.DataFrame, now: pd.Timestamp, minutes: int) -> pd.DataFrame:
    """Drop the still-forming bar (and anything from the future)."""
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df[df.index + pd.Timedelta(minutes=minutes) <= now]


class TwelveDataFeed(Feed):
    """https://twelvedata.com — free tier: 800 requests/day, 8/minute. XAU/USD has no volume."""

    name = "twelvedata"
    URL = "https://api.twelvedata.com/time_series"

    def __init__(self, api_key: str, symbol: str = "XAU/USD", minutes: int = 15, bars: int = 3000,
                 get: Callable = http.request):
        if not api_key:
            raise FeedError("TWELVEDATA_API_KEY is not set")
        self.api_key, self.symbol, self.minutes, self.n, self._get = api_key, symbol, minutes, bars, get

    def bars(self, now: pd.Timestamp) -> pd.DataFrame:
        params = {"symbol": self.symbol, "interval": f"{self.minutes}min", "outputsize": min(self.n, 5000),
                  "timezone": "UTC", "order": "ASC", "apikey": self.api_key}
        data = self._get("GET", self.URL, params=params).json()
        if not isinstance(data, dict) or data.get("status") != "ok":
            msg = data.get("message", data) if isinstance(data, dict) else data
            raise FeedError(f"Twelve Data error: {msg}")
        df = pd.DataFrame(data["values"])
        idx = pd.DatetimeIndex(pd.to_datetime(df["datetime"])).tz_localize("UTC")
        out = pd.DataFrame({c: pd.to_numeric(df[c]).to_numpy(float) for c in ("open", "high", "low", "close")},
                           index=idx)
        out["volume"] = pd.to_numeric(df["volume"]).to_numpy(float) if "volume" in df else 0.0
        out.index.name = "timestamp"
        return completed(out, now, self.minutes)


class AlphaVantageFeed(Feed):
    """https://www.alphavantage.co — FX_INTRADAY. Note: intraday FX is a premium endpoint."""

    name = "alphavantage"
    URL = "https://www.alphavantage.co/query"

    def __init__(self, api_key: str, minutes: int = 15, get: Callable = http.request):
        if not api_key:
            raise FeedError("ALPHAVANTAGE_API_KEY is not set")
        if minutes not in (1, 5, 15, 30, 60):
            raise FeedError("Alpha Vantage supports 1/5/15/30/60 minute bars")
        self.api_key, self.minutes, self._get = api_key, minutes, get

    def bars(self, now: pd.Timestamp) -> pd.DataFrame:
        params = {"function": "FX_INTRADAY", "from_symbol": "XAU", "to_symbol": "USD",
                  "interval": f"{self.minutes}min", "outputsize": "full", "apikey": self.api_key}
        data = self._get("GET", self.URL, params=params).json()
        key = f"Time Series FX ({self.minutes}min)"
        if key not in data:
            msg = data.get("Error Message") or data.get("Information") or data.get("Note") or data
            raise FeedError(f"Alpha Vantage error: {msg}")
        rows = data[key]
        idx = pd.DatetimeIndex(pd.to_datetime(list(rows))).tz_localize("UTC")
        out = pd.DataFrame({
            "open": [float(v["1. open"]) for v in rows.values()],
            "high": [float(v["2. high"]) for v in rows.values()],
            "low": [float(v["3. low"]) for v in rows.values()],
            "close": [float(v["4. close"]) for v in rows.values()],
        }, index=idx)
        out["volume"] = 0.0
        return completed(out.sort_index(), now, self.minutes)


class FrameFeed(Feed):
    """Serves bars from an in-memory frame as if the clock were `now` (replays and tests)."""

    name = "frame"

    def __init__(self, df: pd.DataFrame, minutes: int = 15, bars: int = 3000):
        self.df, self.minutes, self.n = df.sort_index(), minutes, bars

    def bars(self, now: pd.Timestamp) -> pd.DataFrame:
        end = self.df.index.searchsorted(now - pd.Timedelta(minutes=self.minutes), side="right")
        return self.df.iloc[max(0, end - self.n):end][["open", "high", "low", "close", "volume"]]


class CSVFeed(FrameFeed):
    name = "csv"

    def __init__(self, path: str | Path, minutes: int = 15, bars: int = 3000):
        p = Path(path)
        files = find_data_files(p) if p.is_dir() else [p]
        if not files:
            raise FeedError(f"no CSV files in {p}")
        super().__init__(load_raw(files), minutes, bars)


class FallbackFeed(Feed):
    """Try feeds in order; the first that works wins."""

    name = "fallback"

    def __init__(self, feeds: list[Feed]):
        self.feeds = feeds

    def bars(self, now):
        errors = []
        for f in self.feeds:
            try:
                return f.bars(now)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{f.name}: {e}")
        raise FeedError("; ".join(errors) or "no feeds configured")


def make_feed(cfg) -> Feed:
    feeds: list[Feed] = []
    order = [cfg.data_provider] + [p for p in ("twelvedata", "alphavantage") if p != cfg.data_provider]
    for p in order:
        if p == "csv":
            feeds.append(CSVFeed(cfg.csv_path, cfg.timeframe_minutes, cfg.history_bars))
        elif p == "twelvedata" and cfg.twelvedata_api_key:
            feeds.append(TwelveDataFeed(cfg.twelvedata_api_key, cfg.symbol, cfg.timeframe_minutes, cfg.history_bars))
        elif p == "alphavantage" and cfg.alphavantage_api_key:
            feeds.append(AlphaVantageFeed(cfg.alphavantage_api_key, cfg.timeframe_minutes))
    if not feeds:
        raise FeedError("no data provider configured: set TWELVEDATA_API_KEY (recommended) "
                        "or ALPHAVANTAGE_API_KEY, or data_provider = 'csv'")
    return feeds[0] if len(feeds) == 1 else FallbackFeed(feeds)
