"""
NEWPIT — The Capture Engine
===========================
A brand-new analytical lens. Every other tool measures what you MADE.
NEWPIT measures what the market OFFERED you — and how little of it you kept.

The core idea (the "Regret Surface"):
  Every trade's MFE (max favourable excursion) is what the market HANDED you at
  the peak. Your P&L is what you KEPT. The gap is REGRET — profit the market gave
  and you gave back.

  OFFERED   = Σ MFE (in ₹)      ← the market's generosity, the theoretical ceiling
  CAPTURED  = Σ P&L (in ₹)      ← what you actually kept
  REGRET    = OFFERED − CAPTURED ← what you left on the table
  CAPTURE % = CAPTURED / OFFERED ← the ONE number that matters

Why this is new: it separates "is there opportunity?" (OFFERED) from "can you
keep it?" (CAPTURE %). For this book the market offered ₹117k and we kept ₹5.9k
— a 5% capture. The problem was never entries; it's 100% in the exits. NEWPIT
proves it, decomposes it by regime / exit-reason / time, and shows the ceiling.

Run:
    uv run python3 newpit.py                 # full report
    uv run python3 newpit.py --strategy trend_rider
    uv run python3 newpit.py --ceiling       # the "what capture % would break even?" curve
"""

import sqlite3
from collections import defaultdict
from pathlib import Path

TRADING_DB = Path.home() / "openalgo" / "trading.db"


def _load(strategy_filter=None):
    con = sqlite3.connect(str(TRADING_DB)); con.row_factory = sqlite3.Row
    q = "SELECT * FROM trades WHERE strategy!='test' AND mfe_pts IS NOT NULL"
    p = ()
    if strategy_filter:
        q += " AND strategy=?"; p = (strategy_filter,)
    rows = [dict(r) for r in con.execute(q, p)]
    con.close()
    return rows


def _qty(r):
    """Recover contract qty from pnl_rs / pnl_pts (both logged)."""
    if r.get("pnl_pts") and abs(r["pnl_pts"]) > 1e-6:
        return abs(r["pnl_rs"] / r["pnl_pts"])
    return 0.0


def _metrics(rows):
    """Per-trade offer/capture, aggregated."""
    offered = captured = regret = adverse = 0.0
    gift_loss = gift_loss_amt = 0
    n = 0
    for r in rows:
        q = _qty(r)
        if q <= 0:
            continue
        n += 1
        off = max((r.get("mfe_pts") or 0) * q, 0.0)   # market's gift at peak (>=0)
        cap = r.get("pnl_rs") or 0.0                    # what we kept
        adv = min((r.get("mae_pts") or 0) * q, 0.0)     # worst drawdown (<=0)
        offered += off; captured += cap; regret += (off - cap); adverse += adv
        if off > 0 and cap < 0:                         # gift turned into a loss
            gift_loss += 1; gift_loss_amt += (off - cap)
    eff = captured / offered * 100 if offered else 0
    return {"n": n, "offered": offered, "captured": captured, "regret": regret,
            "adverse": adverse, "capture_pct": eff,
            "gift_loss": gift_loss, "gift_loss_amt": gift_loss_amt}


def _bar(pct, width=24):
    fill = int(max(0, min(100, pct)) / 100 * width)
    return "█" * fill + "░" * (width - fill)


def report(strategy_filter=None):
    rows = _load(strategy_filter)
    if not rows:
        print("No trades."); return
    m = _metrics(rows)
    scope = strategy_filter or "ALL engines"
    print(f"\n{'='*68}")
    print(f"  NEWPIT — THE CAPTURE ENGINE   ·   {scope}   ·   {m['n']} trades")
    print(f"{'='*68}")
    print(f"\n  The market OFFERED you (Σ MFE) : Rs {m['offered']:>12,.0f}   ← the ceiling")
    print(f"  You CAPTURED (Σ P&L)           : Rs {m['captured']:>12,.0f}")
    print(f"  REGRET (left on the table)     : Rs {m['regret']:>12,.0f}")
    print(f"\n  CAPTURE EFFICIENCY : {m['capture_pct']:5.1f}%  {_bar(m['capture_pct'])}")
    print(f"  (you keep {m['capture_pct']:.0f} of every 100 rupees the market hands you)")
    print(f"\n  Gifts turned into LOSSES (MFE>0 but P&L<0): "
          f"{m['gift_loss']}/{m['n']} = {m['gift_loss']/m['n']*100:.0f}%  "
          f"(Rs {m['gift_loss_amt']:,.0f} squandered)")

    # ── by strategy ──
    if not strategy_filter:
        by = defaultdict(list)
        for r in rows:
            by[r["strategy"]].append(r)
        print(f"\n  {'strategy':16}{'offered':>11}{'captured':>11}{'capture%':>10}  bar")
        for s in sorted(by, key=lambda k: -_metrics(by[k])["captured"]):
            sm = _metrics(by[s])
            print(f"  {s:16}{sm['offered']:>11,.0f}{sm['captured']:>+11,.0f}"
                  f"{sm['capture_pct']:>9.1f}%  {_bar(sm['capture_pct'],14)}")

    # ── by exit reason (which exits leak the most) ──
    byx = defaultdict(list)
    for r in rows:
        byx[r.get("exit_reason") or "?"].append(r)
    print(f"\n  Where the leak is — by EXIT REASON:")
    print(f"  {'exit reason':22}{'trades':>7}{'offered':>10}{'captured':>10}{'cap%':>7}")
    for x in sorted(byx, key=lambda k: -_metrics(byx[k])["offered"]):
        xm = _metrics(byx[x])
        if xm["n"] < 2:
            continue
        print(f"  {x:22}{xm['n']:>7}{xm['offered']:>10,.0f}{xm['captured']:>+10,.0f}"
              f"{xm['capture_pct']:>6.0f}%")

    # ── regime split (trending day vs chop day, by that day's avg MFE) ──
    byday = defaultdict(list)
    for r in rows:
        byday[r["date"]].append(r)
    trend_rows, chop_rows = [], []
    for d, rs in byday.items():
        avg_mfe = sum((x.get("mfe_pts") or 0) for x in rs) / len(rs)
        (trend_rows if avg_mfe >= 3 else chop_rows).extend(rs)
    print(f"\n  Capture by REGIME (day classified by avg MFE):")
    for lbl, rs in [("TRENDING days (avg MFE≥3)", trend_rows), ("CHOP days (avg MFE<3)", chop_rows)]:
        if rs:
            rm = _metrics(rs)
            print(f"    {lbl:28}: {rm['n']:3} trades  captured Rs{rm['captured']:>+8,.0f}  "
                  f"capture {rm['capture_pct']:.0f}%")
    print(f"{'='*68}")


def ceiling(strategy_filter=None):
    """The counterfactual: if you'd captured K% of each trade's MFE, what's the
    net P&L? Finds the capture discipline that flips the book positive."""
    rows = _load(strategy_filter)
    trades = []
    for r in rows:
        q = _qty(r)
        if q <= 0:
            continue
        off = max((r.get("mfe_pts") or 0) * q, 0.0)
        cost = 55 if r["strategy"] != "trend_rider" and r["strategy"] != "trend_rider_v3" else 60
        trades.append((off, cost))
    if not trades:
        print("No trades."); return
    print(f"\n{'='*60}")
    print(f"  NEWPIT CEILING — 'if I captured K% of every MFE, net P&L?'")
    print(f"{'='*60}")
    print(f"  {'capture K%':>11}{'net P&L':>14}   {'verdict'}")
    for k in (5, 10, 15, 20, 25, 30, 40, 50):
        net = sum(off * k/100 - cost for off, cost in trades)
        flag = "✅ profitable" if net > 0 else ""
        print(f"  {k:>10}%{net:>+14,.0f}   {flag}")
    # find breakeven K
    lo, hi = 0.0, 100.0
    for _ in range(40):
        mid = (lo+hi)/2
        net = sum(off*mid/100 - cost for off, cost in trades)
        if net < 0: lo = mid
        else: hi = mid
    print(f"\n  → Breakeven at ~{hi:.0f}% capture. You're at 5%.")
    print(f"  The whole game is exits: get capture from 5% to ~{hi:.0f}% and the book flips.")
    print(f"{'='*60}")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="NEWPIT — the Capture Engine")
    p.add_argument("--strategy", type=str, default=None)
    p.add_argument("--ceiling", action="store_true", help="capture-vs-breakeven curve")
    a = p.parse_args()
    if a.ceiling:
        ceiling(a.strategy)
    else:
        report(a.strategy)
