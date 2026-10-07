"""Pluggable strategy resolution.

`strategy = "auto"` uses the best strategy from the research report
(results/summary.json, written together with REPORT.md) if one cleared the
"EDGE" verdict; otherwise it falls back to the London breakout.
Any class implementing goldlab's `Strategy.signals(df) -> StrategySignals`
can be plugged in with `strategy = "my_package.my_module:MyStrategy"`.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

from goldlab.strategies import ALL_STRATEGIES, LondonBreakout
from goldlab.strategies.base import Strategy

REGISTRY = {cls.name: cls for cls in ALL_STRATEGIES}
DEFAULT = LondonBreakout.name


def pick_from_report(summary_path: str | Path) -> tuple[str, str]:
    p = Path(summary_path)
    if not p.exists():
        return DEFAULT, f"no research report ({p}) found, using the default London breakout"
    summary = json.loads(p.read_text())
    edges = {name: s for name, s in summary.items() if str(s.get("verdict", "")).startswith("EDGE")}
    if not edges:
        return DEFAULT, ("the research report found NO strategy with an edge after costs; "
                         "falling back to the London breakout (treat signals as unproven)")
    best = max(edges, key=lambda n: edges[n]["stats"]["Walk-forward OOS"]["expectancy_r"])
    return best, f"chosen from the research report: {best} ({edges[best]['verdict']})"


def load_class(name: str) -> type[Strategy]:
    if ":" in name:
        mod, attr = name.split(":", 1)
        return getattr(importlib.import_module(mod), attr)
    if name not in REGISTRY:
        raise ValueError(f"unknown strategy '{name}'; known: {sorted(REGISTRY)} or 'module:Class'")
    return REGISTRY[name]


def resolve(cfg) -> tuple[Strategy, str]:
    name, why = (pick_from_report(cfg.report_summary) if cfg.strategy == "auto"
                 else (cfg.strategy, "set in config"))
    cls = load_class(name)
    strat = cls(**cfg.strategy_params)
    if not hasattr(strat, "signals"):
        raise TypeError(f"{name} does not implement signals(df)")
    return strat, why
