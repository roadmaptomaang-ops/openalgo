"""
sage_scorecard.py — Net-of-Cost Reality Check
=============================================
Every P&L number in the logs is GROSS. This tool shows NET — after realistic
Upstox intraday charges (brokerage + STT + exchange txn + SEBI + GST + stamp).

Why it exists: over the full history the book was +Rs5,881 GROSS but
-Rs10,129 NET — costs (Rs16,010) were the single biggest line item, larger
than any strategy's P&L. Gross P&L is a fiction that hides overtrading friction.
This makes the truth impossible to ignore and kills the gross-number self-deception.

Run:
    uv run python3 sage_runner.py scorecard              # full history
    uv run python3 sage_runner.py scorecard --days 5     # last 5 trading days
    uv run python3 sage_scorecard.py                     # same, direct
"""

import sqlite3
from collections import defaultdict
from pathlib import Path

TRADING_DB = Path.home() / "openalgo" / "trading.db"

# ── Cost model (Upstox intraday, all-in per ROUND-TRIP). Edit to your real rates.
# Index options: Rs20/leg brokerage + STT on sell premium + exchange txn + GST + stamp.
# Equity intraday: min(Rs20, 0.05%)/leg + STT + exchange + GST + stamp.
COST_PER_ROUNDTRIP = {"options": 55.0, "equity": 60.0}


def _kind(strategy: str) -> str:
    return "equity" if strategy == "trend_rider" or strategy == "trend_rider_v3" else "options"


def _rupees(x: float) -> str:
    return f"Rs {x:+,.0f}"


def scorecard(days: int | None = None, verbose: bool = True) -> dict:
    if not TRADING_DB.exists():
        print("[scorecard] trading.db not found"); return {}
    con = sqlite3.connect(str(TRADING_DB)); con.row_factory = sqlite3.Row
    q = "SELECT date,strategy,pnl_rs FROM trades WHERE strategy != 'test'"
    rows = [dict(r) for r in con.execute(q)]
    con.close()
    if not rows:
        print("[scorecard] no trades"); return {}

    all_dates = sorted({r["date"] for r in rows})
    keep = set(all_dates[-days:]) if days else set(all_dates)
    rows = [r for r in rows if r["date"] in keep]

    per = defaultdict(lambda: {"n": 0, "gross": 0.0, "wins": 0})
    for r in rows:
        p = r["pnl_rs"] or 0.0
        s = per[r["strategy"]]
        s["n"] += 1; s["gross"] += p
        if p > 0:
            s["wins"] += 1

    # ── Edge-decay check: is the RECENT window still earning, or is the profit a
    # fossil? Compare the last ~6 trading days against everything before. Recent decay
    # is what matters — a median split hides it (the good early days drag the average up).
    RECENT_DAYS = 6
    decay = {}
    for strat in per:
        sdays = sorted({r["date"] for r in rows if r["strategy"] == strat})
        if len(sdays) < RECENT_DAYS + 3:
            continue
        cut = sdays[-RECENT_DAYS]
        cost = COST_PER_ROUNDTRIP[_kind(strat)]
        early = [r for r in rows if r["strategy"] == strat and r["date"] < cut]
        late = [r for r in rows if r["strategy"] == strat and r["date"] >= cut]
        if not early or not late:
            continue
        e_npt = (sum(r["pnl_rs"] or 0 for r in early) - len(early)*cost) / len(early)
        l_npt = (sum(r["pnl_rs"] or 0 for r in late) - len(late)*cost) / len(late)
        decay[strat] = {"early_npt": e_npt, "late_npt": l_npt, "cut": cut,
                        "decayed": e_npt > 0 and l_npt < 0}

    result = {}
    tot = {"n": 0, "gross": 0.0, "cost": 0.0, "net": 0.0}
    for strat, s in per.items():
        cost = s["n"] * COST_PER_ROUNDTRIP[_kind(strat)]
        net = s["gross"] - cost
        result[strat] = {
            "trades": s["n"], "gross": s["gross"], "cost": cost, "net": net,
            "net_per_trade": net / s["n"] if s["n"] else 0,
            "win_rate": s["wins"] / s["n"] if s["n"] else 0,
        }
        tot["n"] += s["n"]; tot["gross"] += s["gross"]; tot["cost"] += cost; tot["net"] += net
    result["_TOTAL"] = {**tot, "net_per_trade": tot["net"]/tot["n"] if tot["n"] else 0}

    if verbose:
        span = f"last {days} days" if days else f"all {len(all_dates)} days"
        print(f"\n{'='*66}")
        print(f"  SAGE SCORECARD — NET of costs  ·  {span}")
        print(f"  Cost model: options Rs{COST_PER_ROUNDTRIP['options']:.0f}/RT, "
              f"equity Rs{COST_PER_ROUNDTRIP['equity']:.0f}/RT")
        print(f"{'='*66}")
        print(f"  {'strategy':16}{'trades':>7}{'WR':>5}{'GROSS':>10}{'costs':>8}{'NET':>10}{'/trade':>8}")
        ordered = sorted((k for k in result if k != "_TOTAL"),
                         key=lambda k: -result[k]["net"])
        for k in ordered:
            d = result[k]
            print(f"  {k:16}{d['trades']:>7}{d['win_rate']*100:>4.0f}%"
                  f"{d['gross']:>+10.0f}{d['cost']:>8.0f}{d['net']:>+10.0f}{d['net_per_trade']:>+8.0f}")
        t = result["_TOTAL"]
        print(f"  {'-'*64}")
        print(f"  {'TOTAL':16}{t['n']:>7}{'':>5}{t['gross']:>+10.0f}{t['cost']:>8.0f}{t['net']:>+10.0f}")

        # verdict
        earners = [k for k in ordered if result[k]["net"] > 0]
        bleeders = [k for k in ordered if result[k]["net"] < 0]
        print(f"\n  VERDICT")
        if earners:
            best = earners[0]
            print(f"    Net-positive after costs : {', '.join(earners)}")
            print(f"    → {best} carries the book (+{result[best]['net']:,.0f} net).")
        if bleeders:
            worst = min(bleeders, key=lambda k: result[k]["net"])
            print(f"    Net-negative after costs : {', '.join(bleeders)}")
            print(f"    → {worst} is the biggest drain ({result[worst]['net']:,.0f} net); "
                  f"costs alone were Rs{result[worst]['cost']:,.0f}.")
        if t["net"] < 0 < t["gross"]:
            print(f"    ⚠ Book is GROSS-positive (+{t['gross']:,.0f}) but NET-NEGATIVE "
                  f"({t['net']:,.0f}) — overtrading friction (Rs{t['cost']:,.0f}) is the enemy.")
        # decay warnings — the thing that matters most
        fossils = [k for k, v in decay.items() if v["decayed"]]
        if fossils:
            print(f"\n  ⚠ EDGE DECAY DETECTED")
            for k in fossils:
                v = decay[k]
                print(f"    {k}: {_rupees(v['early_npt'])}/trade before {v['cut']} → "
                      f"{_rupees(v['late_npt'])}/trade since. "
                      f"The profit is a FOSSIL — recent trades lose. Stand down / re-fit.")
        # regime gauge for the earner — the leading indicator that decayed
        for earner in ("trend_rider_v3", "trend_rider"):
            if earner in per and per[earner]["n"] >= 10:
                follow_through_health(earner, verbose=True)
                break
        print(f"{'='*66}")

    result["_DECAY"] = decay
    return result


def follow_through_health(strategy: str = "trend_rider", window: int = 10,
                          verbose: bool = True) -> dict:
    """Rolling avg MFE — the REGIME gauge. Follow-through (how far trades run in
    your favour) collapses BEFORE P&L does, so it's a leading indicator of a
    trend→chop shift. For trend_rider it went 6.9pts (trending) → 1.0pts (chop),
    which is why the edge decayed. Read this to know if the market is offering
    moves RIGHT NOW — trade the earner only when it's healthy (>~3pts)."""
    con = sqlite3.connect(str(TRADING_DB)); con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT date,mfe_pts FROM trades WHERE strategy=? ORDER BY date,entry_time", (strategy,))]
    con.close()
    if len(rows) < window:
        return {}
    recent = [r["mfe_pts"] or 0 for r in rows[-window:]]
    prior = [r["mfe_pts"] or 0 for r in rows[-2*window:-window]] or recent
    r_avg = sum(recent) / len(recent)
    p_avg = sum(prior) / len(prior)
    state = "TRENDING (trade it)" if r_avg >= 3 else (
            "CHOP (stand down)" if r_avg < 2 else "WEAK (half size)")
    if verbose:
        arrow = "↓" if r_avg < p_avg else "↑"
        print(f"\n  FOLLOW-THROUGH HEALTH — {strategy}  (regime gauge)")
        print(f"    last {window} trades avg MFE : {r_avg:.1f} pts  {arrow}  "
              f"(prior {window}: {p_avg:.1f})")
        print(f"    regime read              : {state}")
        print(f"    (>3 = market offers moves · <2 = no follow-through, edge dormant)")
    return {"recent_mfe": r_avg, "prior_mfe": p_avg, "state": state}


def equity_curve(strategy: str, verbose: bool = True) -> list:
    """Cumulative NET P&L per day for one strategy — is the curve rising?"""
    con = sqlite3.connect(str(TRADING_DB)); con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT date,pnl_rs FROM trades WHERE strategy=? ORDER BY date,entry_time", (strategy,))]
    con.close()
    cost = COST_PER_ROUNDTRIP[_kind(strategy)]
    byday = defaultdict(lambda: [0, 0.0])
    for r in rows:
        byday[r["date"]][0] += 1
        byday[r["date"]][1] += (r["pnl_rs"] or 0.0)
    cum = 0.0; curve = []
    for d in sorted(byday):
        n, gross = byday[d]
        net = gross - n * cost
        cum += net
        curve.append((d, n, net, cum))
    if verbose and curve:
        print(f"\n  {strategy} — daily NET equity curve")
        print(f"  {'date':11}{'trades':>7}{'day net':>9}{'cumulative':>12}")
        for d, n, net, c in curve:
            bar = "▲" if net > 0 else ("▼" if net < 0 else "·")
            print(f"  {d:11}{n:>7}{net:>+9.0f}{c:>+12.0f}  {bar}")
    return curve


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=None)
    p.add_argument("--curve", type=str, default=None, help="show equity curve for a strategy")
    a = p.parse_args()
    scorecard(days=a.days)
    if a.curve:
        equity_curve(a.curve)
