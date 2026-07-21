"""
whatif.py — What-If Research Engine
=====================================
Simulates historical results with different parameter values.
Uses only actual trade data from the DB — no synthetic data.

Filters tested:
  - ADX threshold (skip trades below X)
  - Score threshold (skip trades below X)
  - Max trades per day (cap entries)
  - Time window (only trade between hours)
  - SL multiplier (widen/tighten stop)
  - Direction filter (CE only / PE only)

Usage:
    from whatif import run_whatif
    result = run_whatif()
    result = run_whatif(strategy="scalper")
"""

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from analytics_core import (
    load_trades, profit_factor, win_rate, expectancy,
    max_drawdown, bootstrap_analytics_tables, get_db, safe_div
)


def _stats(trades):
    if not trades:
        return {"n": 0, "net_pnl": 0, "win_rate": 0,
                "profit_factor": None, "expectancy": 0, "max_dd": 0}
    return {
        "n":             len(trades),
        "net_pnl":       round(sum(t["pnl_rs"] for t in trades), 0),
        "win_rate":      round((win_rate(trades) or 0) * 100, 1),
        "profit_factor": round(profit_factor(trades), 2) if profit_factor(trades) else None,
        "expectancy":    round(expectancy(trades) or 0, 0),
        "max_dd":        round(max_drawdown(trades), 0),
    }


def _verdict(actual, sim):
    if sim["n"] == 0: return "SKIP"
    delta = sim["net_pnl"] - actual["net_pnl"]
    removed = actual["n"] - sim["n"]
    if delta <= 0: return "SKIP"
    if removed < 2: return "SKIP"
    if delta >= abs(actual["net_pnl"]) * 0.10 or (delta > 200 and removed >= 2):
        return "ADOPT"
    return "TEST"


def _save(run_date, name, param, actual, sim):
    v = _verdict(actual, sim)
    db = get_db()
    try:
        db.execute(
            """INSERT INTO whatif_results
               (run_date,filter_name,filter_param,actual_trades,filtered_trades,
                removed_trades,actual_pnl,sim_pnl,pnl_delta,
                actual_wr,sim_wr,actual_pf,sim_pf,
                actual_expectancy,sim_expectancy,verdict)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_date, name, json.dumps(param),
             actual["n"], sim["n"], actual["n"] - sim["n"],
             actual["net_pnl"], sim["net_pnl"],
             sim["net_pnl"] - actual["net_pnl"],
             actual["win_rate"] / 100, sim["win_rate"] / 100,
             actual["profit_factor"], sim["profit_factor"],
             actual["expectancy"], sim["expectancy"], v)
        )
    except Exception:
        pass
    return v


def run_whatif(strategy=None, instrument=None,
               date_from=None, date_to=None) -> dict:
    bootstrap_analytics_tables()
    trades = load_trades(strategy=strategy, instrument=instrument,
                         date_from=date_from, date_to=date_to)
    if not trades:
        return {"error": "No trades found."}

    actual = _stats(trades)
    today  = datetime.now().strftime("%Y-%m-%d")
    results = []

    # ── 1. ADX threshold filters ──────────────────────────────
    for adx_min in [20, 22, 25, 28, 30]:
        filtered = [t for t in trades if (t.get("adx_entry") or 0) >= adx_min]
        sim = _stats(filtered)
        v   = _save(today, f"ADX≥{adx_min}", {"adx_min": adx_min}, actual, sim)
        results.append({
            "filter":   f"ADX ≥ {adx_min}",
            "category": "Entry Filter",
            "param":    adx_min,
            "removed":  actual["n"] - sim["n"],
            "sim_pnl":  sim["net_pnl"],
            "sim_wr":   sim["win_rate"],
            "sim_pf":   sim["profit_factor"],
            "delta":    sim["net_pnl"] - actual["net_pnl"],
            "verdict":  v,
            "actual":   actual,
            "simulated": sim,
        })

    # ── 2. Score threshold filters ────────────────────────────
    for score_min in [80, 85, 88, 90, 92]:
        filtered = [t for t in trades
                    if (t.get("trend_score") or 0) >= score_min
                    or t.get("trend_score") is None]
        # Only apply if meaningful reduction
        if len(filtered) == len(trades):
            filtered = [t for t in trades if (t.get("trend_score") or 100) >= score_min]
        sim = _stats(filtered)
        v   = _save(today, f"Score≥{score_min}", {"score_min": score_min}, actual, sim)
        results.append({
            "filter":    f"Score ≥ {score_min}",
            "category":  "Entry Filter",
            "param":     score_min,
            "removed":   actual["n"] - sim["n"],
            "sim_pnl":   sim["net_pnl"],
            "sim_wr":    sim["win_rate"],
            "sim_pf":    sim["profit_factor"],
            "delta":     sim["net_pnl"] - actual["net_pnl"],
            "verdict":   v,
            "actual":    actual,
            "simulated": sim,
        })

    # ── 3. Max trades per day ─────────────────────────────────
    for max_t in [1, 2, 3, 4]:
        by_day = defaultdict(list)
        for t in trades:
            by_day[t["date"]].append(t)
        filtered = []
        for day_trades in by_day.values():
            filtered.extend(day_trades[:max_t])
        sim = _stats(filtered)
        v   = _save(today, f"MaxTrades={max_t}", {"max_trades": max_t}, actual, sim)
        results.append({
            "filter":    f"Max {max_t} trade/day",
            "category":  "Frequency Filter",
            "param":     max_t,
            "removed":   actual["n"] - sim["n"],
            "sim_pnl":   sim["net_pnl"],
            "sim_wr":    sim["win_rate"],
            "sim_pf":    sim["profit_factor"],
            "delta":     sim["net_pnl"] - actual["net_pnl"],
            "verdict":   v,
            "actual":    actual,
            "simulated": sim,
        })

    # ── 4. Time window filters ────────────────────────────────
    windows = [
        ("09:20–11:00", "09:20", "11:00"),
        ("09:20–11:30", "09:20", "11:30"),
        ("09:20–12:00", "09:20", "12:00"),
        ("10:00–14:00", "10:00", "14:00"),
    ]
    for label, t_start, t_end in windows:
        filtered = [t for t in trades
                    if t_start <= (t.get("entry_time") or "00:00") <= t_end]
        sim = _stats(filtered)
        v   = _save(today, f"Time {label}", {"start": t_start, "end": t_end}, actual, sim)
        results.append({
            "filter":    f"Entry {label}",
            "category":  "Time Filter",
            "param":     label,
            "removed":   actual["n"] - sim["n"],
            "sim_pnl":   sim["net_pnl"],
            "sim_wr":    sim["win_rate"],
            "sim_pf":    sim["profit_factor"],
            "delta":     sim["net_pnl"] - actual["net_pnl"],
            "verdict":   v,
            "actual":    actual,
            "simulated": sim,
        })

    # ── 5. Direction filters ──────────────────────────────────
    for direction in ["CE", "PE", "BUY"]:
        filtered = [t for t in trades if t.get("direction") == direction]
        if not filtered or len(filtered) == len(trades): continue
        sim = _stats(filtered)
        v   = _save(today, f"Direction={direction}",
                    {"direction": direction}, actual, sim)
        results.append({
            "filter":    f"{direction} only",
            "category":  "Direction Filter",
            "param":     direction,
            "removed":   actual["n"] - sim["n"],
            "sim_pnl":   sim["net_pnl"],
            "sim_wr":    sim["win_rate"],
            "sim_pf":    sim["profit_factor"],
            "delta":     sim["net_pnl"] - actual["net_pnl"],
            "verdict":   v,
            "actual":    actual,
            "simulated": sim,
        })

    # ── 6. Exclude zero-MFE trades ────────────────────────────
    filtered = [t for t in trades if (t.get("mfe_pts") or 0) > 0]
    sim = _stats(filtered)
    v   = _save(today, "ExcludeZeroMFE", {"mfe_min": 0.01}, actual, sim)
    results.append({
        "filter":    "Exclude MFE=0 entries",
        "category":  "Quality Filter",
        "param":     "MFE>0",
        "removed":   actual["n"] - sim["n"],
        "sim_pnl":   sim["net_pnl"],
        "sim_wr":    sim["win_rate"],
        "sim_pf":    sim["profit_factor"],
        "delta":     sim["net_pnl"] - actual["net_pnl"],
        "verdict":   v,
        "actual":    actual,
        "simulated": sim,
    })

    # ── Best recommendation ───────────────────────────────────
    adopt = [r for r in results if r["verdict"] == "ADOPT"]
    best  = max(adopt, key=lambda r: r["delta"]) if adopt else None

    return {
        "module":          "whatif",
        "generated_at":    today,
        "strategy_filter": strategy,
        "actual_stats":    actual,
        "results":         sorted(results, key=lambda r: r["delta"], reverse=True),
        "best_recommendation": best,
        "adopt_count":     len(adopt),
        "test_count":      len([r for r in results if r["verdict"] == "TEST"]),
    }


def _cli():
    import argparse
    p = argparse.ArgumentParser(description="What-If Research Engine")
    p.add_argument("--strategy", type=str, default=None)
    p.add_argument("--instrument", type=str, default=None)
    p.add_argument("--from", dest="date_from", type=str, default=None)
    p.add_argument("--to", dest="date_to", type=str, default=None)
    p.add_argument("--json", action="store_true")
    a = p.parse_args()
    r = run_whatif(strategy=a.strategy, instrument=a.instrument,
                   date_from=a.date_from, date_to=a.date_to)
    if a.json:
        import json as _j; print(_j.dumps(r, indent=2, default=str)); return
    if "error" in r:
        print(r["error"]); return
    act = r["actual_stats"]
    print("=" * 70)
    print(f"  WHAT-IF  ·  actual: n={act['n']} net Rs {act['net_pnl']:,.0f} "
          f"WR {act['win_rate']}%  ·  {r['adopt_count']} ADOPT / {r['test_count']} TEST")
    print("=" * 70)
    print(f"  {'Filter':<26}{'Removed':>8}{'Sim P&L':>12}{'Delta':>12}  Verdict")
    for x in r["results"]:
        print(f"  {x['filter']:<26}{x['removed']:>8}{x['sim_pnl']:>12,.0f}"
              f"{x['delta']:>12,.0f}  {x['verdict']}")
    if r["best_recommendation"]:
        b = r["best_recommendation"]
        print(f"  BEST: {b['filter']}  (+Rs {b['delta']:,.0f})")


if __name__ == "__main__":
    _cli()
