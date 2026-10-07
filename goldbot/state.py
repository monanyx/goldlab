"""Persistent bot state: one JSON file plus append-only CSV logs in `state_dir`."""

from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from goldlab.engine import EngineState

STATE_FILE = "bot_state.json"
SIGNALS_FILE = "signals.csv"
PAPER_TRADES_FILE = "paper_trades.csv"
SIGNAL_COLUMNS = ["created_utc", "mode", "strategy", "direction", "order_type", "entry", "stop_loss",
                  "take_profit", "size_oz", "risk_usd", "valid_until_utc", "status", "reason"]


@dataclass
class BotState:
    engine: EngineState
    mode: str = ""
    strategy: str = ""
    halted: bool = False  # set by the Telegram /stop command
    telegram_offset: int = 0
    summary_sent_for: str | None = None  # local date of the last daily summary
    days: dict = field(default_factory=dict)  # trade_date -> {"signals": n, "rules": {reason: n}}
    live: dict = field(default_factory=dict)  # broker bookkeeping in live mode
    last_error_alert: str | None = None

    # ---- per-day stats

    def day(self, trade_date) -> dict:
        key = pd.Timestamp(trade_date).strftime("%Y-%m-%d")
        d = self.days.setdefault(key, {"signals": 0, "rules": {}})
        for old in sorted(self.days)[:-14]:  # keep two weeks
            del self.days[old]
        return d

    def rule(self, trade_date, reason: str) -> bool:
        """Count a rule trigger; True the first time it fires that day."""
        rules = self.day(trade_date)["rules"]
        rules[reason] = rules.get(reason, 0) + 1
        return rules[reason] == 1

    # ---- io

    def to_dict(self) -> dict:
        return {"engine": self.engine.to_dict(), "mode": self.mode, "strategy": self.strategy,
                "halted": self.halted, "telegram_offset": self.telegram_offset,
                "summary_sent_for": self.summary_sent_for, "days": self.days, "live": self.live,
                "last_error_alert": self.last_error_alert}

    @classmethod
    def from_dict(cls, d: dict) -> "BotState":
        d = dict(d)
        d["engine"] = EngineState.from_dict(d["engine"])
        return cls(**d)


def load(state_dir: str | Path, initial_equity: float) -> BotState:
    p = Path(state_dir) / STATE_FILE
    if p.exists():
        return BotState.from_dict(json.loads(p.read_text()))
    return BotState(engine=EngineState(equity=initial_equity))


def save(state_dir: str | Path, st: BotState) -> None:
    d = Path(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / (STATE_FILE + ".tmp")
    tmp.write_text(json.dumps(st.to_dict(), indent=1))
    os.replace(tmp, d / STATE_FILE)


def append_signal(state_dir: str | Path, row: dict) -> None:
    p = Path(state_dir) / SIGNALS_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists()
    with p.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SIGNAL_COLUMNS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(row)


def write_paper_trades(state_dir: str | Path, st: BotState) -> None:
    rows = [t.to_dict() for t in st.engine.trades]
    df = pd.DataFrame(rows)
    p = Path(state_dir) / PAPER_TRADES_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(p, index=False)
