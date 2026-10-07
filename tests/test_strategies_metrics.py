import numpy as np
import pandas as pd
import pytest

from goldlab.config import CostConfig, RiskConfig
from goldlab.data import clean
from goldlab.engine import Backtester, BacktestResult
from goldlab.sessions import trading_day
from goldlab.metrics import longest_losing_streak, max_drawdown, monthly_returns, summarize
from goldlab.strategies import ALL_STRATEGIES
from goldlab.synthetic import random_walk_bars


@pytest.fixture(scope="module")
def bars():
    df, _ = clean(random_walk_bars("2023-01-02", "2023-04-01", minutes=5, seed=3))
    return df


@pytest.mark.parametrize("cls", ALL_STRATEGIES)
def test_strategies_are_causal(cls, bars):
    """Signals up to bar k must not change when future bars are removed."""
    cut = int(len(bars) * 0.6)
    full = cls().signals(bars)
    part = cls().signals(bars.iloc[:cut])
    # Ignore the last day (truncation can drop the day-level gating of an unfinished day).
    last_day_start = int(np.flatnonzero(bars["trade_date"].to_numpy() == bars["trade_date"].iloc[cut - 1])[0])
    keys = lambda d: {i: [(o.side, o.kind, round(o.price or 0, 6), o.stop_dist, o.target_r,
                           round(o.target_price or 0, 6)) for o in v]
                      for i, v in d.items() if i < last_day_start}
    assert keys(full.orders) == keys(part.orders)
    assert len(keys(full.orders)) >= 3
    assert (full.flat[:cut] == part.flat).all()


@pytest.mark.parametrize("cls", ALL_STRATEGIES)
def test_every_trade_has_stop_and_obeys_risk_rules(cls, bars):
    risk = RiskConfig()
    res = Backtester(CostConfig(), risk).run(bars, cls().signals(bars))
    t = res.trades
    assert len(t) > 0
    assert t["stop"].notna().all()
    assert (t.groupby("trade_date").size() <= risk.max_trades_per_day).all()
    # flat before session end: no trade ends by rollover swap or is held overnight
    assert (t["swap_usd"] == 0).all()
    exit_day = trading_day(pd.DatetimeIndex(t["exit_time"]))
    assert (exit_day == pd.DatetimeIndex(t["trade_date"])).all()
    # risk per trade never above 1% of equity (+ rounding)
    assert (t["risk_usd"] <= 0.0101 * t["equity_after"].shift(1).fillna(risk.initial_equity) * 1.05).all()


@pytest.mark.parametrize("cls", ALL_STRATEGIES)
def test_random_walk_has_no_edge_after_costs(cls, bars):
    """On a driftless random walk, costs must make every strategy lose on average."""
    res = Backtester(CostConfig(spread=2.0), RiskConfig()).run(bars, cls().signals(bars))
    assert res.trades["r_multiple"].mean() < 0


def test_longest_losing_streak():
    assert longest_losing_streak(np.array([1, -1, -1, 2, -1, -1, -1, 0.5])) == 3
    assert longest_losing_streak(np.array([])) == 0


def test_max_drawdown_and_summary():
    eq = pd.Series([100, 120, 90, 130, 117],
                   index=pd.date_range("2024-01-01", periods=5, freq="D", tz="UTC"), dtype=float)
    assert max_drawdown(eq) == pytest.approx(0.25)
    trades = pd.DataFrame({"r_multiple": [2.0, -1.0, -1.0, 1.0], "pnl_usd": [20.0, -10, -10, 10]})
    s = summarize(BacktestResult(trades, eq, 100.0))
    assert s["win_rate"] == 0.5
    assert s["profit_factor"] == pytest.approx(1.5)
    assert s["expectancy_r"] == pytest.approx(0.25)
    assert s["longest_losing_streak"] == 2
    assert s["total_return"] == pytest.approx(0.17)


def test_monthly_returns_chain_to_total():
    idx = pd.date_range("2024-01-01", "2024-03-31", freq="D", tz="UTC")
    eq = pd.Series(np.linspace(100, 130, len(idx)), index=idx)
    m = monthly_returns(eq)
    assert len(m) == 3
    assert np.prod(1 + m.values) - 1 == pytest.approx(0.30)
