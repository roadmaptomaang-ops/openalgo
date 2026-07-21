# PURSE — capital-aware single-position options trader

**Built 2026-07-11.** The one engine in this repo that trades a **real, finite
wallet** the way you'd actually run ₹15–20k — every other engine assumes the
~₹1 Crore sandbox balance. Its distinctive organ is the **ledger**, not the
signal.

Isolation: label **`purse`** → `logs/YYYY-MM-DD/purse/`, `logs/purse_master.csv`,
`trading.db` (SAGE ingests automatically). Wallet persists in `PURSE/ledger.json`;
an open position survives restarts via `PURSE/position.json`.

## The rules you asked for

1. **One position at a time.** When money is in a trade it's locked — nothing
   else can enter until it exits. (At this account size it's also just
   arithmetic: one lot is most of the wallet.)
2. **Price the trade before entering.** Computes premium × lot + round-trip
   cost and refuses anything it can't afford from the **current** balance.
   As the wallet grows/shrinks, what it can afford moves with it.
3. **Auto-picks instrument + strike to fit the wallet.** Tries SENSEX then
   NIFTY (BANKNIFTY optional/off — weekly options discontinued), stepping a
   few strikes OTM *only as far as needed* to fit the budget. Refuses to reach
   past 3 strikes OTM or below ₹20 premium — no lottery tickets.
4. **₹2,000 hard stop per trade** (~10% of a 20k account). Because position
   size is forced, the STOP is the real risk control. Then break-even at +1R,
   trail at 50% of peak after +2R, time-exit 15:15.

## Honest scope — read before judging

- The **signal is a deliberate placeholder** (ORB breakout + EMA/ADX confirm)
  behind a clean `Signal` class. You chose "build the edge later" — so this
  build proves the **money-management layer**, not an edge. Swap
  `Signal.evaluate` without touching the ledger. Expect the placeholder itself
  to be roughly break-even-minus-costs; that's fine, it's the harness.
- **The stop is a trigger, not a guaranteed fill.** Market-order slippage +
  ₹55 cost means a −₹2,000 stop books ≈ −₹2,100. Verified in testing.
- **Concentration is the standing risk.** Full-lot-per-trade means one bad
  stop is ~10% of the account by design. The ₹2,000 cap bounds it; a losing
  streak still compounds. Watch the ledger's drawdown, not just per-trade P&L.

## Run

```bash
cd ~/openalgo/PURSE && ./run_purse.sh
# optional: PURSE_CAPITAL=15000 ./run_purse.sh   to start with a different wallet
```

Validation: ledger math, affordability gate (fits / steps OTM / refuses when
broke), anti-lottery min-premium filter, and all three exit paths (stop,
break-even, trail ratchet) unit-tested offline. Live option-chain quotes and
real fills untested — app was down at build; first paper day confirms.
Watch `ledger.json` — that running balance IS the scorecard for this engine.
