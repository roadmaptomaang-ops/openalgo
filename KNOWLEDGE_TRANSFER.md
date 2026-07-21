# Knowledge Transfer — Danny's Algo Trading System

> A complete guide to what this system is, why it exists, how it evolved, what
> every folder does, and exactly how to run each piece. If you're new here, read
> this top to bottom once — you'll be able to run everything by the end.
>
> _Last updated: 2026-07-02. Scale: ~54,650 lines of code + docs across 6 folders
> (33,420 pure Python), 169k lines including all trade logs._

---

## 1. What is this?

This is a **self-hosted algorithmic trading research lab** built on top of
[OpenAlgo](https://openalgo.in) (an open-source broker-API platform). It does
three things:

1. **Trades** NIFTY/SENSEX index options and NIFTY-50 equities intraday, via
   automated Python strategies, in **paper/analyzer mode** (virtual money — no
   real capital at risk).
2. **Analyses** every trade after the close (the "PIT" layer) — why it won or
   lost, what filters would have helped, whether the edge is decaying.
3. **Predicts & learns** (the "SAGE" layer) — each morning it forms a hypothesis
   about the day and matches today's setup against a memory of past trades; each
   evening it grades itself and updates its playbook.

**The goal:** find a genuinely profitable intraday edge, prove it with honest
data (net of costs, not gross), and know *when* to trade it vs when to sit out.

**The broker:** Upstox, connected through OpenAlgo running locally at
`http://127.0.0.1:5000`. Everything is paper-traded through OpenAlgo's sandbox.

---

## 2. The big picture — three layers

```
   ┌─────────────────────────────────────────────────────────┐
   │  STRATEGY ENGINES  (place the paper trades)              │
   │  · Scalper family  → NIFTY/SENSEX options               │
   │  · Trend Rider      → NIFTY-50 equity                    │
   └───────────────┬─────────────────────────────────────────┘
                   │ writes every trade to trading.db + logs/
                   ▼
   ┌─────────────────────────────────────────────────────────┐
   │  PIT  (Post-market Intelligence)                         │
   │  reads trading.db → loss autopsy, what-if filters,       │
   │  edge-decay, backtests, reports                          │
   └───────────────┬─────────────────────────────────────────┘
                   │ shares the same trading.db
                   ▼
   ┌─────────────────────────────────────────────────────────┐
   │  SAGE  (Situational Awareness & Growth Engine)           │
   │  morning brief · similarity memory · pattern registry ·  │
   │  evening verdict · NET-of-cost scorecard · regime gauge  │
   └─────────────────────────────────────────────────────────┘
```

All three share **one SQLite database: `~/openalgo/trading.db`** (plus SAGE has
its own `~/openalgo/sage.db` for learned artifacts).

---

## 3. Folder map — what each folder does

| Folder | Lines | What it is |
|---|---|---|
| `NSE/` | 7,227 | **The archive.** The original strategy lineage — where it all started. Kept for history; not run anymore. |
| `PIT/` | 7,801 | **The analytics engine.** Post-market analysis: loss autopsy, what-if filter sims, edge-decay, backtests, reports. Reads `trading.db`. |
| `PIT_coco/` | 8,245 | A parallel/experimental copy of PIT (adds `filter_audit.py`). Same analytics, a variant sandbox. |
| `PIT_codex_fixes/` | 10,004 | **The live engines** we actually run day-to-day: Scalper V5 / V5.1 + Trend Rider V2, plus a copy of the PIT analytics. **This is the production folder.** |
| `PIT_v2/` | 5,110 | **Next-gen experimental engines** (built 2026-07-02): Trend Rider V3, Scalper V5.2. Isolated so if they break, the originals in `PIT_codex_fixes/` are untouched. |
| `SAGE/` | 2,260 | **The intelligence layer.** Morning brief, similarity memory, pattern registry, evening verdict, net-of-cost scorecard, regime gauge. |
| `NEWPIT/` | new | **The Capture Engine** (built 2026-07-02) — a brand-new lens: measures what the market OFFERED (Σ MFE) vs what you KEPT (Σ P&L). Found the book captures only 5% of the ₹117k offered; the whole problem is exits, not entries. `uv run python3 newpit.py`. |
| `MINT/` | new | **The post-PIT engine** (built 2026-07-02) — successor strategy designed from the evidence: regime gate (won't trade chop) + pattern gate (consults SAGE's playbook live, blocks TRAPs) + **capture ratchet** exit (an armed winner cannot round-trip into a loss) + budget governor (4/day, DB-seeded). Replay on 197 historical trades: −₹14,718 net → **+₹10,110 net** (capture −4.6% → 24.6%). `MINT/run_mint.sh`. |
| `EQUITY_DASHBOARD/` | new | **Live market heatmap** (built 2026-07-07) — read-only, no strategy code, no orders. Finviz-style squarified treemap: tile size = market weight, tile color = live % change from today's open (gradient, pale→deep). Shows "Our Arsenal" (the 20 stocks `trend_rider_v2.py` trades) pinned on top, full NIFTY 100 below with those 20 outlined. Own Flask server on port 5051, polls OpenAlgo `/quotes` every 20s; frontend polls every 12s and repaints in place — never reloads. `EQUITY_DASHBOARD/run_dashboard.sh`. |

> Why several PIT folders? They're iterations. `PIT/` is the base analytics,
> `PIT_coco/` a variant, and `PIT_codex_fixes/` is the one whose *strategies* we
> keep running and fixing. `PIT_v2/` is the clean-room for new ideas.

---

## 4. Strategy versioning — the full genealogy

### 4a. The Scalper family (NIFTY/SENSEX options)

Every version added filters trying to be more selective — until V4.3 became
*over*-filtered and sat out good trends. V5 threw out the target and let winners
ride.

| Version | File | Lives in | What it does / changed |
|---|---|---|---|
| **V1** | `nifty_sensex_scalp.py` | NSE/ | The seed. 1 condition, very fast. |
| **V3** | `production_momentum_scalper.py` | NSE/ | 4 filters. First "production" attempt. |
| **V4.1** | `momentum_scalper_v4.py` | NSE/ | 7 filters. More selective. |
| **V4.2** | `momentum_scalper_v4_2_fixed.py` | NSE/ | Bug fixes on V4.1. |
| **V4.3** | `scalper_pro_v4_3.py` | PIT*/ | 10 filters, ADX 25–35 **band**. The over-filtered one — rejected strong trends as "too extended." |
| **V5** | `scalper_v5_rider.py` | PIT_codex_fixes/ | **The rethink.** Same breakout ENTRY, but RIDE the move: no fixed target, no ADX ceiling, wide trailing stop. Exits on trend-flip / trailing SL / hard time. |
| **V5.1** | `scalper_v51_rider.py` | PIT_codex_fixes/ | V5 + **re-entry guard** (don't chase the same move twice) + entry-funnel diagnostic. |
| **V5.2** | `scalper_v52_rider.py` | PIT_v2/ | V5.1 + **ExcludeZeroMFE** fast-bailout + **per-day trade cap** (reruns can't re-arm the budget). |

### 4b. The Trend Rider family (NIFTY-50 equity)

| Version | File | Lives in | What it does / changed |
|---|---|---|---|
| **V1** | `trend_rider.py` | NSE/ | Original equity trend-follower. |
| **V2** | `trend_rider_v2.py` | PIT_codex_fixes/ | WebSocket-driven, scores NIFTY-50 stocks by trend strength, rides the strongest. **The profitable engine.** Later fixed: nan-ADX guard, PE/short support, OpenAlgo routing. |
| **V3** | `trend_rider_v3.py` | PIT_v2/ | V2 + **cap fix**: daily trade cap 5→8, and quick no-momentum scratches no longer burn a cap slot (they were locking it out of afternoon trends). |

---

## 5. The intelligence layers explained

### PIT (in `PIT/`, `PIT_coco/`, `PIT_codex_fixes/`)
Post-market analytics. Key modules:
- `loss_autopsy.py` — categorises every loss (bad entry, reversal, chop…).
- `whatif.py` — simulates "what if we'd added filter X?" over history.
- `edge_decay.py` — tracks whether the edge is weakening over time.
- `backtest.py` / `report_generator.py` — replays and reports.

### SAGE (in `SAGE/`) — the forward-looking brain
- `sage_enricher.py` — turns each trade into a "story" with a feature vector.
- `sage_similarity.py` — cosine-matches today's setup to similar past trades.
- `sage_patterns.py` — mines the story library into a **pattern registry**
  (EDGE / TRAP / NEUTRAL setups). _(Built 2026-07-02.)_
- `sage_morning_brief.py` — the morning HTML: regime, bias, P&L range, playbook.
- `sage_evening_verdict.py` — grades the morning call against what happened.
- `sage_scorecard.py` — **NET-of-cost P&L**, edge-decay flag, follow-through
  regime gauge. _(Built 2026-07-02 — the most important tool; see §9.)_
- `sage_runner.py` — the single CLI that runs all of the above.

---

## 6. Major changes made (the 2026-06 → 07 work)

| Change | Where | Why |
|---|---|---|
| **nan-ADX guard** | V5, V5.1, V5.2, TR | `nan < threshold` is False in Python → strategy traded blind at the open. Now skips until ADX is valid. |
| **Circuit breaker** | V5 family | Halts new entries after 3 straight losses or a big profit give-back. Stops afternoon-chop bleed. |
| **Stale-quote protective exit** | V5 family | If the quote feed dies for 25s while in a position, exit protectively (never ride blind). |
| **Trend Rider PE/short support** | TR V2 | Removed long-only gate — can now short on bearish days. |
| **Re-entry guard** | V5.1 | Blocks chasing the same move twice (chop insurance). |
| **ExcludeZeroMFE bailout** | V5.2 | Exit fast when a trade never goes green (SAGE's #1 fix). |
| **Per-day trade cap** | V5.2 | Reruns continue the day's budget instead of re-arming a fresh 5. |
| **Trend Rider cap fix** | TR V3 | Cap 5→8 + scratches don't count (it was locking itself out of trends). |
| **SAGE pattern registry** | SAGE | Mines trades into a named EDGE/TRAP playbook. |
| **SAGE scorecard + decay + regime gauge** | SAGE | Net-of-cost truth, auto edge-decay flag, follow-through regime meter. |

Full detail with line numbers: `PIT_codex_fixes/FIXES_AND_TODO.md`.

---

## 7. HOW TO RUN EVERYTHING

**Prerequisite:** OpenAlgo must be running (`uv run app.py` in `~/openalgo`), and
you need the API key. Set it once per terminal:

```bash
export OPENALGO_API_KEY=1c19c83e21ce74f33fc323b1c1af438c35d11ef669cea5215f07212af308e81e
```

> Every strategy also has a ready-made `run_*.sh` script (key baked in) — you can
> just run those instead of typing the commands below. And in VS Code:
> `Cmd+Shift+P → Tasks: Run Task` gives a clickable menu of everything.

### Run a Scalper (pick ONE — they trade the same options)

```bash
# V5 (control)
cd ~/openalgo/PIT_codex_fixes && caffeinate -i uv run python3 scalper_v5_rider.py

# V5.1 (re-entry guard)
cd ~/openalgo/PIT_codex_fixes && caffeinate -i uv run python3 scalper_v51_rider.py

# V5.2 (SAGE fixes — the newest)
cd ~/openalgo/PIT_v2 && caffeinate -i uv run python3 scalper_v52_rider.py
```

### Run the Trend Rider (the earner)

```bash
# V2 (proven)
cd ~/openalgo/PIT_codex_fixes && caffeinate -i uv run python3 trend_rider_v2.py

# V3 (cap fix — the newest)
cd ~/openalgo/PIT_v2 && caffeinate -i uv run python3 trend_rider_v3.py
```

### Run SAGE

```bash
cd ~/openalgo/SAGE
uv run python3 sage_runner.py morning      # before 09:20 — the brief + playbook
uv run python3 sage_runner.py evening      # after 15:30 — grade the day
uv run python3 sage_runner.py enrich       # ingest new trades + refresh patterns
uv run python3 sage_runner.py scorecard    # ⭐ NET P&L + edge-decay + regime gauge
uv run python3 sage_runner.py patterns     # the EDGE/TRAP playbook
```

### Daily routine (the short version)
- **Morning (pre-9:20):** `sage_runner.py morning` → start Trend Rider → start ONE scalper.
- **Evening (post-3:30):** Ctrl+C the engines → `sage_runner.py evening`, then `enrich`, then `scorecard`.

`caffeinate -i` keeps the Mac awake while a script runs (sleep freezes strategies).

### The rules that matter
1. **Run only ONE scalper live at a time** (V5/V5.1/V5.2 trade the same options → double exposure). Paper A/B with several is fine, but their combined P&L is meaningless.
2. **After the circuit breaker fires, do NOT rerun the scalper** (V5.2 enforces this in code; older ones rely on you).
3. **Only the scorecard's NET number counts.** Gross P&L is a fiction (see §9).

---

## 8. How to read the results

- **Live:** each engine prints trade cards to its terminal and to
  `logs/YYYY-MM-DD/<strategy>/trades.log`.
- **Structured:** `logs/<strategy>_master.csv` (every trade) and `trading.db`.
- **The honest summary:** `sage_runner.py scorecard` — net of costs, per engine.
- **The why:** `PIT_codex_fixes/FIXES_AND_TODO.md` and `PIT_v2/README.md`.

Quick DB check of any strategy:
```bash
cd ~/openalgo && uv run python3 - <<'PY'
import sqlite3
c=sqlite3.connect('trading.db')
for s in ('trend_rider','trend_rider_v3','scalper_v5','scalper_v51','scalper_v52'):
    r=c.execute("SELECT COUNT(*),ROUND(SUM(pnl_rs)) FROM trades WHERE strategy=?",(s,)).fetchone()
    print(f"{s:16} {r[0] or 0:4} trades  gross Rs {r[1] or 0:+.0f}")
PY
```

---

## 9. Current state & verdict (2026-07-02)

**The book is GROSS-positive (+₹5,881) but NET-NEGATIVE (−₹10,129).** Trading
costs (₹16,010) are the single biggest line item — overtrading, not bad entries,
is the primary enemy. Only **Trend Rider is net-positive (+₹7,484)**; both
scalpers are net losers.

**AND the edge has decayed.** Trend Rider earned all its profit by June 12
(+₹153/trade); since June 13 it loses (−₹49/trade). Diagnosis: a **regime shift**
— the market stopped offering follow-through (avg MFE collapsed from 6.9pts to
1.0pt). The strategy isn't broken; the trending market that fed it ended. **The
edge is dormant, not dead** — it returns when trends return.

**The one instruction that falls out of all of it:** watch the scorecard's
**follow-through regime gauge**. It reads `CHOP (stand down)` right now. Trade the
earner only when it climbs back above ~3pts. Until then, everything is
observation/paper.

---

## 10. Glossary (key concepts)

- **ORB** — Opening Range Breakout. The high/low of the first few minutes;
  breaking out of it signals a potential trend.
- **ADX** — trend-strength indicator. High = strong trend. The scalpers use a
  floor (must be trending) but V5+ removed the ceiling (don't reject strong trends).
- **EMA9 / EMA21** — fast/slow moving averages. 9 above 21 = bullish.
- **VWAP** — volume-weighted average price; a fair-value line.
- **MFE / MAE** — Maximum Favourable / Adverse Excursion. How far a trade went
  in your favour / against you before closing. **MFE is the regime tell** — high
  MFE = market offers moves; low MFE = chop.
- **Capture %** — how much of the MFE you actually kept (exit vs peak).
- **Circuit breaker** — auto-halt after N losses or a profit give-back.
- **Regime** — trending vs choppy market. The single biggest driver of P&L.
- **Net vs Gross** — Gross ignores brokerage (~₹55/round-trip). Net is the truth.

---

## 11. Scale

| Metric | Count |
|---|---|
| Pure Python | 33,420 lines |
| Code + docs + configs (6 folders) | 54,650 lines |
| Including all trade logs | 169,458 lines |
| Strategy versions built | 8 scalper + 3 trend rider |
| Trades logged (all engines) | ~284 across 24 days |
| Databases | `trading.db` (trades), `sage.db` (learned) |

---

*End of KT. For the "why" behind any decision, see `PIT_codex_fixes/FIXES_AND_TODO.md`
(engineering log), `PIT_v2/README.md` (the new engines), and `RUN.md` (the daily
runbook). SAGE memory of the whole journey lives in the project's memory files.*
