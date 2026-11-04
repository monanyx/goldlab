# London breakout improvement tests (pre-registered 2026-11-04)

Written and committed **before** running any of these variants on real data.
After 5 losing-ish paper trades, it is tempting to tweak the strategy until the
recent past looks good. That only fits noise. Instead, these four ideas and the
rule for adopting them are fixed in advance, and each is tested with the same
research lab: in-sample/out-of-sample split, walk-forward, double costs and
parameter grid.

## Baseline

`london_breakout` with default parameters. Its result on 2 years of real 15-minute
XAU/USD data was **inconclusive**: about +0.04R a trade, with all of the profit in 2026.

## The four variants

Each variant changes exactly one thing. Everything else stays at the baseline
defaults.

| Name | Change | Why it might help |
|---|---|---|
| `lb_trend` | Only trade in the direction of the trend. Long only if yesterday's close is above the 20-day average of daily closes; short only if it is below. | Breakouts against a strong trend tend to fail. Gold trended hard in 2025–26. |
| `lb_confirm` | Enter only after a 15-minute bar **closes** beyond the range. The baseline enters as soon as price touches the range edge. Entry is at the next bar's open. | Filters out quick fake-outs that poke through the range and reverse. |
| `lb_early_exit` | Close everything at 13:00 London time instead of 16:00. | 75% of baseline trades end at the time stop. Exiting before the US data releases (13:30 London) avoids the most violent reversals. |
| `lb_tight_range` | Only trade when the Asia range is at most 0.5× daily ATR. The baseline allows up to 0.8×. | A quiet, compressed Asia session is the classic setup for a London expansion. Wide ranges also force wide stops. |

## Adoption rule (fixed now)

A variant is adopted only if **both** of these hold:

1. It earns **EDGE AFTER COSTS** under the lab's existing pre-registered rules:
   - positive walk-forward expectancy with t-stat ≥ 2 over at least 30 trades
   - positive default-parameter out-of-sample expectancy
   - still positive at 2× spread and slippage
   - at least 60% of its parameter grid profitable
2. Its walk-forward expectancy is higher than the baseline's.

Testing 4 ideas raises the chance that one passes by luck. So even a variant
that passes is paper-traded for at least 2 months before any demo-account trading.
If none passes, the honest conclusion is that these simple fixes don't create an
edge, and the bot stays as it is.

Combining variants (for example trend + confirmation) is **not** part of this
test. Choosing a combination after seeing which single ideas did well would be
data snooping. It can be a new, separately registered test later.
