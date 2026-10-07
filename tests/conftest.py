import numpy as np
import pandas as pd
import pytest

from goldlab.config import CostConfig, RiskConfig
from goldlab.sessions import label_sessions


def make_bars(rows, start="2024-01-09 09:00", minutes=5):
    """rows: list of (open, high, low, close). Timestamps are UTC, consecutive."""
    idx = pd.date_range(start, periods=len(rows), freq=f"{minutes}min", tz="UTC")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx, dtype=float)
    df["volume"] = 100.0
    df["gap_before"] = False
    return label_sessions(df)


@pytest.fixture
def costs():
    # Flat, easy-to-check costs: $0.40 spread (0.20 per side), $0.10 slippage.
    return CostConfig(spread=0.40, slippage=0.10, session_spread_mult={}, swap_long=-1.0, swap_short=-0.5)


@pytest.fixture
def risk():
    return RiskConfig(risk_pct=0.01, daily_loss_limit_pct=0.03, max_trades_per_day=3,
                      initial_equity=100_000.0, min_units=0.001, unit_step=0.001, max_leverage=1000)


def no_flat(df):
    return np.zeros(len(df), dtype=bool)
