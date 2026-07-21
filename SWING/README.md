# SWING — multi-day equity swing book

**Built 2026-07-11.** The slow book: 2–10 day holds, one decision per morning,
a handful of trades per month. The whole point is the cost math — the intraday
book pays ₹60/RT dozens of times a week for fractions of moves it must abandon
at 15:25; SWING pays it a few times a month and gives winners days to run.

Isolation: strategy label **`swing`** → `logs/YYYY-MM-DD/swing/`,
`logs/swing_master.csv`, `trading.db` (a row per closed trade — SAGE ingests
automatically). Open positions persist in `SWING/positions.json`.

## How it works (all on DAILY closed candles — never intraday)

- **Universe**: the same 5yr-backtest-proven 20 stocks Trend Rider trades
  (copied, not imported — no dependency on the intraday engine).
- **Entry**: yesterday closed at a fresh 20-day closing high by at least
  0.25×ATR (a close 0.03% above the high is noise, not a breakout), with
  SMA9 > SMA21. Rank by breakout strength, fill up to **4 positions ×
  ₹50k**, product **CNC** (delivery — positions survive overnight).
- **Exit**: yesterday's close below the trailing stop
  (highest-close-since-entry − 2.5×ATR20), or below SMA21 (trend over),
  or 15 days held (sanity ceiling). Wide by design: daily noise must not
  shake out a position that needs days to work.
- **No daemon, no monitoring loop.** Stops are evaluated on daily closes
  only. Run it once each morning and it exits in under a minute.

## Run — every trading morning after 09:20

```bash
cd ~/openalgo/SWING && ./run_swing.sh
```

Add it to the morning routine next to the SAGE brief. If you skip a day,
nothing breaks — it just decides a day late (stops are daily anyway).

## How to judge it

Slowest engine to evaluate: expect **2–6 signals per month**. Do not judge
before ~10 closed trades (realistically 6–8 weeks). Metrics: net per trade
(costs should be <5% of average win), average hold days, and whether the
2.5×ATR trail is wide enough that winners aren't dying on day-2 noise.

Validation status: full lifecycle unit-tested offline (breakout + noise
filter, ATR trail exit, SMA21 exit, peak persistence across runs — a real
bug caught and fixed: HOLD-path peak updates weren't saved, which would
have computed tomorrow's stop from a stale peak). Live `history` endpoint
call with interval "D" untested — app was down at build time; first
morning run will confirm.
