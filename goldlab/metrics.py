"""Performance metrics."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .engine import BacktestResult


def longest_losing_streak(r: np.ndarray) -> int:
    best = cur = 0
    for x in r:
        cur = cur + 1 if x < 0 else 0
        best = max(best, cur)
    return best


def max_drawdown(equity: pd.Series) -> float:
    """Max peak-to-trough decline as a positive fraction."""
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    return float(((peak - equity) / peak).max())


def drawdown_series(equity: pd.Series) -> pd.Series:
    peak = equity.cummax()
    return (equity - peak) / peak


def monthly_returns(equity: pd.Series) -> pd.Series:
    if equity.empty:
        return pd.Series(dtype=float)
    idx = equity.index.tz_convert("UTC").tz_localize(None)
    eq = pd.Series(equity.to_numpy(), index=idx)
    month_end = eq.resample("ME").last().dropna()
    prev = month_end.shift(1)
    prev.iloc[0] = eq.iloc[0]
    out = month_end / prev - 1
    out.index = out.index.to_period("M")
    return out


def summarize(res: BacktestResult) -> dict:
    t = res.trades
    eq = res.equity
    n = len(t)
    base = {
        "trades": n,
        "win_rate": float("nan"),
        "profit_factor": float("nan"),
        "expectancy_r": float("nan"),
        "r_t_stat": float("nan"),
        "total_return": float(eq.iloc[-1] / res.initial_equity - 1) if len(eq) else 0.0,
        "max_drawdown": max_drawdown(eq),
        "longest_losing_streak": 0,
        "avg_win_r": float("nan"),
        "avg_loss_r": float("nan"),
        "trades_per_month": 0.0,
    }
    if n == 0:
        return base
    r = t["r_multiple"].to_numpy(float)
    pnl = t["pnl_usd"].to_numpy(float)
    wins, losses = pnl[pnl > 0], pnl[pnl < 0]
    gross_loss = -losses.sum()
    months = max((eq.index[-1] - eq.index[0]).days / 30.44, 1e-9)
    base.update(
        win_rate=float((pnl > 0).mean()),
        profit_factor=float(wins.sum() / gross_loss) if gross_loss > 0 else float("inf"),
        expectancy_r=float(r.mean()),
        r_t_stat=float(r.mean() / (r.std(ddof=1) / math.sqrt(n))) if n > 1 and r.std(ddof=1) > 0 else float("nan"),
        longest_losing_streak=longest_losing_streak(r),
        avg_win_r=float(r[r > 0].mean()) if (r > 0).any() else float("nan"),
        avg_loss_r=float(r[r < 0].mean()) if (r < 0).any() else float("nan"),
        trades_per_month=n / months,
    )
    return base
