# goldlab + goldbot

This repo has two parts:

- **goldbot** is an XAUUSD (spot gold) trading bot. It ships in **signal mode**
  (Telegram alerts only). It can also run in **paper mode** (virtual trades
  with P&L tracking) and in **live mode**, which places orders on a
  **Capital.com demo account only**.
- **goldlab** is the research lab: backtests, out-of-sample tests, walk-forward
  tests and the `REPORT.md` verdict. The bot runs the lab's engine, so both
  make the same decisions.

> **The strategy is not proven.** `REPORT.md` has not been produced yet
> because no real price history has been added to `data/` (see
> [Research lab](#research-lab-goldlab)). Until a report shows a strategy with
> an edge after costs, the bot uses the London breakout as a fallback, and its
> signals have no demonstrated edge. Use signal or paper mode only.

---

## Setting up goldbot (signal and paper mode on GitHub Actions)

You need a GitHub account, a Telegram account and about 20 minutes.

1. **Install locally (optional, for testing).** With Python 3.11 or newer:
   ```bash
   git clone <this repo> && cd goldlab
   pip install -e ".[test]"
   python -m pytest -q          # should end with "passed"
   ```

2. **Get a free Twelve Data API key** (live XAU/USD prices).
   1. Sign up at <https://twelvedata.com> and open **API Keys** in your
      dashboard.
   2. Copy the key. The free plan allows 800 requests a day and 8 a minute.
      The bot uses one request per run, so 64 runs a day is well within it.

3. **Create your Telegram bot with BotFather.**
   1. In Telegram, search for **@BotFather** (it has a blue check mark) and
      open the chat.
   2. Send `/newbot`.
   3. Send a display name, for example `My Gold Signals`.
   4. Send a username. It must end in `bot`, for example `my_gold_signals_bot`.
   5. BotFather replies with a **token** that looks like
      `123456789:AAH...`. This is your `TELEGRAM_BOT_TOKEN`. Keep it secret:
      anyone with the token controls the bot.
   6. Optional: send `/setcommands`, choose your bot, and paste:
      ```
      stop - Halt everything (kill switch)
      resume - Resume after /stop
      status - Show bot status
      ```

4. **Get your chat ID.**
   1. Open your new bot in Telegram (search for its username) and press
      **Start**, or send it any message.
   2. Then do one of these:
      - Locally: `TELEGRAM_BOT_TOKEN=<token> python -m goldbot telegram-setup`
        prints `chat id: 123456789`.
      - Or open `https://api.telegram.org/bot<token>/getUpdates` in a browser
        and find `"chat":{"id":123456789,...}`.
   3. That number is your `TELEGRAM_CHAT_ID`. Group chat IDs start with `-`.
   4. Test it:
      `TELEGRAM_BOT_TOKEN=<token> TELEGRAM_CHAT_ID=<id> python -m goldbot telegram-setup --test`.
      You should receive "goldbot test message ✅".

5. **Add the secrets to GitHub.** In your repository, go to **Settings →
   Secrets and variables → Actions → Secrets → New repository secret** and add:

   | Secret | Value |
   |---|---|
   | `TWELVEDATA_API_KEY` | key from step 2 |
   | `TELEGRAM_BOT_TOKEN` | token from step 3 |
   | `TELEGRAM_CHAT_ID` | ID from step 4 |
   | `ALPHAVANTAGE_API_KEY` | optional backup feed (its intraday FX data is a paid feature) |

6. **Choose the mode (optional).** On the same page, open the **Variables**
   tab and add:
   - `GOLDBOT_MODE`: `signal` (default) or `paper`
   - `GOLDBOT_ACCOUNT_BALANCE`: e.g. `10000`, the balance the 1% position
     size is based on
   - `GOLDBOT_KILL_SWITCH`: `true` halts the bot without a code change

   Everything else is set in `goldbot.toml`.

7. **Allow the workflow to save its state.** Go to **Settings → Actions →
   General → Workflow permissions**, select **Read and write permissions**
   and click Save. The bot stores its state and paper-trade log on a branch
   called `goldbot-state`.

8. **Merge this branch into `main`.** GitHub only runs scheduled workflows
   from the default branch.

9. **Start it.** Go to **Actions → goldbot → Run workflow**. You should get a
   "goldbot started in SIGNAL mode" message in Telegram. After that it runs
   every 15 minutes, Monday to Friday, 06:00–22:00 UTC, which covers the
   London and New York sessions.

10. **Read the results.**
    - Alerts and the daily summary (22:30 Vienna time) arrive in Telegram.
    - The `goldbot-state` branch holds `paper_trades.csv`, `signals.csv` and
      `bot_state.json`. Each run also uploads them as a workflow artifact.

**Cost note:** GitHub Actions is free for public repositories. A private
repository gets 2,000 free minutes a month, and this schedule uses roughly
1,400 of them.

### Stopping it (kill switch)

There are three ways to stop the bot. Any one of them halts everything: pending
orders are cancelled, open positions are closed and no new trades are taken.

- Send **`/stop`** to your bot in Telegram. Send **`/resume`** to continue.
  On GitHub Actions the command takes effect at the next run, so within
  about 15 minutes. On a VPS it takes about 1 minute.
- Set the GitHub variable `GOLDBOT_KILL_SWITCH` to `true`.
- Set `kill_switch = true` in `goldbot.toml`.

`/stop` and `/resume` are only accepted from your `TELEGRAM_CHAT_ID`. Messages
from anyone else are ignored.

---

## How the bot works

On each run the bot:

1. Reads Telegram commands.
2. Fetches the last 3,000 completed 15-minute bars from Twelve Data.
3. Runs the strategy over them.
4. Feeds every bar it hasn't processed yet into the same engine the backtester
   uses, applying the risk rules.
5. Sends alerts, records paper trades, or places demo orders, depending on the
   mode.
6. Saves its state.

If a run starts late or is skipped, the next run processes all the missed bars
in order, and any alert produced by a late run is marked **LATE**.

| Mode | What happens |
|---|---|
| `signal` (default) | Telegram alert for every signal. Nothing is traded. The risk rules still run on hypothetical results, so after 3 losing signals in a day the alerts stop. |
| `paper` | Alerts, plus every signal becomes a virtual trade with spread and slippage. Fills, P&L and equity are logged to `state/paper_trades.csv` and reported in the daily summary. |
| `live` | Alerts, plus orders on the **Capital.com demo** account. Needs `live_trading_enabled = true` in `goldbot.toml` **and** `GOLDBOT_LIVE_CONFIRM=I_UNDERSTAND_THIS_PLACES_ORDERS_ON_DEMO` in the environment. It is refused on GitHub Actions; run it on your own VPS. |

**Every signal contains:**
- direction and order type (e.g. a buy-stop at a price)
- entry price, stop loss and take profit
- position size in oz and lots, with the dollar risk (1% of the configured
  balance, including spread)
- the time the order expires
- a one-line reason

London-breakout signals come as a one-cancels-other pair: when one side
triggers, the other is cancelled.

### Hard risk rules (enforced in code)

| Rule | Where it's enforced |
|---|---|
| Every trade has a stop loss | `Order` can't be created without one, `BrokerOrder` refuses a missing or wrong-side stop, and the broker adapter always sends `stopLevel` |
| 1% risk per trade | Size = 1% of the day's starting balance divided by the stop distance (including spread and slippage), rounded **down** |
| Daily loss limit 3% | The engine blocks new entries once losses reach 3%, and each trade's risk is capped at what's left of the 3%. In live mode the broker's equity is also checked: at -3% everything is closed for the day |
| Max 3 trades per day | Engine counter per trading day. In live mode, each order sent to the broker (or one-cancels-other pair) counts as a trade |
| No new trades after 20:00 Europe/Vienna | Engine cut-off; set with `no_new_trades_after` and `timezone` |
| Close before session end | The strategy's flat time, plus closing before the weekend |
| Kill switch | `kill_switch` config flag, `GOLDBOT_KILL_SWITCH` variable or Telegram `/stop` |

These limits can't be loosened by config. The config loader rejects a risk
above 2% per trade, a daily loss limit above 3% and more than 3 trades a day.

**Position-size rounding:** gold trades in steps of 1 oz (0.01 lot). On a
$10,000 balance this rounding means signals often risk 0.8–0.95% instead of
exactly 1%. The bot always rounds down, never up.

### Proof that the live code matches the backtest

`python -m goldbot replay --data data/` runs the **exact live code path** over
historical bars:
- a fresh bot process for every simulated cron run
- state saved to disk and reloaded between runs
- a feed that only shows bars completed by the simulated clock
- a rolling 3,000-bar history window
- random late and skipped runs

It then compares the resulting paper trades with `Backtester.run()` on the
same bars and writes `results/replay_check.md`. The test suite runs the same
check for all three strategies (`tests/test_replay_equivalence.py`).

Results so far, on synthetic data only (no real data is in the repo):
`replay --synthetic` gave 1,469 runs and 26 trades, an identical match. Run it
on real data once you've added a CSV to `data/`.

### Strategies are pluggable

`strategy = "auto"` picks the best strategy from the research report
(`results/summary.json`, written alongside `REPORT.md`), but only one whose
verdict is `EDGE`. Otherwise it falls back to `london_breakout`.

You can also name a built-in strategy (`london_breakout`, `ny_momentum`,
`vwap_fade`) or any class with `signals(df) -> StrategySignals`, for example
`strategy = "mypkg.mystrat:MyStrategy"`. Parameters go in
`strategy_params = { ... }`.

### Commands

```
python -m goldbot run              # one cycle (what GitHub Actions runs)
python -m goldbot loop             # runs forever, one cycle per minute (VPS/Docker)
python -m goldbot status           # config (secrets redacted) + state
python -m goldbot replay --data data/   # live path vs backtest on your CSV
python -m goldbot telegram-setup [--test]
python -m goldbot broker-check     # Capital.com DEMO login, balance, size rules (read-only)
```

All secrets come from environment variables: `TWELVEDATA_API_KEY`,
`ALPHAVANTAGE_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
`CAPITAL_API_KEY`, `CAPITAL_IDENTIFIER` and `CAPITAL_API_PASSWORD`. The config
loader refuses secrets placed in `goldbot.toml`, and `.gitignore` excludes
`*.env` files and `state/`.

---

## Live mode later: Capital.com demo on a VPS

### Why Capital.com

Trading 212's API doesn't cover CFD accounts. Of the brokers with an official
API that offer gold CFDs, Capital.com was the easiest to use from Python:

- a plain REST/JSON API with a free demo environment
  (`demo-api-capital.backend-capital.com`)
- login with an API key plus your login and an API-key password, which returns
  two session tokens
- gold is a single market, `GOLD`, sized in ounces
- stop loss and take profit are fields on the order itself

IG also has a mature REST API with a demo, but needs more handling: separate
demo credentials, region-specific market codes for gold, and contract-size and
currency details. The adapter is in `goldbot/brokers/capital.py`. **Its base
URL is hard-coded to the demo server**, with no setting to change it.

> **Not yet tested against the real demo server.** The adapter was built from
> Capital.com's public API reference and an open-source client, because the
> build environment couldn't reach Capital.com. Every request is
> unit-tested against a mocked server. Run `broker-check` before anything else.

### Steps

1. **Create a Capital.com account and switch to the demo account.**
   Enable two-factor authentication. Then go to **Settings → API
   integrations → Generate new key**, set a custom API-key password, and copy
   the key. Follow Capital.com's current instructions for using a key with
   the demo account.
2. **Get a small VPS with Docker.** Any 1 vCPU / 1 GB machine is enough.
   Clone this repo there.
3. **Create the env file.** Run `cp goldbot.env.example goldbot.env` and fill
   in:
   - `TWELVEDATA_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
   - `CAPITAL_API_KEY`
   - `CAPITAL_IDENTIFIER`, which is your login email
   - `CAPITAL_API_PASSWORD`, the API-key password
4. **Check the connection (read-only):**
   ```bash
   docker build -t goldbot .
   docker run --rm --env-file goldbot.env goldbot python -m goldbot broker-check
   ```
5. **Run in paper mode on the VPS first** for a few weeks:
   ```bash
   docker run -d --name goldbot --restart unless-stopped --env-file goldbot.env \
     -e GOLDBOT_MODE=paper -v $(pwd)/state:/app/state goldbot
   ```
6. **Switch to live (demo).** Only after paper results justify it:
   1. Set `mode = "live"` and `live_trading_enabled = true` in `goldbot.toml`.
   2. Add `GOLDBOT_LIVE_CONFIRM=I_UNDERSTAND_THIS_PLACES_ORDERS_ON_DEMO` to
      `goldbot.env`.
   3. Rebuild and restart the container.

In live mode the bot:
- places stop-entry orders with stop loss and take profit attached, set to
  expire at the end of the entry window
- cancels the other side of a one-cancels-other pair as soon as a position
  exists (checked every minute)
- closes everything at session end, at the daily loss limit or on `/stop`

---

## Research lab (goldlab)

Backtests the strategies with realistic costs and risk rules, and checks
whether any apparent edge survives out-of-sample data, walk-forward
re-optimisation and parameter changes. The output is `REPORT.md` with a verdict
for each strategy, plus PNG charts.

```bash
# put >= 2 years of XAUUSD M5/M15 bars in data/   (format: data/README.md)
python -m goldlab check-data
python -m goldlab run             # -> REPORT.md, results/*.png, results/summary.json
python -m goldlab smoke           # pipeline check on synthetic data (meaningless numbers)
```

`python -m goldlab fetch-dukascopy --start 2023-01-01 --end 2025-01-01` can
download free Dukascopy data. It hasn't been tested against the live server
yet, because the build environment couldn't reach Dukascopy.

### What's inside

```
goldbot/
  config.py      BotConfig (TOML + env), hard-limit validation, live-mode double lock
  bot.py         one cycle: commands -> data -> strategy -> engine -> alerts/orders -> state
  feeds.py       Twelve Data, Alpha Vantage, CSV / replay feeds
  notify.py      Telegram alerts + /stop /resume /status
  brokers/       Capital.com demo adapter
  replay.py      live-path vs backtest equivalence check
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

#### Sessions (stored in UTC, defined in local time so DST is handled)

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

#### Engine rules

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

#### Strategies

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

#### Validation

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

#### Not included (on purpose)

The research lab itself has no broker API and no order routing; that lives in `goldbot`.
