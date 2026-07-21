# PIT Strategies — Fixes, Status & Handoff

_Last updated: 2026-07-01 (evening). Self-contained handoff so you can maintain these
without further assistance._

## The three engines

| File | Strategy tag / log folder | What it trades | Status |
|---|---|---|---|
| `scalper_pro_v4_3.py` | `scalper` | NIFTY+SENSEX index options | Retired (superseded by V5). Still has ADX ceiling 25–35. |
| `scalper_v5_rider.py` | `scalper_v5` | NIFTY+SENSEX index options | **Primary.** Breakout entry, ride-the-trend exit. |
| `scalper_v51_rider.py` | `scalper_v51` | NIFTY+SENSEX index options | **NEW.** = V5 + re-entry guard (toggle). For A/B vs V5. |
| `trend_rider_v2.py` | `trend_rider` / `Trend Rider V2.0` | NIFTY-50 equity (long + short) | Runs alongside as the equity engine. |

All run in the OpenAlgo **sandbox/paper** book (virtual money). Orders route through the
local OpenAlgo API at `http://127.0.0.1:5000` and show up in the analyzer.

---

## How to run (every trading day, before 09:20 IST)

```bash
# key is 64 hex chars — the leading '1' is easy to drop; verify length == 64
export OPENALGO_API_KEY=1c19c83e21ce74f33fc323b1c1af438c35d11ef669cea5215f07212af308e81e

# Terminal 1 — V5 (or V5.1 — do NOT run both live; double exposure on same options)
cd ~/openalgo/PIT_codex_fixes && caffeinate -i uv run python3 scalper_v5_rider.py

# Terminal 2 — Trend Rider (equity, safe to run with a scalper — different instruments)
cd ~/openalgo/PIT_codex_fixes && caffeinate -i uv run python3 trend_rider_v2.py

# Terminal 3 — SAGE (morning before open, evening after close)
cd ~/openalgo/SAGE && uv run python3 sage_runner.py morning
cd ~/openalgo/SAGE && uv run python3 sage_runner.py evening
```

- `caffeinate -i` = keep Mac awake **while the script runs** (auto-releases on exit).
  Without it, Mac sleep freezes the process mid-session (caused 11–90 min log gaps).
- Run from the `PIT_codex_fixes/` directory (scripts import siblings).
- The `VIRTUAL_ENV=.venv-1 does not match` warning is harmless — uv uses `.venv`.

### THE #1 DISCIPLINE RULE
**If the circuit breaker fires, the day is DONE. Do NOT rerun the scalper.**
`MAX_TRADES=5` resets per process, so a rerun starts a fresh 5-trade budget and
re-bleeds into the same chop. This single behaviour caused the two worst days:
- 24-Jun: 4 reruns turned −₹1,590 into **−₹4,902**
- 30-Jun: 1 rerun added −₹859 after session 1's −₹1,175

Rerunning after a *clean* session that hit the 5-trade cap (not the breaker) is fine —
that's catching more of a live trend (e.g. 01-Jul, +₹2,259 over two clean sessions).

---

## FIXED (verified in live paper)

1. **Stale-quote protective exit** (V5/V5.1) — `STALE_QUOTE_EXIT_SECS=25`. If no valid
   quote for 25s while in a position, fire a verified protective market exit instead of
   riding blind. Fixes the 23-Jun trade-#9 blackout (−₹1,095, a 4-min feed gap let a
   loss balloon to 2.4× the stop).

2. **Circuit breaker** (V5/V5.1, shared `DayRisk` across NIFTY+SENSEX threads) — halts
   ALL new entries after `MAX_CONSEC_LOSSES=3`, or profit-lock: once day peak ≥ ₹2000
   (`PROFIT_LOCK_ARM`), halt if P&L gives back ₹1500 (`PROFIT_LOCK_GIVEBACK`). Open
   positions still ride out. Threshold is 3 not 2 (2 would kill profitable mornings).

3. **nan-ADX entry guard** (V5/V5.1 line ~635; trend_rider line ~2271) — `nan < threshold`
   is `False` in Python, so the strategy traded blind at the open before enough candles
   existed. Fix: explicit `if adx != adx` (nan) skip. **Verified 01-Jul: 41 skips at the
   open, first entry delayed to a valid-ADX time, zero nan trades.** (Bug caused 30-Jun
   trades #1 on both indices — straight to SL with `ADX=nan`.)

4. **Trend Rider PE / short support** — removed the `if rank.direction != "CE": continue`
   long-only gate. Now shorts PE signals (SELL entry / BUY exit), P&L flipped
   (`entry − curr`), breadth gate flipped (skip PE on BULLISH, CE on BEARISH), momentum
   check flipped, hard-exit direction-aware. On bearish days it can now short instead of
   sitting idle.

5. **Trend Rider routes through OpenAlgo** — removed the `paper_mode` short-circuit in
   `ExecutionEngine.execute()` so orders always hit the sandbox API (visible in the
   analyzer) like V5, instead of simulating fills locally.

6. **Trend Rider ADX ceiling 35 → 60** with a once/60s skip-log throttle (stops
   activity-log spam).

---

## V5.1 RE-ENTRY GUARD — built, but READ THIS before trusting it

**What it does:** blocks a *same-direction* re-entry until spot makes a fresh extreme
beyond the previous same-direction entry by `orb_buffer` pts (NIFTY 20 / SENSEX 60).
Opposite direction is never blocked. Toggle: `REENTRY_GUARD = True/False` at the top of
`scalper_v51_rider.py`. Logs to its own `scalper_v51` folder + master CSV for clean A/B.

**Full-log replay (all 7 sessions 20-Jun→01-Jul) — net POSITIVE, but it's chop insurance.**
(An earlier 2-day spot check called it a "wash" — that sample happened to be the two days
the guard hurts. The full replay corrects that.)

| Date | Regime | V5 actual | V5.1 (guard) | Δ |
|---|---|---|---|---|
| 20-Jun | trend | +2,990 | +2,990 | 0 |
| 23-Jun | mixed | +1,467 | +2,715 | **+1,248** |
| 24-Jun | chop+reruns | −4,901 | −2,976 | **+1,925** |
| 25-Jun | chop | −1,523 | −1,385 | +138 |
| 29-Jun | strong trend | +4,164 | +3,078 | **−1,086** |
| 30-Jun | chop | −2,034 | −1,794 | +240 |
| 01-Jul | strong trend | +2,259 | +1,111 | **−1,148** |
| **TOTAL** | | **+2,422** | **+3,739** | **+1,317** |

**Pattern:** guard HELPS on chop/mixed days (blocks re-entries into chop) and HURTS on
strong trend days (blocks continuation winners). It trades upside on the best days to cut
losses on the worst. Net +₹1,317 over the sample (~+54%). Biggest single save was the worst
day, 24-Jun (+₹1,925) — though on that day *not rerunning* would have saved even more.

**Caveats:** (1) naive replay — a blocked trade frees a slot in the 5-trade cap, so live
V5.1 might take different later trades not simulated here; (2) 7 days is a small sample and
the two "hurt" days alone are −₹2,234, so a run of pure trend days erases the edge; (3) the
edge concentrates on chop days, which overlaps what the circuit-breaker + don't-rerun rule
already protect.

**Recommendation:** treat V5.1 as a **net-positive chop hedge**, confidence ~60% it beats
V5. Run it in analyzer side-by-side with V5 (separate log folders) to confirm live. Expect
it to shine on choppy weeks and lag on trending ones. Toggle `REENTRY_GUARD = False` makes
V5.1 == V5 if you want to A/B the guard in isolation. Replay script: parse each
`logs/*/scalper_v5/trades.log` for TRADE cards (Spot Price, Signal, P&L Rupees), reset
`last_entry_spot` on trade-number reset (session boundary), block same-dir unless spot beats
last same-dir entry by orb_buffer (NIFTY 20 / SENSEX 60).

### V5.1 also adds an entry-funnel diagnostic (ported from V4.3)
Writes one row per instrument per day to `logs/scalper_v51_funnel.csv` answering
"why didn't we trade?" — columns: `scans, setup_seen, taken, adx_nan, adx_low, no_setup,
confirm_pending, confirm_inconsistent, reentry_block, entry_failed`. This is the visibility
V5 lacks (V5 tells you what it traded, not what it skipped). Use it to see, e.g., how often
`reentry_block` fires (is the guard too aggressive?) or whether a quiet day was chop
(`no_setup` high) vs filtered (`adx_low` high). V5.1 is the only engine that writes it;
adding the same to plain V5 is a straightforward port if you want it there too.

---

## STILL OPEN (ideas, not yet built)

- **Regime / time-of-day filter** — the real signal in the data is *regime*, not entry
  quality: trend day = green (23,29-Jun; 01-Jul), chop day = red (24,25,30-Jun). V5's
  P&L tracks the market, not the filters. A pre-trade regime check (e.g. skip nan/early
  minutes — done; add: require sustained ADX or opening-range expansion; sit out known
  chop windows) is more promising than any re-entry gate.
- **Per-day trade cap in the DB** — replace the per-process `MAX_TRADES=5` with a count
  against today's DB rows, so reruns can't re-arm the budget. Enforces the discipline
  rule in code instead of by hand.
- **Trail-arm dead zone** (minor) — `trail_activate` (8/18) > `be_trigger` (6/14): trades
  that peak between BE and trail-arm round-trip to the BE stop for a scratch. Low priority
  (it's protective).

---

## Reading the logs / computing P&L yourself

```bash
# per-day folders:  ~/openalgo/logs/YYYY-MM-DD/{scalper_v5,scalper_v51,trend_rider}/
#   trades.log     — human-readable trade cards + day summary
#   activity.log   — every scan line (debugging)
#   trades.csv     — structured, one row per trade
# master (all days): ~/openalgo/logs/<strategy>_master.csv

# Quick P&L for any engine (change the filename):
uv run python3 - <<'PY'
import csv
rows=list(csv.DictReader(open('logs/scalper_v5_master.csv')))
n=lambda x:(float(x) if x not in('',None) else 0.0)
pnl=[n(r['pnl_rs']) for r in rows]
w=[p for p in pnl if p>0]; l=[p for p in pnl if p<0]
print(f'{len(rows)} trades | net Rs {sum(pnl):+.0f} | win% {len(w)/len(rows)*100:.0f} '
      f'| gross +{sum(w):.0f}/{sum(l):.0f} | PF {sum(w)/abs(sum(l)):.2f}')
PY
```

## V5 record so far (91 trades, 23-Jun → 01-Jul)
Net **−₹568**, PF 0.97, 38.5% win rate. But −₹6,935 of the losses were on the two
*fixable* days (24-Jun reruns, 30-Jun nan-bug). The 4 clean days net **+₹6,367**. The
engine makes money on trend days when the discipline rule and nan-fix are honoured.

---

## FINAL HANDOFF (2026-07-02) — read this first

**The verdict after the full history (Jun 1 → Jul 2, all paper):**

| Engine | Net P&L | Verdict |
|---|---|---|
| **Trend Rider** (`trend_rider_v2.py`, equity) | **+₹12,044** (73 trades) | ✅ the only durable earner — make this primary |
| Scalper V4.3 | −₹1,650 | retired |
| Scalper V5 | ≈ −₹4,100 | net loser |
| Scalper V5.1 (guard) | −₹577 | chop-hedge, sample too small |

**Bottom line:** the equity trend-follower makes money; both index-option scalpers lose it.
Feed the winner, starve the loser.

**Why the scalper is structurally weak (not just unlucky):**
1. Only wins on clean trend days (~1 in 3). Chop days erase it.
2. Trades repeatedly reach big MFE (+15/+22/+29 pts) then round-trip to a loss — the ride
   gives back too much on choppy tape.
3. **Brokerage buries it.** Upstox ₹20/leg = ₹40/round-trip. A 20-trade day = ₹800 in fees
   before P&L. Gross P&L on a busy day is fiction; −₹800 is the real starting line.
4. Paper mode flatters it (no slippage, no IV crush, instant fills). Live = worse.

**Highest-value fixes still open, in order (from SAGE's own what-if sims):**
1. `ExcludeZeroMFE` — exit faster on trades whose MFE never goes positive. SAGE: **+₹3,729 ADOPT**. Biggest, cleanest.
2. `cooldown_SL_10min` — 10-min pause after any stop. SAGE: **+₹2,821 ADOPT**.
3. `MaxTrades=3` (not 5) + a per-DB-day cap so reruns can't re-arm the budget. SAGE: +₹765.
4. Investigate why Trend Rider goes idle after ~5 trades (it leaves green setups on the table — money unclaimed on the ONE profitable engine).

**Discipline rule that alone would have saved thousands:** after the circuit breaker fires,
STOP. Do not rerun the scalper. (24-Jun: 4 reruns turned −₹1,590 into −₹4,902.)

## SAGE — now has a working pattern registry (2026-07-02)
- `sage_patterns.py` mines `trade_stories` → `pattern_registry` (EDGE/TRAP/NEUTRAL).
  Run `uv run python3 sage_runner.py patterns`, or it auto-runs after `enrich`.
- Morning brief now shows the playbook table (edges green, traps red).
- Top EDGE: OPT-VSTRONG-MIDDAY-SHORT (65% WR, +₹469, 17 trades).
  Top TRAP: all high-ADX LONGS (options and equity) lose — confirms the ADX-exhaustion thesis.
- Fixed: V5.1 was writing DB rows as `strategy="scalper_v5"` (line 436) — corrected.

## Daily commands (unchanged)
```
export OPENALGO_API_KEY=<64-hex key>
cd ~/openalgo/PIT_codex_fixes && caffeinate -i uv run python3 trend_rider_v2.py   # the earner
cd ~/openalgo/PIT_codex_fixes && caffeinate -i uv run python3 scalper_v51_rider.py # if you must scalp
cd ~/openalgo/SAGE && uv run python3 sage_runner.py morning     # before open
cd ~/openalgo/SAGE && uv run python3 sage_runner.py evening     # after close
cd ~/openalgo/SAGE && uv run python3 sage_runner.py enrich      # after evening: ingest + refresh patterns
```
Only run ONE scalper instance live at a time (V5 and V5.1 trade the same options).

---

## THE REAL PICTURE — net of costs (added 2026-07-02, `sage_runner.py scorecard`)

Every P&L in the logs is GROSS. After realistic Upstox costs (~₹55/RT options,
~₹60/RT equity), the full history (284 trades, Jun 1 → Jul 2):

| Engine | Gross | Costs | **NET** |
|---|---|---|---|
| trend_rider | +₹12,164 | ₹4,680 | **+₹7,484** |
| scalper (v4.3) | −₹1,650 | ₹4,895 | **−₹6,545** |
| scalper_v5 | −₹4,127 | ₹6,160 | **−₹10,287** |
| **TOTAL** | **+₹5,881** | **₹16,010** | **−₹10,129** |

**The book is gross-positive but NET-NEGATIVE. Costs (₹16k) are the single biggest
line item.** Overtrading friction — not bad entries — is the primary enemy.

### And the edge has DECAYED
trend_rider's entire +₹7,484 was earned **by June 12**. Since June 13 it's −₹49/trade
net. The scorecard now auto-flags this ("EDGE DECAY DETECTED"). **Nothing — scalper or
equity — has been net-positive for ~3 weeks.** Something changed around Jun 12–17
(regime shift?). Do not trade live off a fossil edge; re-fit or stand down until a
fresh net-positive window appears.

### New SAGE command
```
uv run python3 sage_runner.py scorecard              # net-of-cost per engine + decay flag
uv run python3 sage_runner.py scorecard --days 5     # recent window
uv run python3 sage_runner.py scorecard --curve trend_rider   # daily net equity curve
```
Run it every evening. NEVER evaluate a gross number again. Edit `COST_PER_ROUNDTRIP`
in `SAGE/sage_scorecard.py` to your real broker rates.

---

## WHY the edge decayed — it's REGIME, and it's recoverable (2026-07-02)

The decay isn't a broken strategy — the MARKET stopped offering follow-through.
Same entries, but price stopped moving after entry:

| trend_rider | Jun 1–12 (winning) | Jun 13+ (losing) |
|---|---|---|
| avg MFE (how far trades ran for you) | **6.9 pts** | **1.0 pts** |
| trades that caught a real move (MFE≥3) | 59% | 9% |
| avg hold | 12.8 min | 5.5 min |
| no-momentum scratches | 29% | 59% |

Textbook trend→chop shift. **The edge is DORMANT, not dead — it returns when
trending markets return.** (Tested an intraday "probe first 3 trades, halt if
low MFE" gate — it FAILED, -Rs387, too noisy / too few trades to probe. Regime
persists for WEEKS, so the signal is a rolling gauge, not an intraday halt.)

### The gauge to watch: follow-through health (in the scorecard now)
`scorecard` prints rolling avg MFE for the earner. MFE collapses BEFORE P&L, so
it's a LEADING regime indicator:
- **avg MFE > 3 pts → TRENDING**, trade the earner
- **avg MFE < 2 pts → CHOP**, stand down (edge dormant)

As of Jul 2 it reads **1.1 pts = CHOP → stand down.** Resume the earner only when
it climbs back above ~3. This is the single number that tells you when the edge
is back — check it every evening with the scorecard.

---

## CHASE GATE — built 2026-07-08, backtested first (the "exhaustion" signal)

**Origin:** Danny called out that every rebuild of these strategies keeps getting
"fucked" the same way — good entry, good gates, still gives back the day's gains.
The question: is there a signal for when a MOVE is exhausted, not just whether an
ENTRY looks good? Mined `trading.db` for it before writing any code.

### The finding
Grouped every trade by `(date, strategy, instrument-or-symbol, direction)` and
ranked by entry order within that group — i.e. "was this the 1st, 2nd, 3rd...
attempt at this exact direction today?"

| Entry # in the day's push | n | Avg P&L | Win Rate |
|---|---|---|---|
| 1st | 78 | **+₹15** | 48.7% |
| 2nd | 48 | −₹84 | 47.9% |
| 3rd | 36 | −₹37 | 41.7% |
| 6th | 11 | −₹110 | 36.4% |
| 9th-11th | 3-8 | −₹155 to −₹403 | **0%** |

The 1st entry in any direction is the ONLY one that's net profitable on average.
**ADX at entry does NOT predict this** (bounces 30-52 with no trend across the
sequence) — the exhaustion signal is purely "how many times has this direction
already been tried today," not anything visible in the indicators.

Sharper cut — 2nd trade split by whether the 1st won or lost:
| | n | Avg P&L | WR |
|---|---|---|---|
| 2nd trade, after a WIN | 23 | **−₹157** | 43.5% |
| 2nd trade, after a LOSS | 25 | **−₹17** | 52.0% |

Chasing a winner is ~9x worse than re-entering after a stop (small-n caveat:
23/25, directional not gospel).

### The rule: "Chase Gate"
Block any 2nd+ same-direction entry if the immediately preceding same-direction
trade WON. Toggle: `CHASE_GATE_ENABLED`.

### Backtest per engine BEFORE arming it live — results are NOT uniform
First pass grouped trend_rider by `instrument` (="EQUITY" for every trade,
regardless of stock) — wrong, and gave a misleadingly positive number. Redone
grouped by actual `symbol` for trend_rider:

| Strategy | Actual NET | With gate | Δ | Sample | Verdict |
|---|---|---|---|---|---|
| scalper_v51 | −₹3,398 | −₹938 | **+₹2,460** | n=21 | ✅ ON by default |
| trend_rider | +₹6,685 | +₹4,715 | **−₹1,970** | n=94 (large) | ❌ OFF by default |
| mint | +₹538 | −₹83 | −₹621 | n=15 (~4 days) | ❌ OFF, too small to trust |

**Why it flips for trend_rider:** index options have exactly one tradeable
direction per instrument at a time, so a 2nd entry really is re-chasing the
identical exhausted move. Trend Rider re-selects from a rotating 20-stock
universe by live momentum score each cycle — re-buying a stock that just won
AND is still top-ranked is usually genuine continuation, not blind chasing.
Sequence-position alone doesn't mean the same thing across engine designs.

### What's built
Added to all three engines as requested, defaults set per the evidence above:
- `scalper_v51_rider.py` — `CHASE_GATE_ENABLED = True`
- `trend_rider_v2.py` — `CHASE_GATE_ENABLED = False` (flip on to re-test; large
  sample says it currently hurts)
- `mint.py` — `CHASE_GATE_ENABLED = False` (sample too small either way; revisit
  once MINT has 30+ trades)

Mechanism: one dict tracking the outcome of the most recent CLOSED trade per
direction (options) or per symbol (equity); checked right before order
placement; updated at every exit path via each engine's existing single choke
point (V5.1's `finalize()`, MINT's `governor.record()` call sites, trend_rider's
`risk.on_close()` call sites).

---

## BUG FIXED 2026-07-08: MINT was never reading its own pattern history

`SAGE/sage_patterns.py::_strategy_group()` only mapped `strategy=='trend_rider'`
(exact match) and `strategy.startswith('scalper')` to their pattern families.
`'mint'` matched neither → fell through to `'other'` → every MINT trade got
bucketed under an orphaned `OTH-*` pattern family nothing reads. Meanwhile
`mint.py::pattern_gate()` queries `OPT-*` patterns (built only from the
scalper engines' history). **Net effect: MINT's "consult your own learned
history" gate has been checking OTHER engines' track record, not its own,
since it was built on 2026-07-02.**

Fixed: `_strategy_group()` now also maps `mint` → `options` and any
`trend_rider*` prefix → `equity` (the latter also silently broken for the
PIT_v2 `trend_rider_v3` variant, never triggered yet but same bug). Re-ran
`sage_runner.py patterns` after the fix — `OPT-VSTRONG-MIDDAY-SHORT` correctly
absorbed MINT's 20-vs-17 trade count, WR moved 65%→60%, avg +469→+369, still
EDGE but appropriately thinner. New pattern `OPT-MODERATE-AFTERNOON-SHORT`
surfaced (16 samples, 56% WR, +₹149, now ENTER).

**Also fixed same day: `Governor.today_db_count()` (mint.py)** — counted ALL
mint trades today with no `instrument` filter, so a rerun had both NIFTY and
SENSEX threads read the COMBINED total (e.g. 6) instead of their own count
(3 each), concluding the 4-trade budget was already blown and exiting
instantly on startup. Fixed by adding `instrument=?` to the query and passing
`name` at the call site. Verified live: NIFTY/SENSEX now correctly read 3/3
independently after a rerun.

Two real, silent bugs in MINT's two newest mechanisms (pattern-history lookup
and per-day budget), both caught by actually trying to rerun it mid-session
rather than trusting the design on paper. Worth a slow, deliberate spot-check
of any new gate/organ the first few times it's exercised for real.

---

## MONITOR-INTERVAL FIX 2026-07-09 (adaptive fast-poll while in profit)

**Problem quantified 2026-07-08:** the trail/floor is a TRIGGER level, not a
guaranteed fill. Both scalper engines poll quotes every 5s in a position; a
violent reversal gaps through the trail between polls. V5.1's big SENSEX PE
winner had "Final Locked +111.3pts" but filled at +55.1 — **Rs1,123 of
slippage in a single 5s window** (NIFTY same day: locked +36.1, filled +26.4).
~Rs1,400 quantified across two trades.

**Fix (both engines):** adaptive poll interval — 5s normally, **1s once
there's locked profit to protect**:
- `mint.py`: `MONITOR_INTERVAL_ARMED = 1`, used when the ratchet is `armed`.
- `scalper_v51_rider.py`: `MONITOR_INTERVAL_HOT = 1`, used when `trail_active`.

Shrinks the worst-case gap ~5x exactly when it matters, without hammering the
quotes API all day (fast mode only runs during armed/trailing stretches, which
last minutes). Trade-off: armed periods write ~5x more activity-log lines and
quote calls (1/s per instrument — trivial vs the 50/s rate limit).

Applies to the current lineup (MINT + V5.1). NOT ported to PIT_v2's
scalper_v52 (not in the daily lineup); trend_rider uses a different
tick-cache architecture and wasn't the engine that showed the slippage.
