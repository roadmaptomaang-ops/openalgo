"""
Smoke test for ORGAN 5 (bleed guard) — check_bleed_guard() in mint.py.

Market's closed right now, so this drives the real trigger function against
synthetic tick sequences instead of live data. Run:
    cd ~/openalgo/MINT && OPENALGO_API_KEY=dummy uv run python3 test_bleed_guard.py

(OPENALGO_API_KEY needs to be set to *anything* just to import mint.py —
scalper_pro_v4_3.py checks for it at module load time even though nothing
in this test makes a network call.)
"""
import sys
from collections import deque
from datetime import datetime, timedelta

sys.path.insert(0, ".")
from mint import (  # noqa: E402
    check_bleed_guard,
    BLEED_MIN_HOLD_SECS, BLEED_WINDOW_SECS, BLEED_ATR_MULT,
    BLEED_GIVEBACK_FRAC, BLEED_CONFIRM_POLLS,
)

T0 = datetime(2026, 7, 20, 10, 0, 0)
ARM_PTS = 6.0   # NIFTY-style arm threshold


def run_ticks(ticks, atr):
    """ticks: list of (seconds_since_entry, pts). Feeds check_bleed_guard()
    tick by tick, exactly as the live loop would, and returns the first
    (sec, kind) where BLEED_CONFIRM_POLLS was reached, or None."""
    vel_hist = deque()
    mfe = peak_val = 0.0
    peak_time = T0
    confirm = 0
    for sec, pts in ticks:
        now = T0 + timedelta(seconds=sec)
        if pts > mfe:
            mfe = pts; peak_val = pts; peak_time = now
        held = sec
        vel_hist.append((now, pts))
        while vel_hist and (now - vel_hist[0][0]).total_seconds() > BLEED_WINDOW_SECS:
            vel_hist.popleft()
        signal, kind = check_bleed_guard(vel_hist, pts, held, mfe, peak_val,
                                          peak_time, atr, ARM_PTS, now, T0)
        confirm = confirm + 1 if signal else 0
        if confirm >= BLEED_CONFIRM_POLLS:
            return sec, kind
    return None


results = []


def check(name, ticks, atr, expect):
    got = run_ticks(ticks, atr)
    fired = got is not None
    ok = fired == (expect is not None) and (not fired or got[1] == expect)
    results.append(ok)
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name}: expected={expect!r} got={got!r}")


# ── Scenario A: fast adverse velocity, no favorable move ever ──
# ATR=10 -> trigger needs adverse_move >= 6.0pts inside a 30s window.
# Ticks 5s apart: flat at 0 for a while (building window + clearing min-hold),
# then drops 8pts across two 5s ticks — should fire "velocity".
check(
    "A) fast adverse velocity (no MFE)",
    ticks=[(0, 0), (5, 0), (10, 0), (15, 0), (20, -1), (25, -8), (30, -9)],
    atr=10.0,
    expect="velocity",
)

# ── Scenario B: same size drop but spread slowly over 5 minutes -> no fire ──
check(
    "B) slow bleed, same total move, should NOT fire",
    ticks=[(t, -8 * t / 300) for t in range(0, 305, 15)],
    atr=10.0,
    expect=None,
)

# ── Scenario C: giveback — arms at +6pts, then reverses fast ──
# Peak reached at t=60 (60s to build). Giveback of >=3pts (50% of 6) must
# happen in under 60s to fire "giveback".
check(
    "C) fast giveback after arming",
    ticks=[(0, 0), (20, 3), (40, 5), (60, 7), (70, 4), (80, 2.5), (90, 2.0)],
    atr=100.0,  # deliberately huge so trigger A can't fire, isolating trigger B
    expect="giveback",
)

# ── Scenario D: arms, gives back slowly over a long hold -> no fire ──
# (giveback_secs must be < build_secs to fire; here it's much slower)
check(
    "D) slow giveback after arming, should NOT fire",
    ticks=[(0, 0), (20, 3), (40, 5), (60, 7)] +
          [(60 + t, 7 - 3.5 * t / 600) for t in range(0, 605, 30)],
    atr=100.0,
    expect=None,
)

# ── Scenario E: healthy winning ratchet trade, should never fire ──
check(
    "E) clean winner ratcheting up, should NOT fire",
    ticks=[(t, min(20.0, t * 0.15)) for t in range(0, 200, 5)],
    atr=10.0,
    expect=None,
)

# ── Scenario F: min-hold gate — fast drop but inside BLEED_MIN_HOLD_SECS ──
check(
    "F) fast drop before min-hold elapses, should NOT fire",
    ticks=[(0, 0), (3, -3), (6, -7), (9, -9)],
    atr=10.0,
    expect=None,
)

# ── Scenario G: single-tick spike then recovery — confirm-gate should stop it ──
check(
    "G) one bad tick then recovery, confirm-gate should suppress",
    ticks=[(0, 0), (15, 0), (20, 0), (25, -7), (30, -1), (35, 0), (40, 1)],
    atr=10.0,
    expect=None,
)

print()
print(f"config: min_hold={BLEED_MIN_HOLD_SECS}s window={BLEED_WINDOW_SECS}s "
      f"atr_mult={BLEED_ATR_MULT} giveback_frac={BLEED_GIVEBACK_FRAC} "
      f"confirm_polls={BLEED_CONFIRM_POLLS}")
passed = sum(results)
print(f"{passed}/{len(results)} scenarios passed")
sys.exit(0 if passed == len(results) else 1)
