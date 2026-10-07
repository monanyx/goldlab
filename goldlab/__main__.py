"""Command line entry point: python -m goldlab {run,smoke,fetch-dukascopy,check-data}."""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

from .config import CostConfig, RiskConfig
from .data import clean, find_data_files, load_raw


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--out", type=Path, default=Path("results"))
    p.add_argument("--report", type=Path, default=Path("REPORT.md"))
    p.add_argument("--minutes", type=int, default=None, help="resample to N-minute bars (e.g. 15)")
    p.add_argument("--is-fraction", type=float, default=0.7)
    p.add_argument("--train-months", type=int, default=12)
    p.add_argument("--test-months", type=int, default=3)
    p.add_argument("--workers", type=int, default=None)
    p.add_argument("--spread", type=float, default=None, help="USD/oz round-trip spread")
    p.add_argument("--slippage", type=float, default=None, help="USD/oz per side")
    p.add_argument("--swap-long", type=float, default=None)
    p.add_argument("--swap-short", type=float, default=None)
    p.add_argument("--risk-pct", type=float, default=None, help="e.g. 0.01 for 1%%")
    p.add_argument("--daily-loss", type=float, default=None, help="e.g. 0.03 for 3%%")
    p.add_argument("--max-trades", type=int, default=None)


def _cfg(args, synthetic: bool, label: str):
    from .pipeline import RunConfig

    costs = CostConfig()
    for k, a in (("spread", args.spread), ("slippage", args.slippage),
                 ("swap_long", args.swap_long), ("swap_short", args.swap_short)):
        if a is not None:
            costs = replace(costs, **{k: a})
    risk = RiskConfig()
    for k, a in (("risk_pct", args.risk_pct), ("daily_loss_limit_pct", args.daily_loss),
                 ("max_trades_per_day", args.max_trades)):
        if a is not None:
            risk = replace(risk, **{k: a})
    cfg = RunConfig(is_fraction=args.is_fraction, train_months=args.train_months,
                    test_months=args.test_months, costs=costs, risk=risk,
                    synthetic=synthetic, data_label=label)
    if args.workers:
        cfg.workers = args.workers
    return cfg


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="goldlab", description="XAUUSD intraday research lab")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="full research run on CSVs in --data-dir")
    p_run.add_argument("--data-dir", type=Path, default=Path("data"))
    p_run.add_argument("--source-tz", default=None,
                       help="timezone of naive timestamps (default UTC; MT5 exports need this)")
    _add_common(p_run)

    p_chk = sub.add_parser("check-data", help="load + clean data and print the quality report")
    p_chk.add_argument("--data-dir", type=Path, default=Path("data"))
    p_chk.add_argument("--source-tz", default=None)
    p_chk.add_argument("--minutes", type=int, default=None)

    p_smoke = sub.add_parser("smoke", help="run the whole pipeline on synthetic data")
    _add_common(p_smoke)
    p_smoke.set_defaults(out=Path("smoke_results"), report=Path("smoke_results/REPORT_SYNTHETIC.md"),
                         train_months=6, test_months=2)

    p_f = sub.add_parser("fetch-dukascopy", help="download Dukascopy 1m BID candles -> M5 CSV")
    p_f.add_argument("--start", required=True)
    p_f.add_argument("--end", required=True)
    p_f.add_argument("--out", type=Path, default=Path("data"))

    args = ap.parse_args(argv)

    if args.cmd == "fetch-dukascopy":
        from .dukascopy import fetch
        print(fetch(args.start, args.end, args.out))
        return 0

    if args.cmd in ("run", "check-data"):
        files = find_data_files(args.data_dir)
        if not files:
            print(f"No CSV files in {args.data_dir}/. See README.md 'Data format'.", file=sys.stderr)
            return 2
        df, q = clean(load_raw(files, args.source_tz), args.minutes)
        if args.cmd == "check-data":
            import json
            from dataclasses import asdict
            print(json.dumps(asdict(q), indent=2))
            print(df["session"].value_counts().to_string())
            return 0
        from .pipeline import run
        label = ", ".join(f.name for f in files)
        run(df, q, _cfg(args, False, label), args.out, args.report)
        print(f"[goldlab] wrote {args.report} and {args.out}/")
        return 0

    if args.cmd == "smoke":
        from .pipeline import run
        from .synthetic import random_walk_bars
        raw = random_walk_bars("2023-01-02", "2024-07-01", minutes=args.minutes or 15, seed=7)
        df, q = clean(raw)
        args.out.mkdir(parents=True, exist_ok=True)
        run(df, q, _cfg(args, True, "synthetic random walk (goldlab.synthetic)"), args.out, args.report)
        print(f"[goldlab] wrote {args.report}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
