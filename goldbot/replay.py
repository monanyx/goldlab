"""Run the *exact* live code path over historical bars and compare it with the backtest.

The bot is driven like the scheduler would drive it: a fresh `Bot` per run,
state saved to and reloaded from disk between runs, the feed only revealing
bars that had completed by the simulated clock, a rolling history window, and
irregular (randomly late / skipped) run times like GitHub's cron. The paper
trades it produces must equal `Backtester.run()` on the same bars.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
import pandas as pd

from goldlab.data import clean
from goldlab.engine import Backtester
from goldlab.validation import slice_signals

from .bot import Bot
from .config import BotConfig
from .feeds import FrameFeed
from .notify import ConsoleNotifier
from .state import load

COMPARE = ["entry_time", "exit_time", "side", "entry_price", "exit_price", "stop", "target",
           "units", "pnl_usd", "exit_reason"]


@dataclass
class ReplayResult:
    bot_trades: pd.DataFrame
    backtest_trades: pd.DataFrame
    runs: int
    first_bar: pd.Timestamp
    last_bar: pd.Timestamp
    match: bool
    mismatches: list
    alerts: int


def _frame(trades) -> pd.DataFrame:
    df = pd.DataFrame([t.to_dict() if hasattr(t, "to_dict") else t for t in trades])
    if df.empty:
        return pd.DataFrame(columns=COMPARE)
    for k in ("entry_time", "exit_time"):
        df[k] = pd.to_datetime(df[k], utc=True)
    return df[COMPARE].reset_index(drop=True)


def compare(a: pd.DataFrame, b: pd.DataFrame) -> list[str]:
    out = []
    if len(a) != len(b):
        out.append(f"trade count differs: bot {len(a)} vs backtest {len(b)}")
    for i in range(min(len(a), len(b))):
        for c in COMPARE:
            x, y = a.at[i, c], b.at[i, c]
            if isinstance(x, (float, np.floating)) or isinstance(y, (float, np.floating)):
                same = (pd.isna(x) and pd.isna(y)) or np.isclose(float(x), float(y), rtol=1e-12, atol=1e-9)
            else:
                same = x == y
            if not same:
                out.append(f"trade {i} {c}: bot {x} vs backtest {y}")
    return out


def replay(raw: pd.DataFrame, cfg: BotConfig, start: pd.Timestamp | None = None,
           end: pd.Timestamp | None = None, max_skip: int = 3, max_delay_min: int = 12,
           seed: int = 0, state_dir: str | None = None) -> ReplayResult:
    tf = cfg.timeframe_minutes
    tmp = tempfile.mkdtemp(prefix="goldbot-replay-") if state_dir is None else state_dir
    cfg = replace(cfg, mode="paper", state_dir=tmp, kill_switch=False)
    cfg._live_confirm, cfg._forbid_live = "", True
    raw = raw.sort_index()
    df, _ = clean(raw, spike_threshold=None)

    # simulated scheduler: run after bar closes, sometimes late, sometimes skipping runs
    rng = np.random.default_rng(seed)
    closes = df.index + pd.Timedelta(minutes=tf)
    first_possible = df.index[min(len(df) - 1, cfg.history_bars)] + pd.Timedelta(minutes=tf)
    start = max(pd.Timestamp(start) if start is not None else first_possible, first_possible)
    end = pd.Timestamp(end) if end is not None else closes[-1]
    candidates = closes[(closes >= start) & (closes <= end)]
    runs, k = [], 0
    while k < len(candidates):
        delay = pd.Timedelta(minutes=int(rng.integers(0, max_delay_min + 1)))
        runs.append(candidates[k] + delay)
        k += int(rng.integers(1, max_skip + 1))

    feed = FrameFeed(raw, tf, cfg.history_bars)
    notifier = ConsoleNotifier(quiet=True)
    for now in runs:
        Bot(cfg, feed, notifier).run_once(now)  # fresh bot each run, like a cron job

    st = load(tmp, cfg.account_balance)
    bot = _frame(st.engine.trades)

    # the backtest over exactly the bars the bot processed
    first_bar = df.index[df.index.searchsorted(runs[0] - pd.Timedelta(minutes=tf), side="right") - 1]
    last_bar = st.engine.last_time
    i0 = int(df.index.get_loc(first_bar))
    i1 = int(df.index.get_loc(last_bar)) + 1
    strat = Bot(cfg, feed, notifier).strategy
    sig = strat.signals(df)
    bt = Backtester(cfg.cost_config(), cfg.risk_config()).run(df.iloc[i0:i1], slice_signals(sig, i0, i1),
                                                              minutes=tf)
    # the bot keeps an open position at the end; the backtest closes nothing extra either
    btf = _frame(bt.trades.to_dict("records"))
    mism = compare(bot, btf)
    return ReplayResult(bot, btf, len(runs), first_bar, last_bar, not mism, mism, len(notifier.sent))


def report(r: ReplayResult, path: Path) -> None:
    lines = ["# Live-path replay vs backtest", "",
             f"- Bars: {r.first_bar} → {r.last_bar}",
             f"- Simulated bot runs: {r.runs} (fresh process each run, state reloaded from disk, "
             "random late/skipped runs)",
             f"- Alerts generated: {r.alerts}",
             f"- Trades: bot {len(r.bot_trades)}, backtest {len(r.backtest_trades)}",
             f"- **Result: {'MATCH — identical trades' if r.match else 'MISMATCH'}**", ""]
    if r.mismatches:
        lines += ["## Differences", ""] + [f"- {m}" for m in r.mismatches[:50]]
    if not r.bot_trades.empty:
        pnl = r.bot_trades["pnl_usd"].sum()
        lines += ["", f"Total P&L of the replayed period: ${pnl:+,.2f}", ""]
    path.write_text("\n".join(lines) + "\n")
