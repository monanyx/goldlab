import numpy as np
import pandas as pd
import pytest

from goldlab.config import RiskConfig
from goldlab.engine import Backtester, Order, StrategySignals

from .conftest import make_bars, no_flat

FLAT_BAR = (2000, 2000.5, 1999.5, 2000)


def run(df, orders, costs, risk, flat=None):
    sig = StrategySignals(orders=orders, flat=no_flat(df) if flat is None else flat)
    return Backtester(costs, risk).run(df, sig)


def test_market_order_fills_next_open_with_costs_and_hits_target(costs, risk):
    df = make_bars([FLAT_BAR, (2001, 2001.5, 2000.5, 2001), (2001, 2012, 2000.8, 2011), FLAT_BAR])
    res = run(df, {0: [Order(side=1, stop_dist=5.0, target_r=2.0)]}, costs, risk)
    t = res.trades.iloc[0]
    # created at bar 0 close -> fills at bar 1 open (2001) + half spread + slippage
    assert t.entry_time == df.index[1]
    assert t.entry_price == pytest.approx(2001 + 0.2 + 0.1)
    assert t.stop == pytest.approx(1996.0)
    assert t.target == pytest.approx(2011.0)
    assert t.exit_reason == "target"
    assert t.exit_time == df.index[2]
    # limit exit: half spread, no slippage
    assert t.exit_price == pytest.approx(2011 - 0.2)


def test_stop_out_loses_exactly_one_r_and_risk_pct(costs, risk):
    df = make_bars([FLAT_BAR, (2000, 2000.5, 1999.5, 2000), (2000, 2000.2, 1994, 1995), FLAT_BAR])
    res = run(df, {0: [Order(side=1, stop_dist=5.0, target_r=2.0)]}, costs, risk)
    t = res.trades.iloc[0]
    assert t.exit_reason == "stop"
    assert t.exit_price == pytest.approx(1995.0 - 0.2 - 0.1)
    assert t.r_multiple == pytest.approx(-1.0, abs=1e-3)
    assert t.pnl_usd == pytest.approx(-1000.0, rel=1e-3)  # 1% of 100k


def test_stop_and_target_in_same_bar_assumes_stop(costs, risk):
    df = make_bars([FLAT_BAR, (2000, 2000.5, 1999.5, 2000), (2000, 2020, 1990, 2010), FLAT_BAR])
    res = run(df, {0: [Order(side=1, stop_dist=5.0, target_r=2.0)]}, costs, risk)
    assert res.trades.iloc[0].exit_reason == "stop"


def test_gap_through_stop_fills_at_open(costs, risk):
    df = make_bars([FLAT_BAR, (2000, 2000.5, 1999.5, 2000), (1990, 1991, 1985, 1988), FLAT_BAR])
    res = run(df, {0: [Order(side=1, stop_dist=5.0, target_r=2.0)]}, costs, risk)
    t = res.trades.iloc[0]
    assert t.exit_reason == "stop_gap"
    assert t.exit_price == pytest.approx(1990 - 0.3)
    assert t.r_multiple < -1.0  # gaps can cost more than 1R


def test_short_side_mirrors_costs(costs, risk):
    df = make_bars([FLAT_BAR, (2000, 2000.5, 1999.5, 2000), (2000, 2000.4, 1989, 1990), FLAT_BAR])
    res = run(df, {0: [Order(side=-1, stop_dist=5.0, target_r=2.0)]}, costs, risk)
    t = res.trades.iloc[0]
    assert t.entry_price == pytest.approx(2000 - 0.3)
    assert t.exit_reason == "target"
    assert t.exit_price == pytest.approx(1990 + 0.2)
    assert t.pnl_usd > 0


def test_no_lookahead_order_cannot_fill_on_creation_bar(costs, risk):
    # Bar 0 itself spikes through the trigger; the order is created at bar 0's close.
    df = make_bars([(2000, 2010, 1999, 2000), (2000, 2000.5, 1999.5, 2000), FLAT_BAR])
    od = Order(side=1, kind="stop", price=2005, stop_dist=3, target_r=1)
    res = run(df, {0: [od]}, costs, risk)
    assert res.trades.empty


def test_stop_entry_gap_fills_at_open_and_oco_cancels_other_leg(costs, risk):
    df = make_bars([FLAT_BAR, (2006, 2007, 2005.5, 2006.5), (2006.5, 2007, 2006, 2006.5),
                    (2006.5, 2006.6, 1990, 1991), FLAT_BAR])
    orders = {0: [Order(side=1, kind="stop", price=2005, stop_dist=10, target_r=5, group="g"),
                  Order(side=-1, kind="stop", price=1995, stop_dist=10, target_r=5, group="g")]}
    res = run(df, orders, costs, risk)
    assert len(res.trades) == 1  # short leg cancelled even though 1995 traded later
    t = res.trades.iloc[0]
    assert t.side == 1
    assert t.entry_price == pytest.approx(2006 + 0.3)  # gap above trigger -> open


def test_stop_entry_bar_target_needs_close_beyond(costs, risk):
    # Triggers at 2001, target 2002 inside the same bar but close back below -> no target fill.
    df = make_bars([FLAT_BAR, (2000.5, 2002.5, 2000.4, 2001.2), (2001.2, 2001.5, 2000.6, 2001.0), FLAT_BAR])
    od = Order(side=1, kind="stop", price=2001, stop_dist=1, target_r=1)
    res = run(df, {0: [od]}, costs, risk)
    t = res.trades.iloc[0]
    assert t.entry_time == df.index[1]
    assert t.exit_time != df.index[1]


def test_expired_order_does_not_fill(costs, risk):
    df = make_bars([FLAT_BAR, FLAT_BAR, (2000, 2010, 2000, 2009), FLAT_BAR])
    od = Order(side=1, kind="stop", price=2005, stop_dist=3, target_r=1, expires=df.index[2])
    assert run(df, {0: [od]}, costs, risk).trades.empty


def test_flat_bar_forces_exit_at_open_and_blocks_entries(costs, risk):
    df = make_bars([FLAT_BAR, FLAT_BAR, (2003, 2004, 2002, 2003), FLAT_BAR, FLAT_BAR])
    flat = np.array([False, False, True, True, True])
    res = run(df, {0: [Order(side=1, stop_dist=5, target_r=3)],
                   2: [Order(side=1, stop_dist=5, target_r=3)]}, costs, risk, flat)
    assert len(res.trades) == 1
    t = res.trades.iloc[0]
    assert t.exit_reason == "session_end"
    assert t.exit_time == df.index[2]
    assert t.exit_price == pytest.approx(2003 - 0.3)


def _losing_day(n_orders, n_bars_per_trade=3):
    rows, orders = [], {}
    for k in range(n_orders):
        base = len(rows)
        rows += [FLAT_BAR, (2000, 2000.5, 1999.5, 2000), (2000, 2000.2, 1990, 1991)]
        orders[base] = [Order(side=1, stop_dist=5.0, target_r=2.0)]
    rows.append(FLAT_BAR)
    return rows, orders


def test_daily_loss_limit_stops_trading(costs):
    risk = RiskConfig(risk_pct=0.01, daily_loss_limit_pct=0.03, max_trades_per_day=10,
                      min_units=0.001, unit_step=0.001, max_leverage=1000)
    rows, orders = _losing_day(6)
    df = make_bars(rows)
    res = Backtester(costs, risk).run(df, StrategySignals(orders, no_flat(df)))
    # Risk is 1% of the day's starting equity, so three stop-outs = -3% and trading stops.
    assert len(res.trades) == 3
    assert res.trades.pnl_usd.sum() == pytest.approx(-3000, rel=1e-3)
    assert res.blocked["daily_loss_limit"] == 3


def test_risk_capped_to_remaining_daily_allowance(costs):
    risk = RiskConfig(risk_pct=0.02, daily_loss_limit_pct=0.03, max_trades_per_day=10,
                      min_units=0.001, unit_step=0.001, max_leverage=1000)
    rows, orders = _losing_day(3)
    df = make_bars(rows)
    res = Backtester(costs, risk).run(df, StrategySignals(orders, no_flat(df)))
    # 2% + capped 1% = 3%, then blocked.
    assert len(res.trades) == 2
    assert res.trades.risk_usd.tolist() == pytest.approx([2000, 1000], rel=1e-3)
    assert res.blocked["daily_loss_limit"] == 1


def test_max_trades_per_day(costs):
    risk = RiskConfig(risk_pct=0.001, daily_loss_limit_pct=0.5, max_trades_per_day=3,
                      min_units=0.001, unit_step=0.001, max_leverage=1000)
    rows, orders = _losing_day(5)
    df = make_bars(rows)
    res = Backtester(costs, risk).run(df, StrategySignals(orders, no_flat(df)))
    assert len(res.trades) == 3
    assert res.blocked["max_trades"] == 2


def test_daily_counters_reset_on_new_trade_date(costs):
    risk = RiskConfig(risk_pct=0.001, max_trades_per_day=1, min_units=0.001, unit_step=0.001,
                      max_leverage=1000)
    # Straddle the 17:00 New York rollover (22:00 UTC in January).
    df = make_bars([FLAT_BAR, (2000, 2000.5, 1999.5, 2000), (2000, 2000.2, 1990, 1991), FLAT_BAR,
                    FLAT_BAR, (2000, 2000.5, 1999.5, 2000), (2000, 2000.2, 1990, 1991), FLAT_BAR],
                   start="2024-01-09 21:40")
    assert df["trade_date"].nunique() == 2
    orders = {0: [Order(side=1, stop_dist=5, target_r=2)], 4: [Order(side=1, stop_dist=5, target_r=2)]}
    res = Backtester(costs, risk).run(df, StrategySignals(orders, no_flat(df)))
    assert len(res.trades) == 2


def test_swap_charged_across_rollover_triple_on_wednesday(costs, risk):
    # Wednesday 2024-01-10 trade date ends 22:00 UTC; holding through it costs 3 nights.
    df = make_bars([FLAT_BAR] * 8, start="2024-01-10 21:40")
    res = run(df, {0: [Order(side=1, stop_dist=50, target_r=5)]}, costs, risk,
              flat=np.array([False] * 7 + [True]))
    t = res.trades.iloc[0]
    assert pd.Timestamp(t.trade_date).dayofweek == 2
    assert t.swap_usd == pytest.approx(-1.0 * t.units * 3)


def test_position_closed_before_weekend_gap(costs, risk):
    fri = pd.date_range("2024-01-12 21:40", periods=4, freq="5min", tz="UTC")  # Fri, closes 22:00 UTC
    sun = pd.date_range("2024-01-14 23:00", periods=2, freq="5min", tz="UTC")
    from goldlab.sessions import label_sessions
    df = pd.DataFrame([FLAT_BAR] * 6, columns=["open", "high", "low", "close"],
                      index=fri.append(sun), dtype=float)
    df["volume"] = 1.0
    df = label_sessions(df)
    res = run(df, {0: [Order(side=1, stop_dist=50, target_r=5)]}, costs, risk)
    t = res.trades.iloc[0]
    assert t.exit_reason == "pre_close"
    assert t.exit_time == fri[-1]
    assert t.swap_usd == 0


def test_position_size_respects_leverage_cap(costs):
    risk = RiskConfig(risk_pct=0.01, max_leverage=2.0, min_units=1, unit_step=1)
    df = make_bars([FLAT_BAR, FLAT_BAR, FLAT_BAR])
    res = Backtester(costs, risk).run(df, StrategySignals({0: [Order(side=1, stop_dist=0.5, target_r=1)]},
                                                          no_flat(df)))
    t = res.trades.iloc[0]
    assert t.units * t.entry_price <= 2.0 * 100_000 + 1e-6


def test_one_position_at_a_time(costs, risk):
    df = make_bars([FLAT_BAR] * 6)
    orders = {i: [Order(side=1, stop_dist=50, target_r=5)] for i in range(5)}
    flat = np.array([False] * 5 + [True])
    res = run(df, orders, costs, risk, flat)
    assert len(res.trades) == 1


def test_equity_curve_matches_trade_pnl(costs, risk):
    df = make_bars([FLAT_BAR, (2000, 2000.5, 1999.5, 2000), (2000, 2000.2, 1994, 1995), FLAT_BAR])
    res = run(df, {0: [Order(side=1, stop_dist=5.0, target_r=2.0)]}, costs, risk)
    assert res.equity.iloc[-1] == pytest.approx(100_000 + res.trades.pnl_usd.sum())


def test_order_requires_hard_stop():
    with pytest.raises(ValueError):
        Order(side=1)
    with pytest.raises(ValueError):
        Order(side=1, kind="stop", stop_dist=1)  # stop order without trigger
