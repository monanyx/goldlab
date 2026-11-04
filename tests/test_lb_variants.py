"""Pre-registered London-breakout variants (docs/improvement-tests.md)."""

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from goldbot.bot import Bot
from goldbot.config import BotConfig, ConfigError
from goldbot.feeds import FrameFeed
from goldbot.notify import ConsoleNotifier
from goldbot.replay import replay
from goldlab.data import clean
from goldlab.features import daily_trend
from goldlab.strategies import LBConfirm, LBEarlyExit, LBTightRange, LBTrend, LondonBreakout
from goldlab.synthetic import random_walk_bars


@pytest.fixture(scope="module")
def df():
    return clean(random_walk_bars("2023-01-02", "2023-06-01", minutes=15, seed=21))[0]


def test_variants_change_exactly_one_default():
    base = LondonBreakout().params
    changed = {cls.name: {k for k, v in cls().params.items() if base[k] != v}
               for cls in (LBTrend, LBConfirm, LBEarlyExit, LBTightRange)}
    assert changed == {"lb_trend": {"trend_days"}, "lb_confirm": {"confirm_close"},
                       "lb_early_exit": {"flat_hour"}, "lb_tight_range": {"max_range_atr"}}


def test_daily_trend_uses_only_past_days(df):
    full = daily_trend(df, 20)
    cut = len(df) * 2 // 3
    part = daily_trend(df.iloc[:cut], 20)
    assert (full[:cut] == part).all()
    assert set(np.unique(full)) <= {-1.0, 0.0, 1.0}
    assert (full[:20 * 80] == 0).all()  # not enough history in the first weeks


def test_trend_variant_only_trades_with_the_trend(df):
    trend = daily_trend(df, 20)
    sig = LBTrend().signals(df)
    assert sig.orders
    for i, orders in sig.orders.items():
        assert len(orders) == 1 and orders[0].side == trend[i]


def test_confirm_variant_enters_at_market_after_a_close_beyond_the_range(df):
    base = LondonBreakout().signals(df)
    sig = LBConfirm().signals(df)
    assert sig.orders
    c = df["close"].to_numpy()
    for j, orders in sig.orders.items():
        (od,) = orders
        assert od.kind == "market"
        # the same day's baseline stop levels define the range edges (+ buffer)
        k = max(i for i in base.orders if i <= j)
        long_lvl = next(o.price for o in base.orders[k] if o.side == 1)
        short_lvl = next(o.price for o in base.orders[k] if o.side == -1)
        assert (c[j] > long_lvl) if od.side == 1 else (c[j] < short_lvl)


def test_early_exit_and_tight_range_are_subsets_of_the_baseline(df):
    base = LondonBreakout().signals(df)
    early = LBEarlyExit().signals(df)
    tight = LBTightRange().signals(df)
    assert set(early.orders) == set(base.orders)
    assert set(tight.orders) < set(base.orders)
    lon = df.index.tz_convert("Europe/London")
    after_13 = (lon.hour >= 13) & (lon.hour < 16)
    assert early.flat[after_13].all() and not base.flat[after_13 & (lon.dayofweek < 5)].all()


@pytest.mark.parametrize("strategy,bars", [("lb_trend", 2100), ("lb_confirm", 1500)])
def test_variant_live_path_matches_backtest(tmp_path, strategy, bars):
    raw = random_walk_bars("2024-01-01", "2024-03-01", minutes=15, seed=8)
    cfg = replace(BotConfig.load(None, {}), history_bars=bars, strategy=strategy)
    r = replay(raw, cfg, seed=2, state_dir=str(tmp_path))
    assert r.match, r.mismatches[:5]
    assert len(r.bot_trades) >= 2


def test_bot_refuses_too_little_history_for_the_strategy(tmp_path):
    raw = random_walk_bars("2024-01-01", "2024-02-01", minutes=15, seed=8)
    cfg = replace(BotConfig.load(None, {}), history_bars=1500, strategy="lb_trend",
                  state_dir=str(tmp_path))
    with pytest.raises(ConfigError, match="history_bars"):
        Bot(cfg, FrameFeed(raw), ConsoleNotifier(quiet=True))
