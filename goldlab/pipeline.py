"""End-to-end research run: data -> backtests -> validation -> charts -> REPORT.md."""

from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

import numpy as np
import pandas as pd

from .config import CostConfig, RiskConfig
from .data import DataQuality
from .metrics import monthly_returns, summarize
from .plots import equity_drawdown, sensitivity_heatmap
from .strategies import ALL_STRATEGIES
from .strategies.base import Strategy
from .validation import (WalkForwardResult, one_at_a_time, run_grid, run_window,
                         select_best, sensitivity_table, split_point, walk_forward)

# Pre-registered verdict thresholds. Decided before any real data was seen.
EDGE_MIN_T = 2.0
EDGE_MIN_GRID_POSITIVE = 0.6
EDGE_MIN_WF_TRADES = 30
COST_STRESS = 2.0  # multiply spread and slippage
LOOKS_GOOD_IS_R = 0.05  # "looks good in-sample" = IS expectancy above this

# Untouched holdout (docs/regime-switch-test.md, pre-registered 2026-11-13).
# Bars before HOLDOUT_END were never used by round 1; the development window starts there.
HOLDOUT_END = "2024-10-21"
HOLDOUT_MIN_MONTHS = 12
HOLDOUT_MIN_TRADES = 30
HOLDOUT_MIN_T = 1.5
MIN_DEV_DAYS = 365  # below this the split is not made and all data is the development window
BASELINE = "london_breakout"


@dataclass
class RunConfig:
    is_fraction: float = 0.7
    train_months: int = 12
    test_months: int = 3
    workers: int = max(1, (os.cpu_count() or 2) - 1)
    costs: CostConfig = field(default_factory=CostConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    synthetic: bool = False
    data_label: str = ""
    holdout_end: str | None = HOLDOUT_END  # None: no holdout split


@dataclass
class StrategyReport:
    name: str
    default: Strategy
    stats: dict  # label -> summarize() dict
    optimized: Strategy
    wf: WalkForwardResult
    wf_stats: dict
    sens: pd.DataFrame
    oat: pd.DataFrame
    frac_positive: float
    monthly: pd.Series
    verdict: str
    reasons: list[str]
    by_tag: pd.DataFrame = field(default_factory=pd.DataFrame)  # default-param trades per setup
    holdout: dict | None = None  # label -> summarize() dict on the untouched holdout
    adoption: dict | None = None  # pre-registered adoption decision (holdout_required strategies)


def _stress(costs: CostConfig) -> CostConfig:
    return replace(costs, spread=costs.spread * COST_STRESS, slippage=costs.slippage * COST_STRESS)


def _wf_stats(wf: WalkForwardResult, initial: float) -> dict:
    from .engine import BacktestResult
    if wf.equity.empty:
        return summarize(BacktestResult(pd.DataFrame(), pd.Series([initial], dtype=float), initial))
    return summarize(BacktestResult(wf.trades, wf.equity, initial))


def verdict(st: dict, wf: dict, frac_pos: float) -> tuple[str, list[str]]:
    is_d, oos, is_opt, stress = st["IS (default)"], st["OOS (default)"], st["IS (optimised)"], st["OOS 2x costs"]
    checks = {
        f"walk-forward expectancy > 0 (got {wf['expectancy_r']:+.3f}R)": wf["expectancy_r"] > 0,
        f"walk-forward t-stat >= {EDGE_MIN_T} (got {wf['r_t_stat']:.2f})": wf["r_t_stat"] >= EDGE_MIN_T,
        f"walk-forward trades >= {EDGE_MIN_WF_TRADES} (got {wf['trades']})": wf["trades"] >= EDGE_MIN_WF_TRADES,
        f"default params OOS expectancy > 0 (got {oos['expectancy_r']:+.3f}R)": oos["expectancy_r"] > 0,
        f"OOS still positive at {COST_STRESS:g}x costs (got {stress['expectancy_r']:+.3f}R)": stress["expectancy_r"] > 0,
        f">= {EDGE_MIN_GRID_POSITIVE:.0%} of parameter grid positive (got {frac_pos:.0%})": frac_pos >= EDGE_MIN_GRID_POSITIVE,
    }
    clean = {k: (bool(v) if v == v else False) for k, v in checks.items()}  # NaN -> fail
    failed = [k for k, ok in clean.items() if not ok]
    passed = [k for k, ok in clean.items() if ok]
    looks_good_is = any(s["trades"] and s["expectancy_r"] > LOOKS_GOOD_IS_R
                        for s in (is_d, is_opt))
    if not failed:
        return "EDGE AFTER COSTS (tentative)", passed
    wf_pos = wf["expectancy_r"] > 0 if wf["expectancy_r"] == wf["expectancy_r"] else False
    oos_pos = oos["expectancy_r"] > 0 if oos["expectancy_r"] == oos["expectancy_r"] else False
    full_pos = st["Full (default)"]["expectancy_r"] > 0
    if wf_pos and oos_pos and full_pos:
        return "INCONCLUSIVE (positive out-of-sample, but not convincing)", failed
    if looks_good_is:
        return "IN-SAMPLE ONLY (does not survive out-of-sample)", failed
    return "NO EDGE (loses even in-sample after costs)", failed


def analyse_strategy(cls, df: pd.DataFrame, cfg: RunConfig, out: Path) -> StrategyReport:
    costs, risk = cfg.costs, cfg.risk
    n = len(df)
    sp = split_point(df, cfg.is_fraction)
    split_ts = df.index[sp]
    default = cls()
    sig = default.signals(df)

    full = run_window(df, sig, 0, n, costs, risk)
    stats = {
        "Full (default)": summarize(full),
        "IS (default)": summarize(run_window(df, sig, 0, sp, costs, risk)),
        "OOS (default)": summarize(run_window(df, sig, sp, n, costs, risk)),
        "OOS 2x costs": summarize(run_window(df, sig, sp, n, _stress(costs), risk)),
    }
    full.trades.to_csv(out / f"trades_{cls.name}_default.csv", index=False)

    grid = run_grid(df, default.param_grid(), costs, risk, cfg.workers)
    opt = select_best(grid, df.index[0], split_ts)
    osig = opt.signals(df)
    stats["IS (optimised)"] = summarize(run_window(df, osig, 0, sp, costs, risk))
    stats["OOS (optimised)"] = summarize(run_window(df, osig, sp, n, costs, risk))

    wf = walk_forward(df, grid, costs, risk, cfg.train_months, cfg.test_months)
    wf_stats = _wf_stats(wf, risk.initial_equity)
    stats["Walk-forward OOS"] = wf_stats

    sens = sensitivity_table(grid)
    sens_is = sensitivity_table(grid, df.index[0], split_ts)
    sens.to_csv(out / f"sensitivity_{cls.name}_full.csv", index=False)
    sens_is.to_csv(out / f"sensitivity_{cls.name}_is.csv", index=False)
    valid = sens[sens["trades"] >= 20]
    frac_pos = float((valid["expectancy_r"] > 0).mean()) if len(valid) else 0.0
    oat = one_at_a_time(sens, default)

    v, reasons = verdict(stats, wf_stats, frac_pos)

    tag = " [SYNTHETIC DATA]" if cfg.synthetic else ""
    extra = {"walk-forward OOS (stitched)": wf.equity} if not wf.equity.empty else None
    equity_drawdown(full.equity, f"{cls.name}: equity & drawdown after costs{tag}",
                    out / f"equity_{cls.name}.png", split=split_ts, extra=extra)
    keys = list(cls.grid)
    sensitivity_heatmap(sens, keys[-1], keys[-2],
                        f"{cls.name}: full-period expectancy (R){tag}\n"
                        + ", ".join(f"{k}={default.params[k]}" for k in keys[:-2]),
                        out / f"sensitivity_{cls.name}.png",
                        fixed={k: default.params[k] for k in keys[:-2]})

    return StrategyReport(cls.name, default, stats, opt, wf, wf_stats, sens, oat, frac_pos,
                          monthly_returns(full.equity), v, reasons, by_tag=by_tag(full.trades))


def by_tag(trades: pd.DataFrame) -> pd.DataFrame:
    """Trades, win rate and expectancy per setup tag."""
    if trades.empty:
        return pd.DataFrame(columns=["tag", "trades", "win_rate", "expectancy_r"])
    g = trades.groupby("tag")
    return pd.DataFrame({"trades": g.size(), "win_rate": g["pnl_usd"].apply(lambda p: float((p > 0).mean())),
                         "expectancy_r": g["r_multiple"].mean()}).reset_index()


# --------------------------------------------------------------------------- holdout


def split_holdout(df: pd.DataFrame, cfg: RunConfig) -> tuple[pd.DataFrame, pd.DataFrame | None]:
    """(development window, untouched holdout). The holdout is every bar before
    `cfg.holdout_end`; None if there are no such bars or the development window
    would be shorter than MIN_DEV_DAYS."""
    if not cfg.holdout_end:
        return df, None
    cut = pd.Timestamp(cfg.holdout_end, tz="UTC")
    if df.index[0] >= cut:
        return df, None
    dev = df[df.index >= cut]
    if dev.empty or (dev.index[-1] - dev.index[0]).days < MIN_DEV_DAYS:
        return df, None
    return dev, df[df.index < cut]


def holdout_months(hold: pd.DataFrame | None) -> float:
    if hold is None or hold.empty:
        return 0.0
    return (hold.index[-1] - hold.index[0]).days / 30.44


def run_holdout(cls, hold: pd.DataFrame, cfg: RunConfig, out: Path | None = None) -> dict:
    """Default parameters, run once on the holdout. No tuning of any kind."""
    sig = cls().signals(hold)
    n = len(hold)
    res = run_window(hold, sig, 0, n, cfg.costs, cfg.risk)
    if out is not None:
        res.trades.to_csv(out / f"trades_{cls.name}_holdout.csv", index=False)
    return {"Holdout (default)": summarize(res),
            "Holdout 2x costs": summarize(run_window(hold, sig, 0, n, _stress(cfg.costs), cfg.risk))}


def adoption_decision(rep: StrategyReport, months: float, baseline_wf_r: float | None) -> dict:
    """The pre-registered three-part adoption rule (docs/regime-switch-test.md)."""
    h = (rep.holdout or {}).get("Holdout (default)")
    h2 = (rep.holdout or {}).get("Holdout 2x costs")
    wf_r = rep.wf_stats["expectancy_r"]

    def ok(v):  # NaN / None -> fail
        return bool(v) if v == v and v is not None else False

    checks = {
        f"development window: EDGE AFTER COSTS under the lab's rules (got {rep.verdict})":
            rep.verdict.startswith("EDGE"),
        (f"walk-forward expectancy above the baseline {BASELINE}'s (got {_fmt(wf_r, 'r')}R vs "
         f"{_fmt(baseline_wf_r, 'r')}R)"):
            ok(baseline_wf_r is not None and wf_r > baseline_wf_r),
        f"holdout covers >= {HOLDOUT_MIN_MONTHS} months (got {months:.1f})": months >= HOLDOUT_MIN_MONTHS,
        f"holdout trades >= {HOLDOUT_MIN_TRADES} (got {h['trades'] if h else 0})":
            ok(h is not None and h["trades"] >= HOLDOUT_MIN_TRADES),
        f"holdout expectancy > 0 (got {_fmt(h['expectancy_r'] if h else None, 'r')}R)":
            ok(h is not None and h["expectancy_r"] > 0),
        f"holdout t-stat >= {HOLDOUT_MIN_T} (got {_fmt(h['r_t_stat'] if h else None)})":
            ok(h is not None and h["r_t_stat"] >= HOLDOUT_MIN_T),
        (f"holdout still positive at {COST_STRESS:g}x costs "
         f"(got {_fmt(h2['expectancy_r'] if h2 else None, 'r')}R)"):
            ok(h2 is not None and h2["expectancy_r"] > 0),
    }
    checks = {k: bool(v) for k, v in checks.items()}
    return {"adopted": all(checks.values()), "checks": checks,
            "dev_passed": all(list(checks.values())[:2]),
            "holdout_available": months >= HOLDOUT_MIN_MONTHS}


def apply_adoption(reports: list[StrategyReport], months: float) -> None:
    """Decide adoption for strategies whose verdict needs the holdout, and make
    sure their verdict only says EDGE when the whole rule holds (the bot's
    `strategy = "auto"` switches to any EDGE verdict)."""
    base = next((r for r in reports if r.name == BASELINE), None)
    base_wf = base.wf_stats["expectancy_r"] if base is not None else None
    for rep in reports:
        if not getattr(type(rep.default), "holdout_required", False):
            continue
        rep.adoption = adoption_decision(rep, months, base_wf)
        if rep.adoption["adopted"]:
            rep.verdict = "EDGE AFTER COSTS (confirmed on the untouched holdout)"
        elif rep.verdict.startswith("EDGE"):
            missing = "holdout data missing" if not rep.adoption["holdout_available"] else \
                "failed the adoption rule"
            rep.verdict = f"INCONCLUSIVE (edge on the development window, {missing})"


# --------------------------------------------------------------------------- report


def _fmt(v, kind="f"):
    if v is None or (isinstance(v, float) and (math.isnan(v))):
        return "n/a"
    if isinstance(v, float) and math.isinf(v):
        return "inf"
    if kind == "pct":
        return f"{v * 100:+.1f}%"
    if kind == "pct0":
        return f"{v * 100:.1f}%"
    if kind == "r":
        return f"{v:+.3f}"
    if kind == "i":
        return f"{int(v)}"
    return f"{v:.2f}"


METRIC_COLS = [
    ("trades", "Trades", "i"), ("win_rate", "Win rate", "pct0"), ("profit_factor", "PF", "f"),
    ("expectancy_r", "Exp. (R)", "r"), ("r_t_stat", "t-stat", "f"),
    ("total_return", "Return", "pct"), ("max_drawdown", "Max DD", "pct0"),
    ("longest_losing_streak", "Longest losing streak", "i"),
]


def _metrics_table(stats: dict) -> str:
    head = "| Period | " + " | ".join(c[1] for c in METRIC_COLS) + " |"
    sep = "|---|" + "---:|" * len(METRIC_COLS)
    rows = [f"| {lbl} | " + " | ".join(_fmt(s[k], f) for k, _, f in METRIC_COLS) + " |"
            for lbl, s in stats.items()]
    return "\n".join([head, sep, *rows])


def _monthly_table(m: pd.Series) -> str:
    if m.empty:
        return "_no data_"
    df = pd.DataFrame({"y": m.index.year, "m": m.index.month, "r": m.values})
    piv = df.pivot(index="y", columns="m", values="r")
    yearly = df.groupby("y")["r"].apply(lambda r: float(np.prod(1 + r) - 1))
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    lines = ["| Year | " + " | ".join(months) + " | Year |", "|---|" + "---:|" * 13]
    for y in piv.index:
        cells = [_fmt(piv.loc[y].get(mm, float("nan")), "pct") for mm in range(1, 13)]
        lines.append(f"| {y} | " + " | ".join(cells) + f" | {_fmt(yearly[y], 'pct')} |")
    return "\n".join(lines)


def _oat_table(oat: pd.DataFrame) -> str:
    if oat.empty:
        return "_default parameters are not on the grid_"
    lines = ["| Parameter | Value | Trades | Exp. (R) | PF |", "|---|---:|---:|---:|---:|"]
    for _, r in oat.iterrows():
        v = f"**{r['value']:g}**" if r["is_default"] else f"{r['value']:g}"
        lines.append(f"| {r['param']} | {v} | {r['trades']} | {_fmt(r['expectancy_r'], 'r')} | "
                     f"{_fmt(r['profit_factor'])} |")
    return "\n".join(lines)


def _wf_table(wf: WalkForwardResult) -> str:
    if not wf.folds:
        return "_not enough data for a single walk-forward fold_"
    lines = ["| Train | Test | Chosen params | Train exp. (R) | Test trades | Test exp. (R) | Test return |",
             "|---|---|---|---:|---:|---:|---:|"]
    for f in wf.folds:
        p = ", ".join(f"{k}={v:g}" for k, v in f["params"].items())
        lines.append(f"| {f['train']} | {f['test']} | {p} | {_fmt(f['train_expectancy_r'], 'r')} | "
                     f"{f['test_trades']} | {_fmt(f['test_expectancy_r'], 'r')} | {_fmt(f['test_return'], 'pct')} |")
    return "\n".join(lines)


def _holdout_table(reports: list[StrategyReport]) -> str:
    cols = [("trades", "Trades", "i"), ("win_rate", "Win rate", "pct0"), ("profit_factor", "PF", "f"),
            ("expectancy_r", "Exp. (R)", "r"), ("r_t_stat", "t-stat", "f"),
            ("total_return", "Return", "pct"), ("max_drawdown", "Max DD", "pct0")]
    head = "| Strategy | " + " | ".join(c[1] for c in cols) + " | Exp. at 2x costs (R) |"
    lines = [head, "|---|" + "---:|" * (len(cols) + 1)]
    for rep in reports:
        if not rep.holdout:
            continue
        h, h2 = rep.holdout["Holdout (default)"], rep.holdout["Holdout 2x costs"]
        lines.append(f"| {rep.name} | " + " | ".join(_fmt(h[k], f) for k, _, f in cols)
                     + f" | {_fmt(h2['expectancy_r'], 'r')} |")
    return "\n".join(lines)


def _by_tag_table(t: pd.DataFrame) -> str:
    if t.empty:
        return "_no trades_"
    lines = ["| Setup | Trades | Win rate | Exp. (R) |", "|---|---:|---:|---:|"]
    for _, r in t.iterrows():
        lines.append(f"| {r['tag']} | {int(r['trades'])} | {_fmt(r['win_rate'], 'pct0')} | "
                     f"{_fmt(r['expectancy_r'], 'r')} |")
    return "\n".join(lines)


def _adoption_section(reports: list[StrategyReport]) -> list[str]:
    L = []
    for rep in reports:
        if rep.adoption is None:
            continue
        a = rep.adoption
        L += [f"## {rep.name}: pre-registered adoption decision", "",
              f"**{'ADOPTED' if a['adopted'] else 'NOT ADOPTED'}** "
              "(rule fixed in [docs/regime-switch-test.md](docs/regime-switch-test.md) before the run)", ""]
        L += [f"- {'✅' if passed else '❌'} {check}" for check, passed in a["checks"].items()]
        L.append("")
        if a["adopted"]:
            L += ["Next step (stopping rule): the bot's `strategy = \"auto\"` now picks it, still in paper "
                  "mode, for at least 2 months before any demo-account trading.", ""]
        elif a["dev_passed"] and not a["holdout_available"]:
            L += ["The test is **not finished**: the holdout data is missing or shorter than "
                  f"{HOLDOUT_MIN_MONTHS} months. Rerun the research workflow with `source = dukascopy`.", ""]
        else:
            L += ["Stopping rule: the strategy search ends here. The bot stays on the London breakout "
                  "in paper mode as a live experiment.", ""]
    return L


def write_report(reports: list[StrategyReport], q: DataQuality, cfg: RunConfig, df: pd.DataFrame,
                 path: Path, out: Path, hold: pd.DataFrame | None = None) -> None:
    c, r = cfg.costs, cfg.risk
    rel = os.path.relpath(out, path.parent)
    sp = split_point(df, cfg.is_fraction)
    L = []
    title = "XAUUSD intraday backtest report"
    if cfg.synthetic:
        title += " — SYNTHETIC DATA SMOKE RUN (results are meaningless)"
    L += [f"# {title}", ""]
    if cfg.synthetic:
        L += ["> **This report was generated on a synthetic random walk to test the pipeline.**",
              "> It says nothing about real gold markets.", ""]

    L += ["## Verdict", ""]
    for rep in reports:
        L.append(f"- **{rep.name}: {rep.verdict}**")
    winners = [x.name for x in reports if x.verdict.startswith("EDGE")]
    L += ["", ("Bottom line: " + (
        f"only {', '.join(winners)} clears every pre-registered hurdle. Treat it as a candidate "
        "for forward/paper testing, not as proven." if winners else
        f"none of the {len(reports)} strategies shows an edge that survives costs, out-of-sample data, "
        "walk-forward re-optimisation and parameter perturbation. Do not trade them as specified."
    )), ""]
    L += ["Verdict rules (fixed before running): an edge requires positive walk-forward OOS "
          f"expectancy with t-stat >= {EDGE_MIN_T} over >= {EDGE_MIN_WF_TRADES} trades, positive "
          f"default-parameter OOS expectancy, still positive at {COST_STRESS:g}x spread+slippage, "
          f"and >= {EDGE_MIN_GRID_POSITIVE:.0%} of the parameter grid positive.", ""]
    L += _adoption_section(reports)

    L += ["## Data", "",
          f"- Source: {cfg.data_label}",
          f"- Bars: {q.rows_after_clean:,} x {q.bar_minutes}-minute, {q.first_bar} → {q.last_bar} (UTC)",
          (f"- Development window (everything below unless marked holdout): {len(df):,} bars, "
           f"{df.index[0]:%Y-%m-%d} → {df.index[-1]:%Y-%m-%d}. Untouched holdout: {len(hold):,} bars, "
           f"{hold.index[0]:%Y-%m-%d} → {hold.index[-1]:%Y-%m-%d} ({holdout_months(hold):.1f} months)"
           if hold is not None else
           "- No untouched holdout in this data (nothing before "
           f"{cfg.holdout_end or 'the holdout cut-off'}, or too little after it)"),
          f"- Cleaning: {q.duplicates_removed} duplicates, {q.invalid_rows_removed} invalid rows, "
          f"{q.weekend_rows_removed} weekend rows, {q.spikes_removed} spikes removed; "
          f"{q.ohlc_fixed} OHLC inconsistencies fixed; {q.intraday_gaps} intraday gaps > 3 bars "
          "(not filled — see data_quality.json)",
          f"- In-sample: {df.index[0]:%Y-%m-%d} → {df.index[sp - 1]:%Y-%m-%d}; "
          f"out-of-sample: {df.index[sp]:%Y-%m-%d} → {df.index[-1]:%Y-%m-%d} ({1 - cfg.is_fraction:.0%})",
          f"- Walk-forward: {cfg.train_months}-month train / {cfg.test_months}-month test, rolling", ""]

    L += ["## Costs and risk (flagged assumptions)", "",
          f"- **Spread: ${c.spread:.2f}/oz round trip (conservative default — check your broker)**, "
          f"x{c.session_spread_mult.get('Asia', 1):g} in Asia, x{c.session_spread_mult.get('Off', 1):g} "
          "outside main sessions. Bars are treated as mid prices; each fill pays half the spread.",
          f"- Slippage: ${c.slippage:.2f}/oz per side on market and stop fills (none on limit targets).",
          f"- Swap: long ${c.swap_long:+.2f}, short ${c.swap_short:+.2f} per oz per rollover (x3 Wednesdays). "
          "All strategies are flat before session end, so swap should never be charged.",
          f"- Risk: {r.risk_pct:.1%} of equity per trade (a clean stop-out = -1R including costs), hard stop on "
          f"every trade, daily loss limit {r.daily_loss_limit_pct:.0%}, max {r.max_trades_per_day} trades/day, "
          "flat before session end and before weekends.",
          "- Fill pessimism: stop assumed before target when both are inside one bar; gaps fill at the open.",
          ""]

    if hold is not None:
        L += ["## Untouched holdout (default parameters, run once)", "",
              f"Data before {cfg.holdout_end}, which no earlier research run used. Every strategy runs once "
              "with its default parameters and no tuning. Only the regime switch's adoption depends on "
              "it; for the round-1 strategies it is information only.", "",
              _holdout_table(reports), ""]

    for rep in reports:
        L += [f"## {rep.name}", "", f"**Verdict: {rep.verdict}**", ""]
        L += ["Why:" if not rep.verdict.startswith("EDGE") else "Passed:", ""]
        L += [f"- {x}" for x in rep.reasons] + [""]
        L += [f"Default parameters: `{rep.default.label()}`  ",
              f"In-sample optimised: `{', '.join(f'{k}={rep.optimized.params[k]:g}' for k in rep.optimized.grid)}`", ""]
        L += [_metrics_table(rep.stats), ""]
        L += [f"![equity]({rel}/equity_{rep.name}.png)", ""]
        if len(rep.by_tag) > 2:  # more than a plain long/short pair
            L += ["### Trades by setup (default parameters, full period)", "", _by_tag_table(rep.by_tag), ""]
        L += ["### Walk-forward folds", "", _wf_table(rep.wf), ""]
        L += ["### Parameter sensitivity", "",
              f"{rep.frac_positive:.0%} of {len(rep.sens)} parameter combinations have positive full-period "
              "expectancy (combos with >= 20 trades). One parameter at a time, others at default "
              "(default in bold):", "", _oat_table(rep.oat), "",
              f"![sensitivity]({rel}/sensitivity_{rep.name}.png)", ""]
        L += ["### Monthly returns (default parameters, full period)", "", _monthly_table(rep.monthly), ""]

    L += ["## Caveats", "",
          "- Bar data cannot show the true intrabar path; fills are modelled pessimistically but not exactly.",
          "- Real spreads spike around news (NFP, CPI, FOMC). A flat spread understates that tail cost.",
          "- 2 years is a short sample for intraday gold; a single regime (e.g. a strong trend) can dominate.",
          f"- {len(reports)} strategies x large parameter grids = many implicit tests; a t-stat of 2 is a minimum bar, not proof.",
          ""]
    path.write_text("\n".join(L))


def run(df: pd.DataFrame, q: DataQuality, cfg: RunConfig, out: Path, report_path: Path,
        strategies=None) -> list[StrategyReport]:
    out.mkdir(parents=True, exist_ok=True)
    q.to_json(out / "data_quality.json")
    dev, hold = split_holdout(df, cfg)
    months = holdout_months(hold)
    if hold is not None:
        print(f"[goldlab] development window {dev.index[0]:%Y-%m-%d} -> {dev.index[-1]:%Y-%m-%d}; "
              f"untouched holdout {hold.index[0]:%Y-%m-%d} -> {hold.index[-1]:%Y-%m-%d} "
              f"({months:.1f} months)", flush=True)
    reports = []
    for cls in strategies or ALL_STRATEGIES:
        print(f"[goldlab] {cls.name} ...", flush=True)
        rep = analyse_strategy(cls, dev, cfg, out)
        if hold is not None:
            rep.holdout = run_holdout(cls, hold, cfg, out)
        reports.append(rep)
    apply_adoption(reports, months)
    summary = {rep.name: {"verdict": rep.verdict, "stats": rep.stats,
                          "wf_folds": rep.wf.folds, "grid_positive": rep.frac_positive,
                          "holdout": rep.holdout, "adoption": rep.adoption,
                          "by_tag": rep.by_tag.to_dict(orient="records")}
               for rep in reports}
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    (out / "config.json").write_text(json.dumps(
        {"costs": asdict(cfg.costs), "risk": asdict(cfg.risk), "is_fraction": cfg.is_fraction,
         "train_months": cfg.train_months, "test_months": cfg.test_months,
         "holdout_end": cfg.holdout_end if hold is not None else None}, indent=2))
    write_report(reports, q, cfg, dev, report_path, out, hold=hold)
    return reports
