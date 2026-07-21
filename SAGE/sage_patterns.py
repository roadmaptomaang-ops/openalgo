"""
sage_patterns.py — Pattern Miner
=================================
Turns the raw Trade Story library into a registry of named, persistent
patterns — the "playbook" layer that was scaffolded (pattern_registry
table) but never populated.

It buckets every story by interpretable, actionable features:

    strategy_group  ·  ADX band  ·  time-of-day slot  ·  direction

For each bucket with enough samples it computes win rate, avg P&L, MFE,
hold time and profit factor, classifies the bucket as an EDGE / TRAP /
NEUTRAL, and upserts it into pattern_registry keyed on a stable
pattern_id (so re-running refreshes rather than duplicates).

The morning brief already reads `pattern_registry WHERE status='ACTIVE'`,
so once this runs the brief surfaces the learned playbook automatically.

Run:
    uv run python3 sage_patterns.py                 # mine + print
    uv run python3 sage_runner.py patterns          # same, via orchestrator
    (also called automatically at the end of `sage_runner.py enrich`)
"""

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from sage_db import get_sage_db
from sage_enricher import _safe_float, _parse_time

# ── tuning ────────────────────────────────────────────────
MIN_SAMPLES   = 20     # a bucket needs at least this many trades to register
                        # (raised from 5 on 2026-07-17 — an 8-trade "12% WR TRAP"
                        # blocked a real winner same day; 5 trades isn't enough
                        # signal to blacklist a pattern, it's noise wearing a badge)
EDGE_WR       = 0.55   # win rate at/above this + positive avg P&L = EDGE
TRAP_WR       = 0.35   # win rate at/below this (or negative avg P&L & WR<0.5) = TRAP


# ── bucketing helpers ─────────────────────────────────────
def _adx_band(adx: float) -> tuple[str, tuple]:
    a = _safe_float(adx, 0.0)
    if a < 20:  return "choppy",   (0, 20)
    if a < 30:  return "moderate", (20, 30)
    if a < 45:  return "strong",   (30, 45)
    return "vstrong", (45, 999)


def _time_slot(entry_time: str) -> tuple[str, tuple]:
    t = _parse_time(entry_time)
    if t < 10.0:   return "open",      (9.25, 10.0)
    if t < 11.5:   return "morning",   (10.0, 11.5)
    if t < 13.25:  return "midday",    (11.5, 13.25)
    return "afternoon", (13.25, 15.5)


def _dir_group(direction: str) -> str:
    if direction in ("CE", "BUY"):
        return "LONG"
    if direction == "PE":
        return "SHORT"
    return "OTHER"


def _strategy_group(strategy: str) -> str:
    """BUG FIX 2026-07-08: this only matched strategy=='trend_rider' exactly and
    strategy.startswith('scalper') — so 'mint' (and 'trend_rider_v3', the PIT_v2
    variant) silently fell through to 'other' and got bucketed under an orphaned
    'OTH-*' pattern family that nothing reads. Result: MINT's pattern_gate() was
    checking 'OPT-*' patterns built ONLY from the scalper engines' history —
    MINT's own trades never fed back into its own pattern lookups. Broke the
    entire "consult your own history live" design promise since day 1."""
    if not strategy:
        return "other"
    if strategy.startswith("trend_rider"):
        return "equity"
    if strategy.startswith("scalper") or strategy == "mint":
        return "options"
    return "other"


def _classify(win_rate: float, avg_pnl: float) -> tuple[str, str, str]:
    """Return (recommendation, status, tag)."""
    if win_rate >= EDGE_WR and avg_pnl > 0:
        return "ENTER", "ACTIVE", "EDGE"
    if win_rate <= TRAP_WR or (avg_pnl < 0 and win_rate < 0.5):
        return "SKIP", "ACTIVE", "TRAP"
    return "CAUTION", "WATCH", "NEUTRAL"


# ── miner ─────────────────────────────────────────────────
def mine_patterns(verbose: bool = True) -> list[dict]:
    db = get_sage_db()
    stories = db.query(
        "SELECT * FROM trade_stories WHERE strategy != 'test' OR strategy IS NULL"
    )
    buckets: dict = {}
    for s in stories:
        sg = _strategy_group(s.get("strategy"))
        if sg == "other":
            continue
        dg = _dir_group(s.get("direction"))
        if dg == "OTHER":
            continue
        ab, arange = _adx_band(s.get("adx_entry"))
        ts, trange = _time_slot(s.get("entry_time"))
        key = (sg, ab, ts, dg)
        buckets.setdefault(key, {"stories": [], "arange": arange, "trange": trange})
        buckets[key]["stories"].append(s)

    registered = []
    for (sg, ab, ts, dg), data in buckets.items():
        rows = data["stories"]
        n = len(rows)
        if n < MIN_SAMPLES:
            continue

        pnls  = [_safe_float(r.get("pnl_rs")) for r in rows]
        mfes  = [_safe_float(r.get("mfe_pts")) for r in rows]
        holds = [int(r.get("hold_seconds") or 0) for r in rows]
        wins  = [p for p in pnls if p > 50]
        losses = [p for p in pnls if p < -50]
        gross_w = sum(wins)
        gross_l = abs(sum(losses))

        win_rate = len(wins) / n
        avg_pnl  = sum(pnls) / n
        avg_mfe  = sum(mfes) / n
        avg_hold = int(sum(holds) / n)
        pf = (gross_w / gross_l) if gross_l > 0 else (99.9 if gross_w > 0 else 0.0)

        rec, status, tag = _classify(win_rate, avg_pnl)

        pattern_id = f"{sg[:3].upper()}-{ab.upper()}-{ts.upper()}-{dg}"
        name = f"{sg.title()} {dg.lower()} · {ab} ADX · {ts}"
        desc = (f"{tag}: {n} trades, {win_rate*100:.0f}% WR, "
                f"avg Rs {avg_pnl:+.0f}, PF {pf:.2f} → {rec}")
        feature_profile = {
            "strategy_group": sg,
            "adx_band": ab, "adx_range": list(data["arange"]),
            "time_slot": ts, "time_range": list(data["trange"]),
            "direction": dg,
            "recommendation": rec, "tag": tag,
        }

        db.upsert_pattern(pattern_id, {
            "name": name,
            "description": desc,
            "story_ids": json.dumps([r["id"] for r in rows]),
            "trade_count": n,
            "win_rate": round(win_rate, 3),
            "avg_pnl_rs": round(avg_pnl, 0),
            "avg_mfe": round(avg_mfe, 2),
            "avg_hold_sec": avg_hold,
            "profit_factor": round(pf, 2),
            "feature_profile": json.dumps(feature_profile),
            "status": status,
        })
        registered.append({
            "pattern_id": pattern_id, "name": name, "n": n,
            "win_rate": win_rate, "avg_pnl": avg_pnl, "pf": pf,
            "rec": rec, "tag": tag, "status": status,
        })

    registered.sort(key=lambda x: (x["tag"] != "EDGE", x["tag"] != "TRAP", -abs(x["avg_pnl"])))

    if verbose:
        print(f"\n{'='*72}")
        print(f"  SAGE PATTERN REGISTRY  ·  {len(registered)} patterns mined "
              f"from {len(stories)} stories  (min {MIN_SAMPLES}/bucket)")
        print(f"{'='*72}")
        print(f"  {'pattern_id':32} {'n':>3} {'WR':>5} {'avgP&L':>8} {'PF':>5}  {'verdict'}")
        for p in registered:
            mark = "✅" if p["tag"] == "EDGE" else ("⛔" if p["tag"] == "TRAP" else "· ")
            print(f"  {mark} {p['pattern_id']:30} {p['n']:>3} "
                  f"{p['win_rate']*100:>4.0f}% {p['avg_pnl']:>+8.0f} {p['pf']:>5.2f}  {p['rec']}")
        edges = [p for p in registered if p['tag'] == 'EDGE']
        traps = [p for p in registered if p['tag'] == 'TRAP']
        print(f"\n  {len(edges)} EDGE (ACTIVE) · {len(traps)} TRAP (ACTIVE) · "
              f"{len(registered)-len(edges)-len(traps)} NEUTRAL (WATCH)")
    return registered


if __name__ == "__main__":
    mine_patterns(verbose=True)
