from .base import Strategy
from .london_breakout import LBConfirm, LBEarlyExit, LBTightRange, LBTrend, LondonBreakout
from .ny_momentum import NYMomentum
from .vwap_fade import VWAPFade

BASE_STRATEGIES = [LondonBreakout, NYMomentum, VWAPFade]
LB_VARIANTS = [LBTrend, LBConfirm, LBEarlyExit, LBTightRange]  # docs/improvement-tests.md
ALL_STRATEGIES = BASE_STRATEGIES + LB_VARIANTS

__all__ = ["Strategy", "LondonBreakout", "NYMomentum", "VWAPFade", "LBTrend", "LBConfirm",
           "LBEarlyExit", "LBTightRange", "BASE_STRATEGIES", "LB_VARIANTS", "ALL_STRATEGIES"]
