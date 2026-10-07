# Put XAUUSD bar data here

Drop one or more CSV files in this folder (they are git-ignored). All files are
concatenated, de-duplicated and cleaned. You need **at least 2 years** of
**M5 or M15** bars (M1 also works; use `--minutes 5` or `--minutes 15` to resample).

## Preferred format (canonical)

```
timestamp,open,high,low,close,volume
2023-01-02T23:00:00Z,1826.47,1827.10,1825.90,1826.80,412
2023-01-02T23:05:00Z,1826.80,1827.35,1826.55,1827.20,388
```

- `timestamp`: bar **open** time, ISO 8601, **UTC** (a trailing `Z` or `+00:00`
  is fine). If your timestamps are naive and not UTC, pass `--source-tz`, e.g.
  `--source-tz Europe/London`.
- `open,high,low,close`: USD per troy ounce. Bid or mid prices (the engine adds
  the spread itself — do not use ask prices).
- `volume`: tick volume if you have it (used to weight the VWAP); may be 0 or
  omitted, in which case VWAP falls back to equal weights.
- Don't fill gaps or weekends; leave missing bars out.

## Also auto-detected

| Source | What it looks like | Notes |
|---|---|---|
| Dukascopy (JForex / web export) | `Gmt time,Open,High,Low,Close,Volume` with `02.01.2024 00:00:00.000` | Choose GMT, BID, 5 or 15 min |
| MetaTrader 5 "Export bars" | tab-separated `<DATE> <TIME> <OPEN> ...` | Broker server time: pass `--source-tz`, e.g. `Etc/GMT-2` (check your broker; many use GMT+2/+3 with US DST, i.e. `Europe/Athens` is a close match) |
| HistData.com ASCII M1 | `20240102 000000;2063.5;...` (no header) | EST without DST, handled automatically; resample with `--minutes 5` |

Check what the loader sees before a full run:

```
python -m goldlab check-data
```
