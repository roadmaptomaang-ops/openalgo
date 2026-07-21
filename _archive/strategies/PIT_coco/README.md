# PIT — Personal Quant Trading & Analytics Suite

A solo quant trading operation: two live strategies plus a research/analytics
suite that turns the trade history they produce into actionable feedback.

This is the corrected, fully-wired build (`PIT_coco`). See **"Fixes applied"**
at the bottom for what was broken in the originals.

---

## Layout

```
PIT_coco/
├── scalper_pro_v4_3.py     # LIVE: NIFTY + SENSEX options scalper
├── trend_rider_v2.py       # LIVE: NIFTY50 equity trend rider
│
├── db_logger.py            # SQLite persistence layer  (~/openalgo/trading.db)
├── trade_logger.py         # File logging (activity/trades.log, trades.csv) + DB write
│
├── analytics_core.py       # Shared foundation: DB access, stats primitives, trade loader
├── edge_decay.py           # Strategy-degradation detector (7d/14d/30d vs baseline)
├── loss_autopsy.py         # Root-cause classifier for every losing trade
├── whatif.py               # Parameter what-if simulator (ADX / score / time / direction)
├── cooling_simulator.py    # Mandatory-cooldown simulator (after SL / target / N losses)
├── filter_audit.py         # "Does each filter earn its place?" expectancy splits
├── backtest.py             # SMA 9/21 crossover backtester (yfinance, 5yr daily)
│
├── import_csv.py           # Backfill DB from the master CSVs (idempotent)
├── report.py               # Terminal daily / multi-day report + interactive SQL
├── report_generator.py     # Full interactive HTML report (pulls all analytics modules)
└── reports/                # Generated HTML reports land here
```

## Data flow

```
 Live strategies -> trade_logger.csv_row(...) --+--> per-day CSV + master CSV
                                                +--> db_logger.insert_trade() --> trading.db (trades)
 breadth snapshots ----------------------------------> db_logger.insert_breadth() -> breadth_log

 import_csv.py  -- master CSV --> trading.db (idempotent backfill)

 analytics_core.load_trades()  reads trading.db
        |
        +- edge_decay        -> edge_decay_snapshots
        +- loss_autopsy      -> loss_autopsy
        +- whatif            -> whatif_results
        +- cooling_simulator -> whatif_results
                                     |
                          report_generator.py -> reports/report_YYYY-MM-DD.html
```

The SQLite DB lives at `~/openalgo/trading.db` (6 tables: `trades`,
`breadth_log`, `daily_summary`, `edge_decay_snapshots`, `loss_autopsy`,
`whatif_results`).

## Canonical CSV / trades schema (34 columns)

```
date, entry_time, exit_time, strategy, instrument, direction, symbol,
strike_type, entry_price, exit_price, quantity, pnl_pts, pnl_rs, mfe_pts,
mae_pts, capture_pct, exit_reason, hold_seconds, adx_entry, atr_entry,
ema9_entry, ema21_entry, vwap_entry, orb_high, orb_low, max_pain,
max_pain_bias, oi_bias, momentum, be_triggered, trail_count, high_water_pts,
trend_score, running_total
```

Both strategies write exactly these columns; `import_csv.py` and the DB schema
expect exactly these. The DB adds `id`, `diag_tag`, `diag_msg`, `created_at`.

## Usage

All commands assume the OpenAlgo `uv` environment (for `pytz`, `pandas`, etc.):

```bash
cd ~/openalgo/PIT_coco

# 1. Backfill DB from historical master CSVs (safe to re-run; skips duplicates)
uv run python import_csv.py

# 2. Terminal reports
uv run python report.py                       # today
uv run python report.py --date 2026-06-05
uv run python report.py --strategy scalper
uv run python report.py --last 7
uv run python report.py --query               # interactive SQL

# 3. Individual analytics (each runs standalone; prints + persists)
uv run python edge_decay.py
uv run python loss_autopsy.py
uv run python whatif.py
uv run python cooling_simulator.py
uv run python filter_audit.py scalper

# 4. Full HTML report (runs all analytics modules, writes to reports/)
uv run python report_generator.py --last 30
uv run python report_generator.py --date 2026-06-05 --strategy scalper
uv run python report_generator.py --from 2026-06-01 --to 2026-06-12 --out ~/Desktop/r.html

# 5. Backtest (needs yfinance: `uv pip install yfinance`)
uv run python backtest.py
```

Live strategies:

```bash
uv run python scalper_pro_v4_3.py
uv run python trend_rider_v2.py
```

## Exit-reason vocabulary

Strategies log human-readable exit reasons (`"SL Hit"`, `"Target Hit"`,
`"High Water Exit"`, `"Trailing SL"`, `"No Momentum Exit"`, `"Hard Exit"`).
`analytics_core.normalize_exit_reason()` maps these to stable codes
(`SL_HIT`, `TARGET_HIT`, `HIGH_WATER_EXIT`, `TRAILING_SL`, ...). All analytics
consumers classify on the normalized code, never the raw label.

---

## Fixes applied in this build

Carried forward from the live-strategy review:
- **scalper**: `CANDLE_CONFIRM_COUNT=3`, `LATE_ENTRY_CUTOFF="14:30"`,
  per-instrument `adx_max` (50) and `high_water_drop` (NIFTY 3 / SENSEX 7).
- **trend_rider**: removed duplicate decls in `run_equity`, breadth logging goes
  straight to `db_logger`, REST-fallback exchange map is per-symbol (not hardcoded
  `NSE`), No-Momentum exit uses `pnl_pts < min_mfe_pts * 0.5`.

Analytics-suite wiring fixes:
- **Critical -- writes never persisted.** `edge_decay`, `loss_autopsy`, `whatif`
  and `cooling_simulator` saved results via `DBLogger.query()`, which runs the
  INSERT but never commits, so every row was rolled back on connection close.
  Added `DBLogger.execute()` (commits, returns lastrowid) and repointed all four
  modules to it. Verified end-to-end: the three analytics tables now populate.
- **Exit-reason mismatch.** Consumers compared against `"SL"` / `"TARGET"` /
  `"TRAIL"` while strategies write `"SL Hit"` / `"Target Hit"` / `"Trailing SL"`,
  so SL/target logic in loss_autopsy and cooling_simulator never fired. Added
  `normalize_exit_reason()` and wired all consumers + `trade_logger.diagnose` to it.
- **Standalone path resolution.** `analytics_core`, `report_generator` and
  `import_csv` insert their own directory on `sys.path` first, so the suite is
  self-contained and runs from this folder regardless of name.
- **Backtest accounting.** Exit released only the entry notional, dropping the
  trade P&L from cash; now releases margin plus P&L. Added empty/missing-column
  guards and final open-position liquidation.
- `trade_logger.diagnose()` confirmed present and signature-matched to both
  strategies' call sites.
