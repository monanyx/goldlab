# goldlab

A backtesting lab for intraday XAUUSD (spot gold), for research only. It has no
broker connection and no live trading code.

It tests three intraday ideas with realistic, configurable costs and fixed
risk rules. It then checks whether any apparent edge survives out-of-sample
data, walk-forward re-optimisation and small parameter changes. It writes
`REPORT.md` with a verdict for each strategy, plus PNG charts.

## Quick start

```bash
pip install -e ".[test]"          # numpy, pandas, matplotlib, pytest

# 1. Put >= 2 years of XAUUSD M5/M15 bars in data/  (format: data/README.md)
python -m goldlab check-data      # what the loader sees: rows, gaps, sessions

# 2. Full research run -> REPORT.md + results/*.png|csv|json
python -m goldlab run             # add --minutes 15 to resample M5 -> M15

# Tests (engine, data cleaning, sessions, strategy causality, metrics)
python -m pytest
```

To check that the whole pipeline works without real data, use
`python -m goldlab smoke`. It runs everything on a synthetic random walk and
writes `smoke_results/REPORT_SYNTHETIC.md`. Those numbers mean nothing about
gold.

### Getting data

- **Dukascopy** (free): `python -m goldlab fetch-dukascopy --start 2023-01-01 --end 2025-01-01`
  downloads 1-minute BID candles and writes an M5 CSV to `data/`. It is slow
  (one request per day). It was written against Dukascopy's public datafeed
  layout, but it has not been run against the live server: the environment it
  was built in cannot reach Dukascopy. Run `check-data` on the result and look
  at it before you trust it.
- Alternatively export M5 bars from Dukascopy's web UI, MetaTrader 5 or
  HistData. All three formats are auto-detected (see `data/README.md`).

### Common options

```
--spread 0.30 --slippage 0.05        # USD/oz; defaults 0.35 and 0.05
--swap-long -0.6 --swap-short -0.2   # USD/oz per rollover
--risk-pct 0.01 --daily-loss 0.03 --max-trades 3
--is-fraction 0.7                    # 70% in-sample / 30% out-of-sample
--train-months 12 --test-months 3    # walk-forward windows
--workers 4                          # parallel parameter-grid runs
```

## What's inside

```
goldlab/
  config.py      CostConfig (spread, session multipliers, slippage, commission, swap), RiskConfig
  data.py        CSV loading (canonical / Dukascopy / MT5 / HistData), UTC conversion, cleaning
  sessions.py    Asia / London / New York labels (DST-aware), trading day = 17:00 NY rollover
  engine.py      bar-by-bar backtester with conservative fill rules and risk rules
  features.py    look-ahead-free daily ATR, bar ATR, local-time helpers
  strategies/    london_breakout.py, ny_momentum.py, vwap_fade.py
  validation.py  IS/OOS split, walk-forward, parameter grid + sensitivity
  metrics.py     win rate, PF, expectancy (R), t-stat, max DD, losing streak, monthly returns
  plots.py       equity + drawdown, sensitivity heatmaps
  pipeline.py    end-to-end run and REPORT.md writer
  dukascopy.py   optional downloader
tests/           pytest suite
```

### Sessions (stored in UTC, defined in local time so DST is handled)

| Session | Definition |
|---|---|
| Asia | 00:00-07:00 UTC |
| London | 08:00-16:30 Europe/London |
| New York | 08:00-17:00 America/New_York |

Each bar gets `is_asia / is_london / is_ny` and a `session` label. The overlap
is labelled `London+NewYork` and anything else is `Off`. Each bar also gets a
`trade_date`, which rolls at 17:00 New York. Weekend bars (Fri 17:00 NY to
Sun 17:00 NY) are dropped. Gaps are flagged and reported but never filled with
invented prices.

### Engine rules

- Prices are treated as mid. Every fill pays half the spread, so a round trip
  pays one full spread. The spread is multiplied by 1.5 in Asia and by 2
  outside the main sessions.
- Market and stop fills pay slippage. Limit (target) fills don't.
- An order created on bar *i* can only fill on bar *i+1* or later, so the
  engine can't see the future.
- If the stop and the target are both inside one bar, the stop is assumed to
  hit first. If price gaps through a stop, the fill is at the open.
- On the bar where a stop-entry order triggers, the target only counts if
  that bar also closes beyond it.
- Every order must carry a hard stop (the `Order` class refuses to be built
  without one).
- Position size: 1% of the equity at the start of the trading day is lost at
  the stop, including costs. A clean stop-out is therefore exactly -1R.
- Daily loss limit: 3%. Each trade's risk is also capped at whatever is left
  of the day's allowance. Only price gaps can push a day past the limit.
- At most 3 trades per day and one position at a time.
- Each strategy is forced flat before its session ends, and every position is
  closed before a weekend gap.
- Swap is charged per rollover a position is held through, triple on
  Wednesdays. With the default strategies it should never trigger.

### Strategies

1. **London breakout.** Takes the Asia range from 00:00 UTC to the London
   open. Places OCO stop orders a small buffer beyond the range, valid for the
   first N hours of London. Stop distance is a fraction of the range width.
   Target is a multiple of the stop (R). Flat at 16:00 London time.
2. **New York open momentum.** Measures the move over the first 30 minutes
   after 08:30 New York time. If the move is larger than k × daily ATR, it
   enters in the same direction. ATR-based stop, R-multiple target, flat at
   16:00 New York time.
3. **VWAP fade (mean reversion).** Uses a tick-volume VWAP anchored at the
   London open. When price is stretched more than k × daily ATR from VWAP and
   starts turning back, it fades the move with VWAP as the target and an
   ATR-based stop.

The default parameters were fixed before any data was seen. Each strategy also
has a parameter grid, which is used for optimisation and the sensitivity check.

### Validation

- **IS/OOS:** the first 70% of trading days is in-sample and the last 30% is
  out-of-sample. The report shows default parameters and IS-optimised
  parameters on both halves.
- **Walk-forward:** re-optimise on 12 months, trade the next 3 months, roll
  forward. Only the stitched test segments count.
- **Sensitivity:** the whole grid is run. The report shows what share of
  parameter sets are profitable, a one-parameter-at-a-time table and a heatmap.
- **Cost stress:** the OOS period is re-run at 2× spread and slippage.
- **Verdict rules** are fixed in `pipeline.py`. A strategy only earns "EDGE"
  if all of these hold:
  - walk-forward expectancy > 0 with t-stat >= 2 over at least 30 trades
  - default-parameter OOS expectancy > 0
  - still positive at 2× costs
  - at least 60% of the grid is positive

## Not included (on purpose)

There is no broker API, no order routing and no live or paper-trading loop.
