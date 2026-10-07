"""Historical-data downloaders (network mocked) and replay argument handling."""

import lzma
from dataclasses import replace

import pandas as pd
import pytest

from goldlab import dukascopy, twelvedata
from goldlab.data import load_raw


def _bi5(base_price_points: int, minutes: int = 3) -> bytes:
    recs = [(60 * k, base_price_points + k, base_price_points + k + 50, base_price_points - 100,
             base_price_points + 200, 1.0) for k in range(minutes)]
    return lzma.compress(b"".join(dukascopy.REC.pack(*r) for r in recs), format=lzma.FORMAT_ALONE)


def quiet(*a, **k):
    pass


def test_dukascopy_fetch_skips_missing_days_and_writes_canonical_csv(tmp_path):
    calls = []

    def fake_get(url):
        calls.append(url)
        if "/2024/00/10/" in url:
            return None  # 404: no file that day
        return _bi5(2_050_000)

    path = dukascopy.fetch("2024-01-08", "2024-01-14", tmp_path, sleep=0, get=fake_get, log=quiet)
    # Mon 8 .. Sat 13 + Sun excluded by inclusive="left" end; Saturday never requested
    assert not any("/2024/00/13/" in u for u in calls)
    assert "/2024/00/08/BID_candles_min_1.bi5" in calls[0]  # months are zero-based
    df = load_raw(path)
    assert str(df.index.tz) == "UTC"
    assert df["close"].between(2049, 2052).all()  # points / 1000
    days = set(df.index.date)
    assert pd.Timestamp("2024-01-10").date() not in days and len(days) == 4


def test_dukascopy_retries_then_fails_loudly(tmp_path, monkeypatch):
    monkeypatch.setattr(dukascopy.time, "sleep", lambda s: None)

    def broken(url):
        raise OSError("connection reset")

    with pytest.raises(RuntimeError, match="Dukascopy download failed"):
        dukascopy.fetch("2024-01-08", "2024-01-09", tmp_path, sleep=0, get=broken, log=quiet)


def test_twelvedata_history_pages_backwards(tmp_path):
    def page(end, n):
        times = pd.date_range(end=end, periods=n, freq="15min")[::-1]  # newest first
        return {"status": "ok", "values": [
            {"datetime": f"{t:%Y-%m-%d %H:%M:%S}", "open": "2000", "high": "2001", "low": "1999", "close": "2000.5"}
            for t in times]}

    seen = []

    def fake_get(params):
        seen.append(params["end_date"])
        assert params["apikey"] == "k" and params["outputsize"] == 5000
        end = pd.Timestamp(params["end_date"]).floor("15min")
        return page(end, 5000 if len(seen) == 1 else 1200)

    path = twelvedata.fetch("2023-01-01", "2024-06-01", tmp_path, api_key="k", sleep=0, get=fake_get, log=quiet)
    df = load_raw(path)
    assert len(seen) == 2 and len(df) == 6200 and df.index.is_monotonic_increasing
    assert not df.index.duplicated().any()


def test_twelvedata_error_is_raised(tmp_path):
    with pytest.raises(RuntimeError, match="apikey"):
        twelvedata.fetch("2024-01-01", "2024-02-01", tmp_path, api_key="bad", sleep=0, log=quiet,
                         get=lambda p: {"status": "error", "message": "invalid apikey"})


def test_replay_accepts_plain_date_strings(tmp_path):
    from goldbot.config import BotConfig
    from goldbot.replay import replay
    from goldlab.synthetic import random_walk_bars

    raw = random_walk_bars("2024-01-01", "2024-02-02", minutes=15, seed=5)
    cfg = replace(BotConfig.load(None, {}), history_bars=1500, strategy="london_breakout")
    r = replay(raw, cfg, start="2024-01-25", end="2024-02-01", state_dir=str(tmp_path))
    assert r.match and r.first_bar >= pd.Timestamp("2024-01-24 23:00", tz="UTC")
