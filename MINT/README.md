# MINT — the post-PIT engine

**Built 2026-07-02. The successor to the PIT strategy family — the first engine
designed top-down from the evidence instead of bottom-up from indicators.**

## The design law

> **Never give back a gift — and don't trade when there are no gifts.**

Every PIT-era engine (V4.3, V5, V5.1, V5.2) shared the same DNA: find a breakout,
enter, hope. MINT is built the other way around, from the four things 275 real
trades proved:

| Evidence | MINT's answer |
|---|---|
| NEWPIT: 5% capture, `SL Hit` exits ran −178% — winners round-tripped into losses | **Capture Ratchet** — once peak profit reaches the arm threshold, a floor locks at 50% of peak and only rises. An armed winner *mathematically cannot* become a loser. |
| Regime: every engine bled in chop; avg-MFE is the leading gauge | **Regime Gate** — reads rolling follow-through from `trading.db`; below 3 pts avg MFE it refuses to trade at all. Would have sat out 24/25/30-Jun entirely. |
| SAGE registry: high-ADX longs are 0–46% WR TRAPs; some shorts are 57–67% EDGEs | **Pattern Gate** — every entry is checked against the learned playbook *live*. Registered TRAP → blocked. The first strategy here that consults its own history before trading. |
| Costs: ₹55/RT; 20-trade days pay ₹1,100 before P&L | **Budget Governor** — 4 trades/day (seeded from the DB, so reruns can't re-arm it), halt after 2 straight losses, and an entry must have pattern expectancy > 2× cost. |

## The proof (replayed on your own data)

Same entries as the historical scalpers, MINT's exits swapped in
(197 index-option trades):

| | Gross | Net (₹55/RT) | Capture |
|---|---|---|---|
| Actual exits | −₹3,883 | **−₹14,718** | −4.6% |
| **MINT ratchet** | +₹20,945 | **+₹10,110** | **24.6%** |

Breakeven needs ~13% capture; the ratchet replays at 24.6%. The ratchet's core
guarantee is also unit-tested: on a spike-then-crash path (+20 pts peak → −20
crash) the old engines exited −20; MINT exits **+9**.

## Honesty clause (read this)

**Nothing is a guaranteed money printer.** The replay is an approximation — it
assumes exits fill near the floor; gaps and slippage will take a bite, and a
ratchet that never gives back also exits some runners early. MINT is paper-mode
by default (OpenAlgo sandbox). Judge it over 10+ sessions on ONE metric:
**capture %** (`cd ~/openalgo/NEWPIT && uv run python3 newpit.py --strategy mint`).

And note what "trustworthy" means here: as of 2026-07-02 the regime gauge reads
CHOP, so **MINT will refuse to enter anything**. A day of printing nothing beats
a day of printing losses — that refusal is the feature, not a bug.

## Run

```bash
export OPENALGO_API_KEY=<64-hex key>
cd ~/openalgo/MINT && caffeinate -i uv run python3 mint.py
```
Or `./run_mint.sh`, or VS Code task **"🖨 MINT — the post-PIT engine"**.

Logs as strategy `mint` → `logs/YYYY-MM-DD/mint/`, `logs/mint_master.csv`,
`trading.db` (SAGE and NEWPIT pick it up automatically).

Do NOT run MINT and another scalper live at the same time (same index options).

## Anatomy (mint.py)

- `RegimeGate` — organ 1, rolling avg-MFE from `trading.db`, re-checks each 15 min, fails CLOSED
- `pattern_gate()` — organ 2, looks up `(adx band × time slot × direction)` in `sage.db pattern_registry`
- capture-ratchet monitor loop — organ 3, plus zero-MFE 90s bailout and stale-quote protection
- `Governor` — organ 4, DB-seeded daily budget + consecutive-loss halt + edge-vs-cost check

Every knob is a named constant at the top of the file.
