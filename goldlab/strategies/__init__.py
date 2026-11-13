from .base import Strategy
from .london_breakout import LBConfirm, LBEarlyExit, LBTightRange, LBTrend, LondonBreakout
from .ny_momentum import NYMomentum
from .regime_switch import RegimeSwitch
from .vwap_fade import VWAPFade

BASE_STRATEGIES = [LondonBreakout, NYMomentum, VWAPFade]
LB_VARIANTS = [LBTrend, LBConfirm, LBEarlyExit, LBTightRange]  # docs/improvement-tests.md
ROUND2 = [RegimeSwitch]  # docs/regime-switch-test.md
ALL_STRATEGIES = BASE_STRATEGIES + LB_VARIANTS + ROUND2

__all__ = ["Strategy", "LondonBreakout", "NYMomentum", "VWAPFade", "LBTrend", "LBConfirm",
           "LBEarlyExit", "LBTightRange", "RegimeSwitch", "BASE_STRATEGIES", "LB_VARIANTS",
           "ROUND2", "ALL_STRATEGIES"]
