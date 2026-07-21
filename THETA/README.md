# THETA — chop-day defined-risk premium seller

**Built 2026-07-11.** The inverse of every other engine in the book: instead of
buying movement and losing on the 60–70% of days that are chop, THETA **sells**
that premium on days its classifier marks as chop, via a defined-risk NIFTY
iron condor. It earns from time decay, not from predicting direction — the
regime that kills MINT/V5.1/Trend Rider is exactly the regime that pays THETA.

Isolation: strategy label **`theta`** → `logs/YYYY-MM-DD/theta/`,
`logs/theta_master.csv`, `trading.db` rows. SAGE/scorecard pick it up
automatically. Touches nothing else.

## How it works

1. **Day-type classifier** (from 10:15, re-checks every 15 min until 13:00):
   1m NIFTY ADX + directional efficiency (|net drift| ÷ range).
   - ADX < 22 **and** efficiency < 0.45 → **CHOP** → deploy
   - ADX ≥ 30 **or** efficiency > 0.60 → **TREND** → stand down for the day
   - otherwise → UNCLEAR → wait and re-check. No CHOP by 13:00 → flat all day.
2. **Structure**: sell CE above the day's high and PE below the day's low
   (range extended 25%, never nearer than 100pts to spot), buy wings 100pts
   beyond each short. Wings placed FIRST, shorts second; any failed leg →
   full unwind. Skips if total credit < 12pts (not worth the risk).
3. **Risk** (structure-level, monitored every 5s):
   - **Profit take**: buy back when cost-to-close ≤ 40% of credit (keep 60%)
   - **Credit stop**: buy back at 175% of credit (max planned loss 75% of credit)
   - **Strike breach**: spot touches either short strike → close everything
   - **Time exit**: 15:12. One condor per day, DB-checked (reruns can't re-enter).

## Expected profile — read this before judging it

Opposite of the other engines: **win rate should be HIGH (60–75%) with
occasional larger losses** on days the classifier is wrong. Do not judge it on
a losing day; judge it on:
- classifier accuracy (did chop days stay range-bound?)
- net P&L per deployed day vs what the long-options engines lost on those days
- whether the stop capped the bad days as designed

Costs: 4 legs ≈ ₹110/RT (2× the single-leg model) — the scorecard's ₹55
default undercounts it; mentally double.

## Run

```bash
cd ~/openalgo/THETA && ./run_theta.sh
```

Validation status: syntax + import + strike-picker + classifier unit tests
pass (synthetic data). Real-data classifier check pending — the OpenAlgo app
was down when built (00:37 IST). First live paper day is the real test;
watch the CLASSIFIER lines in `logs/YYYY-MM-DD/theta/activity.log`.
