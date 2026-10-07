"""In-sample / out-of-sample split, walk-forward analysis and parameter sensitivity.

Speed trick: signals are computed once per parameter set on the full history
(all features are causal), and the engine is run on index windows of them.
"""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd

from .config import CostConfig, RiskConfig
from .engine import Backtester, BacktestResult, StrategySignals
from .metrics import summarize
from .strategies.base import Strategy

MIN_TRADES_FOR_SELECTION = 20


def slice_signals(sig: StrategySignals, start: int, end: int) -> StrategySignals:
    orders = {}
    for i, ods in sig.orders.items():
        if start <= i < end:
            moved = []
            for od in ods:
                exp = None if od.expires is None else od.expires - start
                moved.append(replace(od, expires=exp))
            orders[i - start] = moved
    return StrategySignals(orders=orders, flat=sig.flat[start:end])


def run_window(df: pd.DataFrame, sig: StrategySignals, start: int, end: int,
               costs: CostConfig, risk: RiskConfig) -> BacktestResult:
    return Backtester(costs, risk).run(df.iloc[start:end], slice_signals(sig, start, end))


def index_at(df: pd.DataFrame, ts: pd.Timestamp) -> int:
    return int(df.index.searchsorted(ts))


def split_point(df: pd.DataFrame, is_fraction: float) -> int:
    """Split at a trade-date boundary so no day straddles IS/OOS."""
    dates = df["trade_date"].drop_duplicates()
    cut_date = dates.iloc[int(len(dates) * is_fraction)]
    return int(np.flatnonzero(df["trade_date"].to_numpy() == np.datetime64(cut_date))[0])


# --------------------------------------------------------------------------- grid runs

_GRID_DF: pd.DataFrame | None = None


def _grid_worker(args):
    strat, costs, risk = args
    sig = strat.signals(_GRID_DF)
    res = Backtester(costs, risk).run(_GRID_DF, sig)
    return strat, res.trades


def _init_worker(df):
    global _GRID_DF
    _GRID_DF = df


def run_grid(df: pd.DataFrame, strategies: list[Strategy], costs: CostConfig,
             risk: RiskConfig, workers: int = 1) -> list[tuple[Strategy, pd.DataFrame]]:
    """Full-history trades for every parameter set (used for WF selection and sensitivity)."""
    jobs = [(s, costs, risk) for s in strategies]
    if workers <= 1:
        _init_worker(df)
        return [_grid_worker(j) for j in jobs]
    with ProcessPoolExecutor(workers, initializer=_init_worker, initargs=(df,)) as ex:
        return list(ex.map(_grid_worker, jobs, chunksize=max(1, len(jobs) // (workers * 4))))


def trades_in(trades: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    if trades.empty:
        return trades
    m = (trades["entry_time"] >= start) & (trades["entry_time"] < end)
    return trades[m]


def r_stats(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {"trades": 0, "expectancy_r": float("nan"), "profit_factor": float("nan")}
    r = trades["r_multiple"].to_numpy(float)
    gl = -r[r < 0].sum()
    return {"trades": len(r), "expectancy_r": float(r.mean()),
            "profit_factor": float(r[r > 0].sum() / gl) if gl > 0 else float("inf")}


def select_best(grid: list[tuple[Strategy, pd.DataFrame]], start, end) -> Strategy:
    """Pick the parameter set with the best expectancy-in-R t-score on [start, end).

    Using mean/stderr * sqrt-ish (i.e. a t-stat) rather than raw expectancy keeps
    the optimiser from picking tiny-sample flukes.
    """
    best, best_score = None, -np.inf
    for strat, trades in grid:
        t = trades_in(trades, start, end)
        if len(t) < MIN_TRADES_FOR_SELECTION:
            continue
        r = t["r_multiple"].to_numpy(float)
        sd = r.std(ddof=1)
        score = r.mean() / (sd / np.sqrt(len(r))) if sd > 0 else -np.inf
        if score > best_score:
            best, best_score = strat, score
    return best if best is not None else grid[0][0]


# --------------------------------------------------------------------------- walk-forward


@dataclass
class WalkForwardResult:
    folds: list[dict] = field(default_factory=list)
    trades: pd.DataFrame = field(default_factory=pd.DataFrame)
    equity: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))


def walk_forward(df: pd.DataFrame, grid: list[tuple[Strategy, pd.DataFrame]], costs: CostConfig,
                 risk: RiskConfig, train_months: int = 12, test_months: int = 3) -> WalkForwardResult:
    """Rolling re-optimisation: pick params on `train_months`, trade the next `test_months`.

    The stitched test segments form a genuinely out-of-sample equity curve.
    """
    out = WalkForwardResult()
    lookup = dict(grid)
    first, last = df.index[0], df.index[-1]
    t0 = first
    eq_parts, trade_parts = [], []
    equity = risk.initial_equity
    while True:
        tr_end = t0 + pd.DateOffset(months=train_months)
        te_end = tr_end + pd.DateOffset(months=test_months)
        if tr_end >= last:
            break
        te_end = min(te_end, last + pd.Timedelta(minutes=1))
        best = select_best(grid, t0, tr_end)
        s, e = index_at(df, tr_end), index_at(df, te_end)
        if e - s < 10:
            break
        res = run_window(df, best.signals(df), s, e, costs, replace(risk, initial_equity=equity))
        st = summarize(res)
        train_stats = r_stats(trades_in(lookup[best], t0, tr_end))
        out.folds.append({
            "train": f"{t0:%Y-%m-%d} -> {tr_end:%Y-%m-%d}",
            "test": f"{tr_end:%Y-%m-%d} -> {df.index[e - 1]:%Y-%m-%d}",
            "params": {k: best.params[k] for k in best.grid},
            "train_expectancy_r": train_stats["expectancy_r"],
            "test_trades": st["trades"],
            "test_expectancy_r": st["expectancy_r"],
            "test_return": st["total_return"],
        })
        eq_parts.append(res.equity)
        if not res.trades.empty:
            trade_parts.append(res.trades)
        equity = float(res.equity.iloc[-1])
        if te_end >= last:
            break
        t0 = t0 + pd.DateOffset(months=test_months)
    if eq_parts:
        out.equity = pd.concat(eq_parts)
    if trade_parts:
        out.trades = pd.concat(trade_parts, ignore_index=True)
    return out


# --------------------------------------------------------------------------- sensitivity


def sensitivity_table(grid: list[tuple[Strategy, pd.DataFrame]], start=None, end=None) -> pd.DataFrame:
    rows = []
    for strat, trades in grid:
        t = trades if start is None else trades_in(trades, start, end)
        rows.append({**{k: strat.params[k] for k in strat.grid}, **r_stats(t)})
    return pd.DataFrame(rows)


def one_at_a_time(table: pd.DataFrame, base: Strategy) -> pd.DataFrame:
    """Vary each grid parameter alone around the default, others held at default."""
    rows = []
    keys = list(base.grid)
    for k in keys:
        others = {o: base.params[o] for o in keys if o != k}
        if any(base.params[o] not in base.grid[o] for o in others):
            continue
        m = np.ones(len(table), dtype=bool)
        for o, v in others.items():
            m &= np.isclose(table[o].astype(float), float(v))
        for _, r in table[m].sort_values(k).iterrows():
            rows.append({"param": k, "value": r[k], "is_default": np.isclose(r[k], base.params[k]),
                         "trades": int(r["trades"]), "expectancy_r": r["expectancy_r"],
                         "profit_factor": r["profit_factor"]})
    return pd.DataFrame(rows)
