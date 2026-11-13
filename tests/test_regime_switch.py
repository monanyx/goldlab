"""Regime switch and the untouched-holdout adoption rule (docs/regime-switch-test.md)."""

import json
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from goldbot.config import BotConfig
from goldbot.replay import replay
from goldbot.strategies import pick_from_report
from goldlab import pipeline
from goldlab.data import clean
from goldlab.features import daily_atr, daily_trend, daily_trend_distance, prev_day_range
from goldlab.pipeline import RunConfig, StrategyReport, apply_adoption, split_holdout
from goldlab.strategies import ALL_STRATEGIES, LondonBreakout, RegimeSwitch
from goldlab.strategies.regime_switch import CHAOS, NO_DATA, RANGE, TREND
from goldlab.synthetic import random_walk_bars
from goldlab.validation import WalkForwardResult


@pytest.fixture(scope="module")
def df():
    return clean(random_walk_bars("2023-01-02", "2023-07-01", minutes=15, seed=21))[0]


# --------------------------------------------------------------------------- features


def test_trend_distance_and_prev_range_use_only_past_days(df):
    cut = len(df) * 2 // 3
    for f in (lambda d: daily_trend_distance(d, 20), prev_day_range):
        full, part = f(df), f(df.iloc[:cut])
        assert np.allclose(full[:cut], part, equal_nan=True)
    # the sign of the distance is the existing trend filter
    dist = daily_trend_distance(df, 20)
    ok = np.isfinite(dist)
    assert ok.any() and (np.sign(dist[ok]) == daily_trend(df, 20)[ok]).all()


def test_prev_day_range_is_yesterdays_high_minus_low(df):
    days = df.groupby("trade_date").agg(hi=("high", "max"), lo=("low", "min"))
    rng = prev_day_range(df)
    d = days.index[5]
    i = int(np.flatnonzero(df["trade_date"].to_numpy() == np.datetime64(d))[0])
    assert rng[i] == pytest.approx(days["hi"].iloc[4] - days["lo"].iloc[4])


# --------------------------------------------------------------------------- classification


def test_classify_order_of_checks():
    s = RegimeSwitch()
    a = 100.0
    assert s.classify(150, 250, 40, a) == CHAOS  # shock day beats a strong trend
    assert s.classify(150, 100, 90, a) == CHAOS  # Asia range too wide
    assert s.classify(150, 100, 10, a) == CHAOS  # Asia range too narrow
    assert s.classify(-100, 100, 40, a) == TREND  # exactly 1.0 ATR below the average
    assert s.classify(99, 100, 40, a) == RANGE
    assert s.classify(float("nan"), 100, 40, a) == NO_DATA
    assert s.classify(50, 100, 40, float("nan")) == NO_DATA


def test_all_three_day_types_occur_and_chaos_days_never_trade(df):
    s = RegimeSwitch(chaos_atr=1.3)  # synthetic data has few shock days at 2.0
    types = s.day_types(df)
    assert {TREND, RANGE, CHAOS} <= set(types)
    sig = s.signals(df)
    traded_days = {pd.Timestamp(df["trade_date"].iloc[i]) for i in sig.orders}
    assert traded_days <= set(types[types.isin([TREND, RANGE])].index)


def test_trend_days_trade_one_side_with_the_trend(df):
    s = RegimeSwitch()
    dist = daily_trend_distance(df, s.trend_days)
    sig = s.signals(df)
    trend_orders = {i: o for i, o in sig.orders.items() if o[0].tag.startswith("trend")}
    assert len(trend_orders) >= 5
    for i, (od, *rest) in trend_orders.items():
        assert not rest and od.kind == "stop"
        assert od.side == np.sign(dist[i])
        assert od.target_r == s.target_r and od.stop_dist > 0


def test_range_fades_follow_the_rules(df):
    s = RegimeSwitch()
    atr = daily_atr(df)
    sig = s.signals(df)
    c = df["close"].to_numpy()
    fades = {i: o[0] for i, o in sig.orders.items() if o[0].tag.startswith("range")}
    assert len(fades) >= 5
    for i, od in fades.items():
        buf = s.buffer_atr * atr[i]
        assert od.kind == "market" and od.stop_price is not None and od.target_price is not None
        if od.side == -1:  # false break above: stop above the poke, target the range low
            assert od.stop_price > c[i] > od.target_price
        else:
            assert od.stop_price < c[i] < od.target_price
        risk, reward = abs(c[i] - od.stop_price), abs(od.target_price - c[i])
        assert reward >= s.fade_min_rr * risk - 1e-9
        # the stop sits beyond the range edge by more than the buffer
        assert abs(od.stop_price - od.target_price) > 2 * buf
        lon = df.index[i].tz_convert("Europe/London")
        assert 8 * 60 <= lon.hour * 60 + lon.minute < 8 * 60 + s.entry_hours * 60


def test_at_most_one_entry_signal_per_day(df):
    sig = RegimeSwitch().signals(df)
    days = pd.Series([df["trade_date"].iloc[i] for i in sig.orders])
    assert not days.duplicated().any()


def test_grid_has_81_combinations_and_registered():
    assert len(RegimeSwitch().param_grid()) == 81
    assert RegimeSwitch in ALL_STRATEGIES
    assert RegimeSwitch.holdout_required and not getattr(LondonBreakout, "holdout_required", False)


def test_regime_switch_live_path_matches_backtest(tmp_path):
    raw = random_walk_bars("2024-01-01", "2024-03-15", minutes=15, seed=8)
    cfg = replace(BotConfig.load(None, {}), history_bars=2100, strategy="regime_switch")
    r = replay(raw, cfg, seed=2, state_dir=str(tmp_path))
    assert r.match, r.mismatches[:5]
    assert len(r.bot_trades) >= 3
    # the compared period exercises both setups, not just one of them
    bars = clean(raw)[0]
    sig = RegimeSwitch().signals(bars)
    replayed = bars.index >= r.bot_trades["entry_time"].min() - pd.Timedelta(hours=3)
    tags = {o.tag.split("_")[0] for i, ods in sig.orders.items() if replayed[i] for o in ods}
    assert tags == {"trend", "range"}


# --------------------------------------------------------------------------- holdout + adoption


def test_split_holdout(df):
    cfg = RunConfig(holdout_end="2023-03-01")
    dev, hold = split_holdout(df, replace(cfg, holdout_end=None))
    assert hold is None and len(dev) == len(df)
    dev, hold = split_holdout(df, cfg)
    assert hold is None  # development window under MIN_DEV_DAYS -> no split
    big = clean(random_walk_bars("2022-01-03", "2023-12-29", minutes=15, seed=4))[0]
    dev, hold = split_holdout(big, replace(cfg, holdout_end="2022-10-03"))
    assert hold.index[-1] < pd.Timestamp("2022-10-03", tz="UTC") <= dev.index[0]
    assert len(dev) + len(hold) == len(big)
    # data that ends before the cut-off: no holdout, everything is the development window
    dev, hold = split_holdout(df, replace(cfg, holdout_end="2025-01-01"))
    assert hold is None and len(dev) == len(df)


def _report(name, verdict, wf_r, hold_r=None, hold_t=None, hold_n=0, hold_r2=None, cls=RegimeSwitch):
    hold = None
    if hold_r is not None:
        hold = {"Holdout (default)": {"trades": hold_n, "expectancy_r": hold_r, "r_t_stat": hold_t},
                "Holdout 2x costs": {"trades": hold_n, "expectancy_r": hold_r2, "r_t_stat": hold_t}}
    return StrategyReport(name, cls(), {}, cls(), WalkForwardResult(), {"expectancy_r": wf_r},
                          pd.DataFrame(), pd.DataFrame(), 0.7, pd.Series(dtype=float), verdict, [],
                          holdout=hold)


EDGE = "EDGE AFTER COSTS (tentative)"


@pytest.mark.parametrize("months,hold,adopted", [
    (24, dict(hold_r=0.10, hold_t=1.8, hold_n=120, hold_r2=0.05), True),
    (24, dict(hold_r=0.10, hold_t=1.2, hold_n=120, hold_r2=0.05), False),  # t too low
    (24, dict(hold_r=0.10, hold_t=1.8, hold_n=25, hold_r2=0.05), False),  # too few trades
    (24, dict(hold_r=0.10, hold_t=1.8, hold_n=120, hold_r2=-0.01), False),  # dies at 2x costs
    (24, dict(hold_r=-0.02, hold_t=-0.3, hold_n=120, hold_r2=-0.05), False),
    (6, dict(hold_r=0.10, hold_t=1.8, hold_n=120, hold_r2=0.05), False),  # holdout too short
    (0, {}, False),  # no holdout at all
])
def test_adoption_rule(months, hold, adopted):
    base = _report("london_breakout", "INCONCLUSIVE", 0.03, cls=LondonBreakout)
    rs = _report("regime_switch", EDGE, 0.08, **hold)
    apply_adoption([base, rs], months)
    assert rs.adoption["adopted"] is adopted
    assert rs.verdict.startswith("EDGE") is adopted  # the bot's "auto" only follows EDGE
    assert base.adoption is None and base.verdict == "INCONCLUSIVE"  # round-1 verdicts untouched
    if months < pipeline.HOLDOUT_MIN_MONTHS:
        assert "holdout data missing" in rs.verdict


def test_adoption_needs_dev_edge_and_beating_the_baseline():
    good = dict(hold_r=0.10, hold_t=1.8, hold_n=120, hold_r2=0.05)
    base = _report("london_breakout", "INCONCLUSIVE", 0.09, cls=LondonBreakout)
    rs = _report("regime_switch", EDGE, 0.08, **good)  # below the baseline's walk-forward
    apply_adoption([base, rs], 24)
    assert not rs.adoption["adopted"]
    rs = _report("regime_switch", "INCONCLUSIVE (positive out-of-sample, but not convincing)", 0.2, **good)
    apply_adoption([base, rs], 24)
    assert not rs.adoption["adopted"] and rs.verdict.startswith("INCONCLUSIVE")
    rs = _report("regime_switch", EDGE, 0.2, **good)
    apply_adoption([rs], 24)  # baseline missing from the run: cannot show it beats it
    assert not rs.adoption["adopted"]


def test_bot_auto_follows_only_a_confirmed_regime_switch(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"regime_switch": {
        "verdict": "INCONCLUSIVE (edge on the development window, failed the adoption rule)",
        "stats": {"Walk-forward OOS": {"expectancy_r": 0.3}}}}))
    assert pick_from_report(p)[0] == "london_breakout"
    p.write_text(json.dumps({"regime_switch": {
        "verdict": "EDGE AFTER COSTS (confirmed on the untouched holdout)",
        "stats": {"Walk-forward OOS": {"expectancy_r": 0.3}}}}))
    assert pick_from_report(p)[0] == "regime_switch"
