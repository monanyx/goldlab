from .base import Strategy
from .london_breakout import LondonBreakout
from .ny_momentum import NYMomentum
from .vwap_fade import VWAPFade

ALL_STRATEGIES = [LondonBreakout, NYMomentum, VWAPFade]

__all__ = ["Strategy", "LondonBreakout", "NYMomentum", "VWAPFade", "ALL_STRATEGIES"]
