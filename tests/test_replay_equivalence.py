"""The live code path (fresh Bot per cron run, state on disk, rolling data window,
late/skipped runs) must produce exactly the same trades as the backtester."""

from dataclasses import replace

import pytest

from goldbot.config import BotConfig
from goldbot.replay import replay
from goldlab.synthetic import random_walk_bars


@pytest.fixture(scope="module")
def raw():
    return random_walk_bars("2024-01-01", "2024-02-12", minutes=15, seed=5)


@pytest.mark.parametrize("strategy", ["london_breakout", "ny_momentum", "vwap_fade"])
def test_live_path_matches_backtest(tmp_path, raw, strategy):
    cfg = replace(BotConfig.load(None, {}), history_bars=1500, strategy=strategy)
    r = replay(raw, cfg, seed=3, state_dir=str(tmp_path))
    assert r.runs > 200
    assert r.match, r.mismatches[:5]
    if strategy != "ny_momentum":
        assert len(r.bot_trades) >= 3  # the comparison is not vacuous


def test_replay_detects_a_difference(tmp_path, raw):
    """Sanity check of the checker: different costs in the backtest must be caught."""
    from goldbot.replay import compare
    cfg = replace(BotConfig.load(None, {}), history_bars=1500, strategy="london_breakout")
    r = replay(raw, cfg, seed=3, state_dir=str(tmp_path))
    tampered = r.backtest_trades.copy()
    tampered.loc[0, "pnl_usd"] += 0.01
    assert compare(r.bot_trades, tampered)
