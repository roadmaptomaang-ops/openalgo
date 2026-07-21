# RUN — the simplest possible guide

Just follow the steps. Copy a block, paste it into a terminal, press Enter.

To open a terminal in VS Code: menu **Terminal → New Terminal** (or `` Ctrl+` ``).
Each engine needs its **own** terminal tab. Click the **+** in the terminal panel
to open another tab.

---

> **Daily lineup (as of 2026-07-20):** Trend Rider V2 + Scalper V5.1 + MINT +
> MINT-E + SWING + THETA (+ PURSE, real-wallet, run separately — see below).
> That's **6 engines total**, all sharing one Upstox connection through
> `app.py`. Scalper V5 is retired. Older iterations (NEWPIT, PIT, PIT_coco,
> PIT_v2) are superseded and moved to `_archive/strategies/` — don't run them.
> MINT, MINT-E, and V5.1 all trade index/equity — fine in paper for
> comparison, but never read their combined P&L as one number.
> MINT also has a **Bleed Guard** (added 2026-07-20) — a velocity-based
> accelerated exit that fires faster than its fixed stop on a fast adverse
> move or fast giveback. Watch `activity.log` for `BLEED GUARD` lines and
> the `Bleed Guard` exit_reason in trade cards while it's still being proven out.

> ⚠️ **Known issue (2026-07-20):** running all 6 engines at once, especially
> when PURSE/MINT/Scalper V5.1 are all actively churning the same NIFTY/SENSEX
> strikes, can trip Upstox's rate limit (`UDAPI10005 "Too Many Request Sent"`).
> When that happens an engine can go silent for an extended period (seen:
> `mint_e`/`trend_rider` and separately `purse` each went quiet for ~1-1.5hrs
> on 07-20) while the process stays alive, just stuck retrying. It self-recovers
> once the rate-limit window clears, but you lose that stretch of trading.
> Check `log/errors.jsonl` for a burst of `Too Many Request Sent` if an engine
> goes quiet — that's the tell vs. an actual crash (see Troubleshooting below).

## ⚡ Fast path (2026-07-17)

`start_all.sh` launches the 5 daemons + runs SWING in one shot instead of
5+ manual terminal tabs. `watchdog.sh` restarts anything that dies mid-day
(built after 07-16, when 4 engines died in sync at 13:34 and the rest of
the day was lost silently).

```bash
export OPENALGO_API_KEY=...
cd ~/openalgo && ./start_all.sh          # morning
cd ~/openalgo && ./watchdog.sh --loop &  # optional, keeps checking every 5 min
```

PURSE stays opt-in (real capital sizing) — start it manually per Step 4
below even when using the fast path. The manual step-by-step morning
routine still works and is documented below for when you want to start
engines one at a time.

## ☀️ MORNING — do this before 9:20 AM

### Step 0 — should you even trade today? (Terminal 1)
```bash
cd ~/openalgo/SAGE && uv run python3 sage_runner.py verdict
```
One screen, one answer: **GO / CAUTION / NO-GO**. If it says NO-GO, the market is
in chop and the edge is dormant — just observe today, don't bother starting engines
for real conviction. (As of Jul 2 it says NO-GO.)

### Step 1 — the SAGE brief (same terminal)
```bash
cd ~/openalgo/SAGE && uv run python3 sage_runner.py morning
```
This opens today's plan in your browser. Read it. Done — you can reuse this terminal.

### Step 2 — start the Trend Rider (Terminal 2)
Open a NEW terminal tab (+), then paste:
```bash
export OPENALGO_API_KEY=1c19c83e21ce74f33fc323b1c1af438c35d11ef669cea5215f07212af308e81e
cd ~/openalgo/PIT_codex_fixes && caffeinate -i uv run python3 trend_rider_v2.py
```
Leave this running. Do NOT close the tab.

### Step 3 — start ONE scalper (Terminal 3)
Open a NEW terminal tab (+), then paste:
```bash
export OPENALGO_API_KEY=1c19c83e21ce74f33fc323b1c1af438c35d11ef669cea5215f07212af308e81e
cd ~/openalgo/PIT_codex_fixes && caffeinate -i uv run python3 scalper_v51_rider.py
```
Leave this running too.

### Step 4 — start MINT (Terminal 4)
```bash
export OPENALGO_API_KEY=1c19c83e21ce74f33fc323b1c1af438c35d11ef669cea5215f07212af308e81e
cd ~/openalgo/MINT && ./run_mint.sh
```

### Step 5 — start MINT-E (Terminal 5)
```bash
export OPENALGO_API_KEY=1c19c83e21ce74f33fc323b1c1af438c35d11ef669cea5215f07212af308e81e
cd ~/openalgo/MINT_E && ./run_mint_e.sh
```

### Step 6 — start THETA (Terminal 6)
```bash
export OPENALGO_API_KEY=1c19c83e21ce74f33fc323b1c1af438c35d11ef669cea5215f07212af308e81e
cd ~/openalgo/THETA && ./run_theta.sh
```

### Step 7 — run SWING (Terminal 1, one-shot, not a daemon)
Daily-close strategy — runs once and exits in under a minute, no need to leave
a tab open for it:
```bash
cd ~/openalgo/SWING && ./run_swing.sh
```

**That's the whole morning.** Five engines running (Trend Rider, V5.1, MINT,
MINT-E, THETA), SWING run once, brief read.

PURSE is optional and separate — it trades real capital sizing against a
₹15-20k wallet, not paper. Only start it deliberately:
```bash
export OPENALGO_API_KEY=1c19c83e21ce74f33fc323b1c1af438c35d11ef669cea5215f07212af308e81e
cd ~/openalgo/PURSE && ./run_purse.sh
```

---

## 🌆 EVENING — do this after 3:30 PM

### Step 1 — stop the engines
Preferred: `cd ~/openalgo && ./squareoff.sh` — tells every running engine to
close its own open positions via its own exit logic (records the trade card +
DB row properly) and wind down within ~5-10s.

Otherwise, click each engine's terminal tab and press `Ctrl+C`.

### Step 2 — run the SAGE wrap-up (any terminal)
```bash
cd ~/openalgo/SAGE && uv run python3 sage_runner.py evening
cd ~/openalgo/SAGE && uv run python3 sage_runner.py enrich
cd ~/openalgo/SAGE && uv run python3 sage_runner.py scorecard
```
The last one — **scorecard** — is the important number: your real (net) P&L.

**That's the whole evening.**

---

## If you want to run a DIFFERENT engine

Same as above, just swap the last line:

| To run… | Use this line |
|---|---|
| ~~Scalper V5~~ | **RETIRED 2026-07-07** (net −₹13,716 over 132 trades — served as the A/B control arm; V5.1/MINT superseded it) |
| Scalper V5.1 | `cd ~/openalgo/PIT_codex_fixes && caffeinate -i uv run python3 scalper_v51_rider.py` |
| Trend Rider V2 | `cd ~/openalgo/PIT_codex_fixes && caffeinate -i uv run python3 trend_rider_v2.py` |
| MINT (post-PIT engine) | `cd ~/openalgo/MINT && ./run_mint.sh` |
| MINT-E (Trend Rider entries + MINT exits, A/B vs Trend Rider V2) | `cd ~/openalgo/MINT_E && ./run_mint_e.sh` |
| SWING (daily-close, 2-10 day equity holds, one-shot each morning) | `cd ~/openalgo/SWING && ./run_swing.sh` |
| THETA (chop-day iron condor seller, stands down on trend days) | `cd ~/openalgo/THETA && ./run_theta.sh` |
| PURSE (real-capital single-position trader, ₹15-20k wallet — not paper) | `cd ~/openalgo/PURSE && ./run_purse.sh` |
| Equity Dashboard (live heatmap, read-only, no key needed to *view*) | `cd ~/openalgo/EQUITY_DASHBOARD && ./run_dashboard.sh` then open http://127.0.0.1:5051 |

(Always `export OPENALGO_API_KEY=...` first in that terminal — every engine line
above needs the key set once per tab. The Equity Dashboard also needs the main
OpenAlgo app — `uv run app.py` — already running on port 5000, since that's
where it pulls quotes from.)

**Retired/superseded iterations** (Scalper V5.2, Trend Rider V3, NEWPIT, PIT,
PIT_coco) live in `_archive/strategies/` — don't run from there without first
checking why they were shelved.

---

## 3 rules
1. **Only ONE scalper at a time.** (V5, V5.1 and V5.2 all trade the same options.)
2. **If it says "CIRCUIT BREAKER", stop for the day.** Don't restart the scalper.
3. **Only trust the scorecard's NET number.** The other numbers ignore fees.

---

## Something went wrong?
- **"OPENALGO_API_KEY not set"** → you forgot the `export OPENALGO_API_KEY=...`
  line in that terminal. Paste it, then re-run.
- **"No such file or directory"** → you're in the wrong folder. The `cd ...` part
  of the command handles this — make sure you pasted the whole block.
- **"command not found: caffeinate"** → drop the `caffeinate -i ` from the front;
  it still runs (just keep the Mac from sleeping manually).
- **Engine prints nothing / stuck on "Not enough candles"** → the market is
  closed (weekend/holiday) or just opened. Wait, or try during market hours.
- **Engine's `activity.log`/`trades.log` has gone silent for many minutes but
  the process is still running** → check `grep "Too Many Request" log/errors.jsonl`
  for a burst around the same time. That's Upstox rate-limiting the shared
  connection (all 6 engines poll through one broker session) — the engine is
  alive, just stuck retrying. It self-recovers; no restart needed, but you do
  lose that window of trading. If there's no rate-limit burst and it's still
  silent after 10+ min, it's a real hang — restart it (`./watchdog.sh` does
  this automatically, or `Ctrl+C` the tab and re-run its start command).
- **Dashboard "Open Positions" shows more rows / a bigger loss than expected**
  → the position book is shared across ALL engines per symbol, not per-strategy
  — two engines trading the same strike blend into one row. Also check for a
  stale row (an option position that's fully closed in `sandbox_orders` but
  still shows open qty) — a known race condition in `sandbox_positions` when
  two engines' fills land within milliseconds of each other. Don't trust the
  dashboard total without cross-checking `trading.db`/`sandbox_orders` if a
  number looks off.
