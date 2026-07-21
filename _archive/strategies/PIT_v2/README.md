# PIT_v2 — next-gen strategies (isolated for safe testing)

Built 2026-07-02. These are **improved copies** of the working engines in
`../PIT_codex_fixes/`. The originals are UNTOUCHED — if anything here breaks,
fall back to `PIT_codex_fixes/`. Each engine here logs under a NEW strategy
name, so its trades never mix with the originals in `trading.db`, the log
folders, or SAGE.

| File here | Copied from | New strategy label / log folder |
|---|---|---|
| `trend_rider_v3.py`  | `trend_rider_v2.py`   | `trend_rider_v3` |
| `scalper_v52_rider.py` | `scalper_v51_rider.py` | `scalper_v52` |
| `scalper_pro_v4_3.py`, `trade_logger.py`, `db_logger.py` | (dependencies, unchanged copies) | — |

## Run

```bash
export OPENALGO_API_KEY=<64-hex key>
cd ~/openalgo/PIT_v2 && caffeinate -i uv run python3 trend_rider_v3.py
cd ~/openalgo/PIT_v2 && caffeinate -i uv run python3 scalper_v52_rider.py
```

Everything writes to the same `~/openalgo/trading.db` and `~/openalgo/logs/`,
just under the new labels. SAGE (`sage_runner.py enrich`) will pick them up
automatically as new strategies — compare them against the originals directly.

---

## What changed vs the originals — and WHY

### `trend_rider_v3.py` (the profitable engine — highest-value changes)

Data that motivated it (78 trades, 18 days): Trend Rider hit its **5-trade/day
cap on 9 of 18 days**, and it was burning the budget on 2-minute scratches —
so it locked itself out of real afternoon trends. P&L by trade-number-of-day
showed **trade #6 averages +Rs427** and #8 +Rs396: trading past 5 is +EV, the
cap was leaving money on the table. Meanwhile 29% of all trades were quick
scratches (<3min, |P&L|<Rs100) netting **−Rs798** that ate cap slots.

1. **Daily cap 5 → 8** (`MODES["balanced"].max_trades_per_day`). Lets it reach
   the profitable #6–#8 trades it was cut off from.
2. **Scratches don't count toward the cap.** A no-momentum exit with
   `|P&L| < SCRATCH_PNL_MAX` (Rs100) is a non-event: `RiskEngine.on_close(...,
   real_trade=False)` settles the P&L but does NOT increment `trade_count` or the
   consecutive-loss counter. So the 8-trade budget is spent on real trades, not
   noise.

Reversible: set `max_trades_per_day` back to 5 and/or `SCRATCH_PNL_MAX = 0`.

### `scalper_v52_rider.py` (= V5.1 + two SAGE-backed fixes)

Keeps everything from V5.1 (re-entry guard, nan-guard, circuit breaker,
stale-quote exit, funnel). Adds:

1. **ExcludeZeroMFE fast bailout** — SAGE's #1 what-if fix (**+Rs3,729 ADOPT**).
   Trades whose MFE never goes positive are near-certain losers. New exit #3:
   after `ZERO_MFE_SECS` (90s), if peak profit is still ≤ `ZERO_MFE_PTS` (1.0),
   bail immediately instead of riding to the full hard SL. Logs as
   `Zero-MFE Bailout`. Toggle: `ZERO_MFE_ENABLED`.
2. **Per-day trade cap vs DB** — `MAX_TRADES` reset per process, so reruns
   re-armed a fresh budget and re-bled. V5.2 seeds `trade_count` at startup from
   today's `scalper_v52` rows in `trading.db` (`_today_trade_count`), so a rerun
   CONTINUES the day's count toward `MAX_TRADES` instead of restarting. The
   "don't-rerun-after-circuit-breaker" discipline, enforced in code. Toggle:
   `PERDAY_CAP_FROM_DB`.

Reversible: `ZERO_MFE_ENABLED = False`, `PERDAY_CAP_FROM_DB = False` → behaves
exactly like V5.1.

---

## How to judge them

Run v3/v52 alongside the originals (paper) for ~10 sessions, then compare in the
DB:

```bash
cd ~/openalgo && uv run python3 - <<'PY'
import sqlite3
c=sqlite3.connect('trading.db')
for s in ('trend_rider','trend_rider_v3','scalper_v51','scalper_v52'):
    r=c.execute("SELECT COUNT(*),ROUND(SUM(pnl_rs)) FROM trades WHERE strategy=?",(s,)).fetchone()
    print(f"{s:16} {r[0] or 0:4} trades  net Rs {r[1] or 0:+.0f}")
PY
```

If v3 doesn't beat trend_rider (or v52 doesn't beat v51) over a real sample,
the toggles above revert each change independently. Nothing here touches the
originals.
