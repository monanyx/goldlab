from __future__ import annotations

import itertools
from dataclasses import dataclass, fields, replace
from typing import ClassVar

import pandas as pd

from ..engine import StrategySignals


@dataclass(frozen=True)
class Strategy:
    """Base class. Subclasses are frozen dataclasses whose fields are parameters.

    `grid` lists the values explored by walk-forward optimisation and the
    sensitivity check; the dataclass defaults are the a-priori parameters,
    fixed before looking at any results.
    """

    name: ClassVar[str] = "base"
    grid: ClassVar[dict[str, list]] = {}

    def signals(self, df: pd.DataFrame) -> StrategySignals:  # pragma: no cover
        raise NotImplementedError

    @property
    def params(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    def with_params(self, **kw) -> "Strategy":
        return replace(self, **kw)

    def param_grid(self) -> list["Strategy"]:
        keys = list(self.grid)
        return [self.with_params(**dict(zip(keys, vals)))
                for vals in itertools.product(*(self.grid[k] for k in keys))]

    def label(self) -> str:
        return ", ".join(f"{k}={v}" for k, v in self.params.items())
