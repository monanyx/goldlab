"""Build the public results page (docs/index.html) from the research outputs.

    python scripts/build_site.py                 # reads results/, writes docs/index.html

Everything on the page comes from files the research run writes: results/summary.json,
results/data_quality.json, results/replay_check.md and the default/holdout trade
logs. The page is a single self-contained HTML file (no external scripts, fonts or
images), so GitHub Pages can serve it from the docs/ folder as it is.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

NAMES = {
    "london_breakout": "London breakout",
    "ny_momentum": "NY momentum",
    "vwap_fade": "VWAP fade",
    "lb_trend": "London breakout + trend filter",
    "lb_confirm": "London breakout + close confirmation",
    "lb_early_exit": "London breakout + early exit",
    "lb_tight_range": "London breakout + tight range",
    "regime_switch": "Regime switch",
}
SETUPS = {
    "trend_long_break": "Trend day, long breakout",
    "trend_short_break": "Trend day, short breakout",
    "range_fade_long": "Range day, fade (long)",
    "range_fade_short": "Range day, fade (short)",
}
BOT_STRATEGY = "london_breakout"


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def fmt_r(v: float, digits: int = 2) -> str:
    return "n/a" if v is None or v != v else f"{v:+.{digits}f}R".replace("-", "−")


def fmt_pct(v: float) -> str:
    return "n/a" if v is None or v != v else f"{v * 100:+.1f}%".replace("-", "−")


# --------------------------------------------------------------------------- data


def load(results: Path) -> dict:
    summary = json.loads((results / "summary.json").read_text())
    quality = json.loads((results / "data_quality.json").read_text())
    replay = (results / "replay_check.md").read_text() if (results / "replay_check.md").exists() else ""
    m = re.search(r"Trades: bot (\d+), backtest (\d+)", replay)
    replay_info = {"bot": int(m.group(1)), "backtest": int(m.group(2)),
                   "match": "MATCH" in replay} if m else None
    curve = []
    for period, suffix in (("holdout", "holdout"), ("development", "default")):
        f = results / f"trades_{BOT_STRATEGY}_{suffix}.csv"
        if f.exists() and f.stat().st_size > 0:
            t = pd.read_csv(f, parse_dates=["exit_time"])
            curve += [{"t": r.exit_time.strftime("%Y-%m-%d"), "r": float(r.r_multiple), "p": period}
                      for r in t.sort_values("exit_time").itertuples()]
    cum = 0.0
    for pt in curve:
        cum += pt.pop("r")
        pt["c"] = round(cum, 3)
    return {"summary": summary, "quality": quality, "replay": replay_info, "curve": curve}


def count_tests(root: Path) -> int | None:
    try:
        out = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q"], cwd=root,
                             capture_output=True, text=True, timeout=300).stdout
    except Exception:
        return None
    m = re.search(r"(\d+) tests? collected", out)
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------------- HTML pieces


def bar_rows(rows: list[dict], series: list[tuple[str, str, str]], domain: float, label_w: str) -> str:
    """Horizontal bars around a zero line, built in HTML so text stays readable on phones.

    rows: {"label": str, <key>: value, ...}; series: (key, css var, legend name).
    """
    out = [f'<div class="bars" style="--label-w:{label_w}">']
    for row in rows:
        out.append('<div class="bar-row">')
        out.append(f'<div class="bar-label">{esc(row["label"])}</div><div class="bar-track">')
        for key, color, name in series:
            v = row.get(key)
            if v is None or v != v:
                continue
            w = min(abs(v) / domain, 1.0) * 50
            side = "pos" if v >= 0 else "neg"
            tip = f'{name}: {fmt_r(v, 3)} per trade' if name else f'{fmt_r(v, 3)} per trade'
            out.append(
                f'<div class="bar-line"><span class="bar {side}" tabindex="0" '
                f'style="--w:{w:.2f}%;--c:var({color})" data-tip-title="{esc(row["label"])}" '
                f'data-tip="{esc(tip)}"></span>'
                f'<span class="bar-val {side}" style="--w:{w:.2f}%">{esc(fmt_r(v))}</span></div>')
        out.append("</div></div>")
    out.append('<div class="bar-row axis"><div></div><div class="bar-axis">'
               f'<span>{esc(fmt_r(-domain, 1))}</span><span>0</span><span>{esc(fmt_r(domain, 1))}</span>'
               "</div></div></div>")
    return "\n".join(out)


def table(head: list[str], rows: list[list[str]]) -> str:
    th = "".join(f"<th>{esc(h)}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="table-wrap"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


def tile(label: str, value: str, note: str = "") -> str:
    n = f'<div class="tile-note">{esc(note)}</div>' if note else ""
    return f'<div class="tile"><div class="tile-label">{esc(label)}</div><div class="tile-value">{esc(value)}</div>{n}</div>'


# --------------------------------------------------------------------------- page


def build(data: dict, repo: str, tests: int | None) -> str:
    s, q = data["summary"], data["quality"]
    blob = f"https://github.com/{repo}/blob/main"
    names = [n for n in NAMES if n in s] + [n for n in s if n not in NAMES]
    have_hold = all(s[n].get("holdout") for n in names)

    def dev(n):
        return s[n]["stats"]["Full (default)"]

    def hold(n):
        return (s[n].get("holdout") or {}).get("Holdout (default)") or {}

    profitable = sum(1 for n in names if (hold(n).get("expectancy_r") or -1) > 0)
    bot_dev, bot_hold = dev(BOT_STRATEGY), hold(BOT_STRATEGY)

    # chart 1: development vs holdout expectancy
    rows1 = [{"label": NAMES.get(n, n), "dev": dev(n)["expectancy_r"],
              "hold": hold(n).get("expectancy_r")} for n in names]
    dom1 = max(0.1, max(abs(v) for r in rows1 for v in (r["dev"], r["hold"]) if v is not None and v == v))
    dom1 = round(dom1 * 1.45 + 0.049, 1)
    chart1 = bar_rows(rows1, [("dev", "--series-1", "Oct 2024 – Nov 2026"),
                              ("hold", "--series-2", "Untouched holdout, Oct 2022 – Oct 2024")],
                      dom1, "minmax(9rem, 34%)")
    table1 = table(["Strategy", "Trades (2024–26)", "Exp. 2024–26", "Trades (holdout)", "Exp. holdout",
                    "Return holdout"],
                   [[NAMES.get(n, n), dev(n)["trades"], fmt_r(dev(n)["expectancy_r"], 3),
                     hold(n).get("trades", "n/a"), fmt_r(hold(n).get("expectancy_r"), 3),
                     fmt_pct(hold(n).get("total_return"))] for n in names])

    # chart 3: regime switch by setup
    rs = s.get("regime_switch", {})
    setups = [r for r in rs.get("by_tag", []) if r["tag"] in SETUPS]
    setups.sort(key=lambda r: list(SETUPS).index(r["tag"]))
    rows3 = [{"label": f'{SETUPS[r["tag"]]} · {r["trades"]} trades', "v": r["expectancy_r"]} for r in setups]
    chart3 = bar_rows(rows3, [("v", "--series-1", "")],
                      round(max([0.1] + [abs(r["v"]) for r in rows3]) * 1.45 + 0.049, 1),
                      "minmax(9rem, 40%)") if rows3 else ""
    table3 = table(["Setup", "Trades", "Win rate", "Exp. per trade"],
                   [[SETUPS[r["tag"]], r["trades"], f'{r["win_rate"] * 100:.0f}%', fmt_r(r["expectancy_r"], 3)]
                    for r in setups]) if setups else ""
    adoption = rs.get("adoption") or {}

    curve = data["curve"]
    hold_end = next((p["t"] for p in reversed(curve) if p["p"] == "holdout"), None)
    hold_total = next((p["c"] for p in reversed(curve) if p["p"] == "holdout"), 0.0)
    dev_total = (curve[-1]["c"] - hold_total) if curve else 0.0
    table2 = table(["Period", "Trades", "Total (R)", "Per trade"],
                   [["Untouched holdout", bot_hold.get("trades", "n/a"), fmt_r(hold_total, 1),
                     fmt_r(bot_hold.get("expectancy_r"), 3)],
                    ["Oct 2024 – Nov 2026", bot_dev["trades"], fmt_r(dev_total, 1),
                     fmt_r(bot_dev["expectancy_r"], 3)]]) if curve else ""

    rp = data["replay"]
    tiles = [
        tile("Strategies tested", str(len(names))),
        tile("Price history", "4 years", f'{q["rows_after_clean"]:,} fifteen-minute bars'),
        tile("Live bot vs backtest", f'{rp["bot"]}/{rp["backtest"]}' if rp else "n/a",
             "trades identical" if rp and rp["match"] else "replay check"),
    ]
    if tests:
        tiles.append(tile("Automated tests", str(tests)))

    page_data = json.dumps({"curve": curve, "holdEnd": hold_end}).replace("</", "<\\/")
    built = datetime.now(timezone.utc).strftime("%-d %b %Y")
    first, last = q["first_bar"][:10], q["last_bar"][:10]

    adoption_line = ""
    if adoption:
        failed = sum(1 for ok in adoption["checks"].values() if not ok)
        adoption_line = (f'<p>Result: <strong>{"adopted" if adoption["adopted"] else "not adopted"}</strong>'
                         f' ({len(adoption["checks"]) - failed} of {len(adoption["checks"])} pre-registered checks passed).'
                         " Range-day fades were the problem: price that faked out of the Asia range usually kept going.</p>")

    return (TEMPLATE
            .replace("{{TILES}}", "\n".join(tiles))
            .replace("{{HERO}}", f"{profitable} of {len(names)}" if have_hold else "n/a")
            .replace("{{CHART1}}", chart1).replace("{{TABLE1}}", table1)
            .replace("{{CHART3}}", chart3).replace("{{TABLE3}}", table3)
            .replace("{{TABLE2}}", table2).replace("{{ADOPTION}}", adoption_line)
            .replace("{{BOT_DEV_R}}", esc(fmt_r(bot_dev["expectancy_r"])))
            .replace("{{BOT_DEV_RET}}", esc(fmt_pct(bot_dev["total_return"])))
            .replace("{{BOT_HOLD_N}}", esc(bot_hold.get("trades", "n/a")))
            .replace("{{BOT_HOLD_R}}", esc(fmt_r(bot_hold.get("expectancy_r"))))
            .replace("{{BOT_HOLD_RET}}", esc(fmt_pct(bot_hold.get("total_return"))))
            .replace("{{FIRST}}", esc(first)).replace("{{LAST}}", esc(last))
            .replace("{{REPO}}", esc(f"https://github.com/{repo}")).replace("{{BLOB}}", esc(blob))
            .replace("{{BUILT}}", esc(built)).replace("{{DATA}}", page_data))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--results", type=Path, default=Path("results"))
    ap.add_argument("--out", type=Path, default=Path("docs/index.html"))
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "monanyx/goldlab"))
    ap.add_argument("--no-test-count", action="store_true")
    args = ap.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    tests = None if args.no_test_count else count_tests(root)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(build(load(args.results), args.repo, tests))
    print(f"wrote {args.out}")
    return 0


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>goldlab: testing gold trading strategies honestly</title>
<meta name="description" content="An automated research lab and live paper-trading bot for spot gold. Eight strategies, four years of data, rules fixed before the results. None survived unseen data.">
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #6f6d68;
  --grid: #e1e0d9; --axis: #c3c2b7; --ring: rgba(11,11,11,0.10); --wash: rgba(11,11,11,0.035);
  --series-1: #2a78d6; --series-2: #eb6834; --accent: #2a78d6;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #9a988f;
    --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10); --wash: rgba(255,255,255,0.04);
    --series-1: #3987e5; --series-2: #d95926; --accent: #6da7ec;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #9a988f;
  --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10); --wash: rgba(255,255,255,0.04);
  --series-1: #3987e5; --series-2: #d95926; --accent: #6da7ec;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink);
  font: 16px/1.6 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 860px; margin: 0 auto; padding: 3rem 1.25rem 4rem; }
a { color: var(--accent); }
h1 { font-size: clamp(1.9rem, 5vw, 2.8rem); line-height: 1.15; margin: .4rem 0 1rem; letter-spacing: -0.02em; }
h2 { font-size: 1.35rem; margin: 3.2rem 0 .4rem; letter-spacing: -0.01em; }
h3 { font-size: 1rem; margin: 0 0 .25rem; }
p { margin: .5rem 0; }
.eyebrow { color: var(--ink-2); font-size: .9rem; }
.lead { font-size: 1.12rem; color: var(--ink-2); max-width: 44rem; }
.sub { color: var(--ink-2); margin-top: 0; }
.card { background: var(--surface); border: 1px solid var(--ring); border-radius: 12px; padding: 1.25rem; margin-top: 1rem; }
.hero { display: flex; flex-wrap: wrap; gap: 1rem 2rem; align-items: center; }
.hero-num { font-size: 3.6rem; font-weight: 650; line-height: 1; }
.hero-text { max-width: 26rem; color: var(--ink-2); }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(10rem, 1fr)); gap: .75rem; margin-top: 1rem; }
.tile { background: var(--surface); border: 1px solid var(--ring); border-radius: 12px; padding: .9rem 1rem; }
.tile-label { font-size: .85rem; color: var(--ink-2); }
.tile-value { font-size: 1.6rem; font-weight: 600; }
.tile-note { font-size: .8rem; color: var(--muted); }
.steps { display: grid; grid-template-columns: repeat(auto-fit, minmax(12rem, 1fr)); gap: .75rem; counter-reset: step; margin-top: 1rem; }
.step { background: var(--surface); border: 1px solid var(--ring); border-radius: 12px; padding: 1rem; counter-increment: step; }
.step::before { content: counter(step); display: inline-grid; place-items: center; width: 1.6rem; height: 1.6rem;
  border-radius: 50%; background: var(--wash); border: 1px solid var(--ring); font-size: .85rem; font-weight: 600; margin-bottom: .5rem; }
.step p { font-size: .92rem; color: var(--ink-2); margin: 0; }
.legend { display: flex; flex-wrap: wrap; gap: .4rem 1.2rem; font-size: .85rem; color: var(--ink-2); margin: .25rem 0 .75rem; }
.legend i { display: inline-block; width: 12px; height: 12px; border-radius: 3px; margin-right: .4rem; vertical-align: -1px; }
.bars { display: grid; gap: .55rem; }
.bar-row { display: grid; grid-template-columns: var(--label-w) 1fr; gap: .75rem; align-items: center; }
.bar-label { font-size: .88rem; line-height: 1.25; }
.bar-track { position: relative; display: grid; gap: 2px; padding: 2px 0;
  background: linear-gradient(var(--axis), var(--axis)) 50% 0 / 1px 100% no-repeat; }
.bar-line { position: relative; height: 12px; }
.bar { position: absolute; top: 0; height: 12px; width: var(--w); background: var(--c); outline-offset: 2px; cursor: default; }
.bar.pos { left: 50%; border-radius: 0 4px 4px 0; }
.bar.neg { right: 50%; border-radius: 4px 0 0 4px; }
.bar:hover, .bar:focus { filter: brightness(1.15); outline: 2px solid var(--ring); }
.bar-val { position: absolute; top: -3px; font-size: .74rem; color: var(--ink-2); white-space: nowrap; font-variant-numeric: tabular-nums; }
.bar-val.pos { left: calc(50% + var(--w) + 6px); }
.bar-val.neg { right: calc(50% + var(--w) + 6px); }
.bar-row.axis { margin-top: .1rem; }
.bar-axis { display: flex; justify-content: space-between; font-size: .75rem; color: var(--muted); font-variant-numeric: tabular-nums; }
details { margin-top: .9rem; font-size: .9rem; }
summary { cursor: pointer; color: var(--ink-2); }
.table-wrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; margin-top: .5rem; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: .4rem .6rem; border-bottom: 1px solid var(--grid); white-space: nowrap; }
th { color: var(--ink-2); font-weight: 600; }
#curve { position: relative; margin-top: .5rem; }
#curve svg { display: block; width: 100%; overflow: visible; }
#curve text { fill: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }
#curve .band-label { fill: var(--ink-2); font-size: 12.5px; font-weight: 600; }
.tip { position: absolute; pointer-events: none; background: var(--surface); border: 1px solid var(--ring);
  border-radius: 8px; padding: .45rem .6rem; font-size: .82rem; box-shadow: 0 4px 16px rgba(0,0,0,.12);
  white-space: nowrap; opacity: 0; transition: opacity .1s; z-index: 5; }
.tip strong { display: block; font-size: .95rem; }
.tip span { color: var(--ink-2); }
ul.learned { padding-left: 1.2rem; }
ul.learned li { margin: .5rem 0; }
.chips { display: flex; flex-wrap: wrap; gap: .5rem; margin-top: .75rem; }
.chips span { border: 1px solid var(--ring); background: var(--surface); border-radius: 999px; padding: .2rem .7rem; font-size: .85rem; }
footer { margin-top: 3.5rem; padding-top: 1.25rem; border-top: 1px solid var(--grid); font-size: .85rem; color: var(--ink-2); }
footer a { margin-right: 1rem; }
@media (max-width: 560px) {
  main { padding-top: 2rem; }
  .bar-row { grid-template-columns: 1fr; gap: .2rem; }
  .bar-axis { margin-left: 0; }
}
</style>
</head>
<body>
<main>
<div class="eyebrow">Research project · spot gold (XAU/USD) · by monanyx</div>
<h1>I built a gold-trading bot, then tried hard to prove it wrong.</h1>
<p class="lead">goldlab is an automated research lab and a live paper-trading bot. Every strategy had to pass rules I wrote down before seeing any results, including a test on two years of price data that no earlier run had touched. None passed, and the lab caught that before any real money was at risk.</p>

<div class="card hero">
  <div class="hero-num">{{HERO}}</div>
  <div class="hero-text">strategies made money on the untouched holdout: two years of data (Oct 2022 – Oct 2024) used once, at the end, with no tuning.</div>
</div>
<div class="tiles">
{{TILES}}
</div>

<h2>How every strategy was tested</h2>
<p class="sub">Backtests are easy to fool. Each step below exists to catch a different way of fooling yourself.</p>
<div class="steps">
  <div class="step"><h3>Fix the rules first</h3><p>Hypotheses, parameter grids and the pass/fail rule were committed to git before any real-data run (<a href="{{BLOB}}/docs/improvement-tests.md">round 1</a>, <a href="{{BLOB}}/docs/regime-switch-test.md">round 2</a>).</p></div>
  <div class="step"><h3>Stress the recent data</h3><p>Oct 2024 – Nov 2026: in-sample vs out-of-sample, rolling walk-forward re-tuning, double trading costs, and every nearby parameter setting.</p></div>
  <div class="step"><h3>Untouched holdout</h3><p>Two older years no earlier run had used. Default settings, run once. No second chances.</p></div>
  <div class="step"><h3>Prove the bot = the backtest</h3><p>The live bot's exact code is replayed over real data (fresh process per run, saved state, late and skipped runs) and must produce identical trades.</p></div>
</div>

<h2>Recent results didn't hold up on older data</h2>
<p class="sub">Average profit per trade, in R (1R = the amount risked on one trade), after spread and slippage. Default settings.</p>
<div class="card">
  <div class="legend"><span><i style="background:var(--series-1)"></i>Oct 2024 – Nov 2026</span><span><i style="background:var(--series-2)"></i>Untouched holdout, Oct 2022 – Oct 2024</span></div>
  {{CHART1}}
  <details><summary>Show as table</summary>{{TABLE1}}</details>
</div>

<h2>The bot's own strategy: London breakout</h2>
<p class="sub">Cumulative result of every trade, in R. On 2024–26 it made {{BOT_DEV_R}} a trade ({{BOT_DEV_RET}}), which looked promising. On the untouched holdout the same rules lost: {{BOT_HOLD_N}} trades, {{BOT_HOLD_R}} a trade, {{BOT_HOLD_RET}}.</p>
<div class="card">
  <div id="curve" role="img" aria-label="Cumulative R of the London breakout across the holdout and the development window"><noscript>Chart needs JavaScript; see the table below.</noscript></div>
  <details><summary>Show as table</summary>{{TABLE2}}</details>
</div>

<h2>Round 2: a bot that reads the market first</h2>
<p class="sub">At the London open the regime switch labels the day (trend, range or chaos) from data known at that moment, then trades a breakout with the trend, fades a false break, or stays out.</p>
<div class="card">
  {{ADOPTION}}
  {{CHART3}}
  <details><summary>Show as table</summary>{{TABLE3}}</details>
</div>

<h2>The live bot</h2>
<div class="card">
  <p>goldbot runs on GitHub Actions throughout the London and New York sessions. It pulls live prices, runs the same engine as the backtests, sends Telegram alerts and paper-trades. Risk rules are enforced in code and can't be loosened by configuration:</p>
  <ul class="learned">
    <li>every order has a stop loss; 1% risk per trade, sized down, never up</li>
    <li>3% daily loss limit, at most 3 trades a day, no new trades after 20:00 Vienna time</li>
    <li>kill switch from Telegram (<code>/stop</code>), a GitHub variable or the config file</li>
    <li>a broker adapter that can only reach a demo account</li>
  </ul>
</div>

<h2>What I took from it</h2>
<ul class="learned">
  <li><strong>A good-looking backtest proves very little.</strong> London breakout looked fine on 2024–26 and lost on 2022–24. Only data you haven't seen can tell those apart.</li>
  <li><strong>Deciding the rules first is what makes a "no" trustworthy.</strong> Because the pass/fail rules were committed beforehand, the negative result can't be argued away, and the bot stays in paper mode.</li>
  <li><strong>The backtest and the live system have to be the same code.</strong> One engine runs both, and a replay test proves they agree trade for trade.</li>
</ul>

<h2>Built with</h2>
<div class="chips"><span>Python</span><span>pandas</span><span>NumPy</span><span>Matplotlib</span><span>pytest</span><span>GitHub Actions</span><span>Docker</span><span>Telegram Bot API</span><span>Twelve Data API</span><span>Capital.com REST API (demo)</span></div>

<footer>
  <p><a href="{{REPO}}">Source on GitHub</a><a href="{{BLOB}}/REPORT.md">Full research report</a><a href="{{BLOB}}/results/replay_check.md">Replay check</a></p>
  <p>Data: Twelve Data XAU/USD 15-minute bars, {{FIRST}} to {{LAST}}. Page generated from the research results on {{BUILT}}. Not financial advice: no strategy here has a proven edge.</p>
</footer>
</main>
<div class="tip" id="tip" role="tooltip"><strong></strong><span></span></div>
<script type="application/json" id="page-data">{{DATA}}</script>
<script>
(function () {
  "use strict";
  var data = JSON.parse(document.getElementById("page-data").textContent);
  var tip = document.getElementById("tip");
  var tipTitle = tip.querySelector("strong"), tipText = tip.querySelector("span");
  function showTip(value, label, x, y) {
    tipTitle.textContent = value; tipText.textContent = label;
    tip.style.opacity = "1";
    var w = tip.offsetWidth, h = tip.offsetHeight;
    var left = Math.min(Math.max(8, x + 14), window.innerWidth - w - 8);
    var top = y - h - 12 < window.scrollY + 8 ? y + 16 : y - h - 12;
    tip.style.left = left + "px"; tip.style.top = top + "px";
  }
  function hideTip() { tip.style.opacity = "0"; }

  // per-bar tooltips (hover and keyboard focus)
  document.querySelectorAll(".bar").forEach(function (el) {
    function show(ev) {
      var r = el.getBoundingClientRect();
      var x = ev && ev.pageX !== undefined && ev.type !== "focus" ? ev.pageX : r.left + window.scrollX + r.width / 2;
      var y = ev && ev.pageY !== undefined && ev.type !== "focus" ? ev.pageY : r.top + window.scrollY;
      showTip(el.dataset.tip, el.dataset.tipTitle, x, y);
    }
    el.addEventListener("pointermove", show);
    el.addEventListener("focus", show);
    el.addEventListener("pointerleave", hideTip);
    el.addEventListener("blur", hideTip);
  });

  // cumulative-R line with a crosshair
  var box = document.getElementById("curve");
  var pts = data.curve;
  if (!pts.length) return;
  var NS = "http://www.w3.org/2000/svg";
  function el(name, attrs, parent) {
    var e = document.createElementNS(NS, name);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }
  function fmt(v) { return (v >= 0 ? "+" : "−") + Math.abs(v).toFixed(1) + "R"; }
  var times = pts.map(function (p) { return Date.parse(p.t); });
  var vals = pts.map(function (p) { return p.c; });
  var holdEnd = data.holdEnd ? Date.parse(data.holdEnd) : null;
  var holdTotal = 0;
  pts.forEach(function (p) { if (p.p === "holdout") holdTotal = p.c; });

  function draw() {
    box.querySelectorAll("svg").forEach(function (s) { s.remove(); });
    var W = box.clientWidth, narrow = W < 560, H = Math.max(240, Math.min(340, W * 0.5));
    var m = { l: 46, r: 58, t: narrow ? 48 : 30, b: 28 };
    var t0 = times[0], t1 = times[times.length - 1];
    var lo = Math.min(0, Math.min.apply(null, vals)), hi = Math.max(0, Math.max.apply(null, vals));
    var step = [1, 2, 5, 10, 20, 25, 50].find(function (s) { return (hi - lo) / s <= 6; }) || 50;
    lo = Math.floor(lo / step) * step; hi = Math.ceil(hi / step) * step;
    function X(t) { return m.l + (t - t0) / (t1 - t0) * (W - m.l - m.r); }
    function Y(v) { return m.t + (hi - v) / (hi - lo) * (H - m.t - m.b); }
    var svg = el("svg", { viewBox: "0 0 " + W + " " + H, height: H, "aria-hidden": "true" }, box);
    if (holdEnd) {
      el("rect", { x: m.l, y: m.t, width: X(holdEnd) - m.l, height: H - m.t - m.b, fill: "var(--wash)" }, svg);
      // on narrow screens the two period labels go on separate rows so they never collide
      var a = el("text", { x: m.l + 6, y: m.t - (narrow ? 28 : 10), "class": "band-label" }, svg);
      a.textContent = (narrow ? "Holdout: " : "Untouched holdout: ") + fmt(holdTotal);
      var b = el("text", { x: W - m.r, y: m.t - 10, "class": "band-label", "text-anchor": "end" }, svg);
      b.textContent = "2024–26: " + fmt(vals[vals.length - 1] - holdTotal);
    }
    for (var v = lo; v <= hi + 1e-9; v += step) {
      el("line", { x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v), stroke: v === 0 ? "var(--axis)" : "var(--grid)", "stroke-width": 1 }, svg);
      var tx = el("text", { x: m.l - 8, y: Y(v) + 4, "text-anchor": "end" }, svg);
      tx.textContent = v === 0 ? "0" : (v > 0 ? "+" : "−") + Math.abs(v);
    }
    var y0 = new Date(t0).getUTCFullYear() + 1, y1 = new Date(t1).getUTCFullYear();
    for (var y = y0; y <= y1; y++) {
      var ty = Date.UTC(y, 0, 1), xx = X(ty);
      if (xx < m.l + 20 || xx > W - m.r - 10) continue;
      var yt = el("text", { x: xx, y: H - 8, "text-anchor": "middle" }, svg);
      yt.textContent = String(y);
    }
    var d = pts.map(function (p, i) { return (i ? "L" : "M") + X(times[i]).toFixed(1) + " " + Y(p.c).toFixed(1); }).join("");
    el("path", { d: d, fill: "none", stroke: "var(--series-1)", "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
    var last = pts.length - 1;
    el("circle", { cx: X(times[last]), cy: Y(vals[last]), r: 4, fill: "var(--series-1)", stroke: "var(--surface)", "stroke-width": 2 }, svg);
    var end = el("text", { x: X(times[last]) + 8, y: Y(vals[last]) + 4 }, svg);
    end.textContent = fmt(vals[last]);
    var cross = el("line", { y1: m.t, y2: H - m.b, stroke: "var(--axis)", "stroke-width": 1, opacity: 0 }, svg);
    var dot = el("circle", { r: 4, fill: "var(--series-1)", stroke: "var(--surface)", "stroke-width": 2, opacity: 0 }, svg);
    var hit = el("rect", { x: m.l, y: 0, width: W - m.l - m.r, height: H, fill: "transparent" }, svg);
    hit.addEventListener("pointermove", function (ev) {
      var r = svg.getBoundingClientRect();
      var t = t0 + (ev.clientX - r.left - m.l) / (W - m.l - m.r) * (t1 - t0);
      var i = 0, best = Infinity;
      for (var k = 0; k < times.length; k++) { var dd = Math.abs(times[k] - t); if (dd < best) { best = dd; i = k; } }
      var cx = X(times[i]), cy = Y(vals[i]);
      cross.setAttribute("x1", cx); cross.setAttribute("x2", cx); cross.setAttribute("opacity", 1);
      dot.setAttribute("cx", cx); dot.setAttribute("cy", cy); dot.setAttribute("opacity", 1);
      showTip(fmt(vals[i]) + " cumulative",
              pts[i].t + (pts[i].p === "holdout" ? " · holdout" : " · 2024–26"),
              r.left + window.scrollX + cx, r.top + window.scrollY + cy);
    });
    hit.addEventListener("pointerleave", function () { cross.setAttribute("opacity", 0); dot.setAttribute("opacity", 0); hideTip(); });
  }
  draw();
  var timer;
  window.addEventListener("resize", function () { clearTimeout(timer); timer = setTimeout(draw, 120); });
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    raise SystemExit(main())
