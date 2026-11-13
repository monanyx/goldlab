# Regime switch test (pre-registered 2026-11-13)

Written and committed **before** the regime switch has been run on any real data.
The rules, the parameter grid, the adoption rule and the stopping rule below are
fixed now and will not be changed after the results exist.

## The idea

Every morning at the London open, the bot reads the chart, decides what kind of
day it is, and uses the strategy that fits that kind of day.

Everything it looks at is known at the London open (the close of the last Asia
bar), so a backtest and a live run make the same decision:

| Input | Meaning |
|---|---|
| ATR | Average daily range over the previous 14 trading days (the lab's existing `daily_atr`) |
| Trend distance | Yesterday's close minus the average of the last 20 daily closes, in ATRs |
| Yesterday's range | Yesterday's high minus low, in ATRs |
| Asia range | High minus low from 00:00 UTC to the London open, in ATRs |

## Day types (checked in this order)

1. **Chaos day: no trade.** Yesterday's range was at least 2.0 ATR (a shock day),
   or the Asia range is outside 0.15–0.8 ATR (the same limits as the baseline).
2. **Trend day: breakout with the trend.** The trend distance is at least 1.0 ATR
   in either direction. Only the side of the trend is traded: a buy-stop above the
   Asia high when price is above its 20-day average, a sell-stop below the Asia low
   when below. Levels, stop (1.0 × range + buffer), target (1.5R) and the 3-hour
   entry window are the baseline London breakout's.
3. **Range day: fade a false break.** Otherwise (trend distance under 1.0 ATR).
   During the first 3 hours of London, price trades more than the buffer
   (0.05 ATR) beyond one edge of the Asia range, then a 15-minute bar **closes back
   inside** the range. Enter at the next bar's open in the opposite direction.
   - Stop: 0.05 ATR beyond the furthest point of the false break.
   - Target: the opposite edge of the Asia range.
   - Skip the trade if the target is less than 1.0 × the stop distance away.
   - At most one fade per day; the first false break that closes back inside decides.

Every position is closed by 16:00 London. All hard risk rules stay exactly as they
are (1% risk, hard stop on every trade, 3% daily loss limit, max 3 trades a day,
no new trades after 20:00 Vienna, kill switch).

## Parameters

Defaults (the values above) are fixed now. The grid used for walk-forward
re-optimisation and the robustness check has 81 combinations:

| Parameter | Values | Default |
|---|---|---|
| `entry_hours` (entry window after the London open) | 2, 3, 4 | 3 |
| `fade_min_rr` (minimum target ÷ stop for a fade) | 0.5, 1.0, 1.5 | 1.0 |
| `target_r` (breakout target in R) | 1.0, 1.5, 2.0 | 1.5 |
| `trend_atr` (trend distance that makes a trend day) | 0.5, 1.0, 1.5 | 1.0 |

`trend_days = 20`, `chaos_atr = 2.0`, `buffer_atr = 0.05`, `stop_frac = 1.0`,
`min_range_atr = 0.15`, `max_range_atr = 0.8` and `flat_hour = 16` are not tuned.

## Why an untouched holdout is required

These rules were chosen **after** seeing the round-1 results on Oct 2024 – Nov 2026.
The trend filter (`lb_trend`) was the most robust round-1 variant, and entering
only after a close beyond the range (`lb_confirm`) lost money, which hints that
many breakouts fail. That is where the fade on range days comes from. So a good
result on that same window could be partly fitted to it.

The decisive test is therefore on data the lab has **never used**: every bar
before **2024-10-21 00:00 UTC**, the first bar of the round-1 data. The research
run downloads 4 years of history and splits it once:

- **Development window:** 2024-10-21 → latest bar. Analysed exactly like round 1
  (in-sample/out-of-sample split, walk-forward, double costs, parameter grid).
- **Holdout:** everything before 2024-10-21. The regime switch runs there once,
  with its default parameters and no re-tuning.

## Adoption rule (fixed now)

The regime switch is adopted only if **all three** hold:

1. **Development window:** it earns **EDGE AFTER COSTS** under the lab's existing
   rules: positive walk-forward expectancy with t-stat ≥ 2 over at least 30
   trades, positive default-parameter out-of-sample expectancy, still positive
   at 2× spread and slippage, and at least 60% of the parameter grid profitable.
2. **Better than today's bot:** its walk-forward expectancy is higher than the
   baseline London breakout's.
3. **Holdout:** at least 12 months of holdout data and at least 30 trades,
   expectancy > 0 with t-stat ≥ 1.5, and still positive at 2× spread and slippage.

If the holdout data does not reach back 12 months (Twelve Data's history may be
limited), the test is **not finished**: rerun with `source = dukascopy`. No
adoption without the holdout.

The regime switch's verdict in `results/summary.json` only says `EDGE` when all
three hold, so the bot's `strategy = "auto"` can never switch to it on the
development window alone.

## Stopping rule (fixed now)

- **Adopted:** the bot switches to it automatically, still in **paper mode**, for
  at least 2 months before any demo-account trading. No real money as part of
  this project.
- **Not adopted:** the strategy search ends here. No round 3 of tweaks to rescue
  it. The bot stays on the London breakout in paper mode as a live experiment.

## Reported for information only

These do not change any verdict:

- Every round-1 strategy, run once on the holdout with default parameters. This is
  the first look at how the bot's current strategy behaves on data it has never
  seen. Round-1 verdicts keep their round-1 rules.
- The regime switch's trades split by day type (trend breakout vs range fade).

---

## Result (added 2026-11-13, after the run; nothing above was changed)

Research run 43377753365, 4 years of Twelve Data XAU/USD 15-minute bars
(2022-10-30 → 2026-11-13). Full numbers in [REPORT.md](../REPORT.md).

**Not adopted.** It failed the development window and the holdout:

| Check | Result |
|---|---|
| Development window verdict | NO EDGE: −0.245R a trade on default settings, 0% of the 81 grid settings profitable |
| Walk-forward vs baseline | −0.026R vs London breakout's +0.054R |
| Holdout (23.7 months, 131 trades) | −0.092R a trade, t-stat −1.03, −0.149R at 2× costs |

By day type (development window, default settings): range-day fades lost
heavily (46 trades, 16–19% win rate, about −0.5R a trade); trend-day
breakouts were roughly break-even (51 trades, −0.04R long, +0.06R short).

Per the stopping rule, the strategy search ends here. The bot stays on the
London breakout in paper mode. Information only: the London breakout itself
lost on the holdout (272 trades, −0.10R a trade, −24.9%), as did every other
strategy.
