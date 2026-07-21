"""
sage_verdict.py — The one-screen daily answer
==============================================
Combines everything SAGE knows into a single GO / CAUTION / NO-GO call:
  · Is the book making money NET of costs?
  · Is the market in a tradeable regime (follow-through health)?
  · Has the edge decayed?

Run:
    uv run python3 sage_runner.py verdict
    uv run python3 sage_verdict.py

This is the command to check each morning. It answers "should I trade today?"
in plain English so you don't have to interpret three separate reports.
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from sage_scorecard import scorecard, follow_through_health, _kind, COST_PER_ROUNDTRIP


def _pick_earner(sc: dict) -> str | None:
    for k in ("trend_rider_v3", "trend_rider"):
        if k in sc and sc[k]["trades"] >= 10:
            return k
    return None


def verdict(verbose: bool = True) -> dict:
    sc = scorecard(verbose=False)
    if not sc:
        print("No data yet."); return {}
    decay = sc.get("_DECAY", {})
    earner = _pick_earner(sc)

    # regime gauge on the earner (follow-through health)
    regime = follow_through_health(earner, verbose=False) if earner else {}
    regime_state = regime.get("state", "UNKNOWN")
    regime_mfe = regime.get("recent_mfe", 0)

    # is the earner's edge currently decayed?
    edge_decayed = bool(earner and decay.get(earner, {}).get("decayed"))

    # overall book net
    total = sc.get("_TOTAL", {})
    book_net = total.get("net", 0)

    # ── decide ────────────────────────────────────────────────
    # GO only if the market is offering moves AND the earner's edge isn't flagged decayed
    tradeable = regime_mfe >= 3 and not edge_decayed
    weak = 2 <= regime_mfe < 3
    if tradeable:
        call, colour = "GO", "trade the earner (Trend Rider)"
    elif weak:
        call, colour = "CAUTION", "half size on the earner only; skip the scalper"
    else:
        call, colour = "NO-GO", "stand down — paper/observe only, do not trade live"

    reasons = []
    if earner:
        reasons.append(f"Regime (follow-through) on {earner}: {regime_mfe:.1f}pts avg MFE "
                       f"→ {regime_state}")
    if edge_decayed:
        d = decay[earner]
        reasons.append(f"Edge DECAYED: was {d['early_npt']:+.0f}/trade, now "
                       f"{d['late_npt']:+.0f}/trade — profit is a fossil")
    reasons.append(f"Book net of costs (all history): Rs {book_net:+,.0f}")
    if book_net < 0 < total.get("gross", 0):
        reasons.append(f"⚠ Gross +Rs{total['gross']:,.0f} but NET Rs{book_net:,.0f} "
                       f"— costs (Rs{total['cost']:,.0f}) are the enemy; trade less, not more")

    if verbose:
        bar = "=" * 60
        print(f"\n{bar}")
        print(f"  SAGE VERDICT  —  should you trade today?")
        print(f"{bar}")
        print(f"\n     ►  {call}  ·  {colour}\n")
        print(f"  Why:")
        for r in reasons:
            print(f"    · {r}")
        print(f"\n  Rule of thumb: trade the earner only when follow-through avg MFE")
        print(f"  climbs above ~3pts. Right now it's {regime_mfe:.1f}. "
              f"{'✅' if regime_mfe>=3 else '⛔'}")
        print(f"{bar}\n")

    return {"call": call, "regime_mfe": regime_mfe, "edge_decayed": edge_decayed,
            "book_net": book_net, "reasons": reasons}


if __name__ == "__main__":
    verdict(verbose=True)
