import numpy as np
import pandas as pd
import pytest

from goldlab.data import clean, is_market_closed, load_raw
from goldlab.sessions import label_sessions


def test_london_open_follows_dst():
    idx = pd.DatetimeIndex(["2024-01-15 08:00", "2024-07-15 07:00", "2024-07-15 06:55"], tz="UTC")
    df = label_sessions(pd.DataFrame({"close": [1, 1, 1]}, index=idx))
    assert df["is_london"].tolist() == [True, True, False]


def test_ny_session_and_overlap_label():
    idx = pd.DatetimeIndex(["2024-01-15 13:00", "2024-07-15 12:00", "2024-01-15 03:00"], tz="UTC")
    df = label_sessions(pd.DataFrame({"close": [1, 1, 1]}, index=idx))
    assert df["session"].tolist() == ["London+NewYork", "London+NewYork", "Asia"]


def test_trade_date_rolls_at_17_new_york():
    # 21:55 UTC Jan = 16:55 NY -> same day; 22:00 UTC = 17:00 NY -> next trade date
    idx = pd.DatetimeIndex(["2024-01-15 21:55", "2024-01-15 22:00"], tz="UTC")
    df = label_sessions(pd.DataFrame({"close": [1, 1]}, index=idx))
    assert [d.day for d in df["trade_date"]] == [15, 16]


def test_weekend_closure():
    idx = pd.DatetimeIndex(["2024-01-12 21:55", "2024-01-12 22:00", "2024-01-13 12:00",
                            "2024-01-14 21:55", "2024-01-14 23:00"], tz="UTC")
    assert is_market_closed(idx).tolist() == [False, True, True, True, False]


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_canonical_csv_and_cleaning(tmp_path):
    p = _write(tmp_path, "a.csv", "\n".join([
        "timestamp,open,high,low,close,volume",
        "2024-01-09T10:00:00Z,2030,2031,2029,2030.5,10",
        "2024-01-09T10:05:00Z,2030.5,2030,2031,2030.8,10",  # high/low swapped -> fixed
        "2024-01-09T10:05:00Z,2030.5,2031.2,2030.1,2030.9,12",  # duplicate, keep last
        "2024-01-09T10:10:00Z,,2031,2029,2030,10",  # invalid
        "2024-01-09T10:20:00Z,2030.9,2031,2030,2030.2,10",  # gap after 10:05
        "2024-01-13T10:00:00Z,2030,2031,2029,2030,10",  # Saturday
    ]))
    df, q = clean(load_raw(p))
    assert len(df) == 3
    assert q.duplicates_removed == 1 and q.invalid_rows_removed == 1 and q.weekend_rows_removed == 1
    assert df.index.tz is not None and str(df.index.tz) == "UTC"
    assert df["close"].iloc[1] == 2030.9
    assert df["gap_before"].tolist() == [False, False, True]
    assert (df["high"] >= df[["open", "close"]].max(axis=1)).all()


def test_naive_timestamps_converted_from_source_tz(tmp_path):
    p = _write(tmp_path, "b.csv", "timestamp,open,high,low,close\n2024-07-09 10:00:00,1,2,0.5,1.5\n")
    df = load_raw(p, source_tz="Europe/London")
    assert df.index[0] == pd.Timestamp("2024-07-09 09:00", tz="UTC")


def test_dukascopy_export_format(tmp_path):
    p = _write(tmp_path, "d.csv", "Gmt time,Open,High,Low,Close,Volume\n"
               "09.01.2024 10:00:00.000,2030.1,2031,2029.5,2030.4,0.12\n")
    df = load_raw(p)
    assert df.index[0] == pd.Timestamp("2024-01-09 10:00", tz="UTC")
    assert df["close"].iloc[0] == 2030.4


def test_mt5_export_requires_source_tz(tmp_path):
    p = _write(tmp_path, "m.csv", "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>\n"
               "2024.01.09\t12:00:00\t2030\t2031\t2029\t2030.5\t100\t0\t20\n")
    with pytest.raises(ValueError):
        load_raw(p)
    df = load_raw(p, source_tz="Etc/GMT-2")
    assert df.index[0] == pd.Timestamp("2024-01-09 10:00", tz="UTC")


def test_spike_removed_but_real_move_kept():
    idx = pd.date_range("2024-01-09 10:00", periods=6, freq="5min", tz="UTC")
    close = [2000, 2001, 2400, 2001, 2002, 2150]  # 2400 = bad tick; 2150 = one-sided move, kept
    df = pd.DataFrame({"open": close, "high": close, "low": close, "close": close,
                       "volume": 1.0}, index=idx, dtype=float)
    out, q = clean(df)
    assert q.spikes_removed == 1
    assert 2400 not in out["close"].values and 2150 in out["close"].values


def test_resample_to_m15():
    from goldlab.synthetic import random_walk_bars
    raw = random_walk_bars("2024-01-08", "2024-01-10", minutes=5)
    df, q = clean(raw, target_minutes=15)
    assert q.bar_minutes == 15
    assert (df.index.minute % 15 == 0).all()
