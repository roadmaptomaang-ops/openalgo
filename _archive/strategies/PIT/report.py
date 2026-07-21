"""
report.py — Daily trading report from SQLite database
======================================================
Usage:
    python3 report.py                        # today
    python3 report.py --date 2026-06-02      # specific date
    python3 report.py --strategy scalper     # one strategy only
    python3 report.py --last 7               # last 7 days summary
    python3 report.py --query               # interactive SQL prompt
"""

import sys
import argparse
from datetime import datetime, timedelta
from pathlib import Path

import pytz

# Add openalgo folder to path so db_logger imports cleanly
sys.path.insert(0, str(Path.home() / "openalgo"))
from db_logger import get_db

IST = pytz.timezone("Asia/Kolkata")

def now_ist():
    return datetime.now(IST)

def sep(char="=", width=64):
    return char * width

def pf_str(gp, gl):
    if gl <= 0: return "∞"
    return f"{gp/gl:.2f}"

# ── Single day report ─────────────────────────────────────────

def report_day(date: str, strategy: str = None):
    db   = get_db()
    filt = "AND strategy = ?" if strategy else ""
    params_base = (date, strategy) if strategy else (date,)

    rows = db.query(
        f"SELECT * FROM trades WHERE date = ? {filt} ORDER BY entry_time",
        params_base
    )

    if not rows:
        print(f"\nNo trades found for {date}" + (f" [{strategy}]" if strategy else ""))
        return

    print(f"\n{sep()}")
    print(f"  DAILY REPORT — {date}" + (f"  [{strategy.upper()}]" if strategy else ""))
    print(sep())

    # Group by strategy
    strategies = sorted(set(r["strategy"] for r in rows))
    totals = {"trades": 0, "wins": 0, "gp": 0, "gl": 0, "pnl": 0}

    for strat in strategies:
        strat_rows = [r for r in rows if r["strategy"] == strat]
        wins   = [r for r in strat_rows if r["pnl_rs"] > 0]
        losses = [r for r in strat_rows if r["pnl_rs"] <= 0]
        n      = len(strat_rows)
        wr     = len(wins) / n * 100 if n else 0
        gp     = sum(r["pnl_rs"] for r in wins)
        gl     = abs(sum(r["pnl_rs"] for r in losses))
        net    = sum(r["pnl_rs"] for r in strat_rows)
        avg_mfe = sum(r["mfe_pts"] or 0 for r in strat_rows) / n
        avg_cap = sum(r["capture_pct"] or 0 for r in strat_rows) / n
        best   = max(r["pnl_rs"] for r in strat_rows)
        worst  = min(r["pnl_rs"] for r in strat_rows)

        exit_ct = {}
        for r in strat_rows:
            exit_ct[r["exit_reason"]] = exit_ct.get(r["exit_reason"], 0) + 1

        print(f"\n  {'─'*30} {strat.upper()} {'─'*10}")
        print(f"  Trades        : {n}  (Wins:{len(wins)} Losses:{len(losses)} WR:{wr:.0f}%)")
        print(f"  Gross Profit  : Rs {gp:.0f}")
        print(f"  Gross Loss    : Rs {gl:.0f}")
        print(f"  Profit Factor : {pf_str(gp, gl)}")
        print(f"  Net P&L       : Rs {net:+.0f}")
        print(f"  Best Trade    : Rs {best:+.0f}")
        print(f"  Worst Trade   : Rs {worst:+.0f}")
        print(f"  Avg MFE       : {avg_mfe:.1f} pts")
        print(f"  Avg Capture   : {avg_cap:.0f}%")
        print(f"  Exit Breakdown:")
        for reason, cnt in sorted(exit_ct.items()):
            print(f"    {reason:<22}: {cnt}")

        totals["trades"] += n
        totals["wins"]   += len(wins)
        totals["gp"]     += gp
        totals["gl"]     += gl
        totals["pnl"]    += net

    if len(strategies) > 1:
        print(f"\n  {'─'*30} COMBINED {'─'*10}")
        wr = totals["wins"] / totals["trades"] * 100 if totals["trades"] else 0
        print(f"  Total Trades  : {totals['trades']}  (WR:{wr:.0f}%)")
        print(f"  Profit Factor : {pf_str(totals['gp'], totals['gl'])}")
        print(f"  Net P&L       : Rs {totals['pnl']:+.0f}")

    # Per-trade detail
    print(f"\n  {'─'*30} TRADE DETAIL {'─'*8}")
    print(f"  {'#':<3} {'Strat':<12} {'Inst':<8} {'Dir':<4} {'Entry':>8} {'PnL Rs':>8} {'MFE':>6} {'Capture':>8} {'Exit Reason'}")
    print(f"  {'─'*3} {'─'*12} {'─'*8} {'─'*4} {'─'*8} {'─'*8} {'─'*6} {'─'*8} {'─'*20}")
    for i, r in enumerate(rows, 1):
        cap = f"{r['capture_pct']:.0f}%" if r["capture_pct"] else "N/A"
        print(f"  {i:<3} {r['strategy']:<12} {r['instrument']:<8} {r['direction']:<4} "
              f"{r['entry_time']:>8} {r['pnl_rs']:>+8.0f} {(r['mfe_pts'] or 0):>6.1f} "
              f"{cap:>8} {r['exit_reason']}")

    # Breadth summary
    breadth = db.query(
        "SELECT bias, COUNT(*) as cnt FROM breadth_log WHERE date=? GROUP BY bias",
        (date,)
    )
    if breadth:
        print(f"\n  {'─'*30} MARKET BREADTH {'─'*6}")
        for b in breadth:
            print(f"  {b['bias']:<10}: {b['cnt']} readings")

    print(f"\n{sep()}\n")

# ── Multi-day summary ─────────────────────────────────────────

def report_last_n(days: int, strategy: str = None):
    db     = get_db()
    filt   = "AND strategy = ?" if strategy else ""
    start  = (now_ist() - timedelta(days=days)).strftime("%Y-%m-%d")
    params = (start, strategy) if strategy else (start,)

    rows = db.query(
        f"SELECT * FROM trades WHERE date >= ? {filt} ORDER BY date, entry_time",
        params
    )

    if not rows:
        print(f"\nNo trades in last {days} days.")
        return

    wins   = [r for r in rows if r["pnl_rs"] > 0]
    losses = [r for r in rows if r["pnl_rs"] <= 0]
    n      = len(rows)
    wr     = len(wins) / n * 100 if n else 0
    gp     = sum(r["pnl_rs"] for r in wins)
    gl     = abs(sum(r["pnl_rs"] for r in losses))
    net    = sum(r["pnl_rs"] for r in rows)

    # Best ADX range
    adx_buckets = {"20-25": [], "25-30": [], "30-40": [], "40+": []}
    for r in rows:
        adx = r.get("adx_entry") or 0
        if 20 <= adx < 25:   adx_buckets["20-25"].append(r["pnl_rs"])
        elif 25 <= adx < 30: adx_buckets["25-30"].append(r["pnl_rs"])
        elif 30 <= adx < 40: adx_buckets["30-40"].append(r["pnl_rs"])
        elif adx >= 40:      adx_buckets["40+"].append(r["pnl_rs"])

    # Best time of day
    time_buckets = {"09:20-10:30": [], "10:30-12:00": [], "12:00-13:30": [], "13:30-15:30": []}
    for r in rows:
        et = r.get("entry_time", "") or ""
        if "09:20" <= et < "10:30": time_buckets["09:20-10:30"].append(r["pnl_rs"])
        elif et < "12:00":          time_buckets["10:30-12:00"].append(r["pnl_rs"])
        elif et < "13:30":          time_buckets["12:00-13:30"].append(r["pnl_rs"])
        else:                       time_buckets["13:30-15:30"].append(r["pnl_rs"])

    # CE vs PE
    ce_rows = [r for r in rows if r["direction"] == "CE"]
    pe_rows = [r for r in rows if r["direction"] == "PE"]

    # Exit reason breakdown
    exit_ct = {}
    for r in rows:
        exit_ct[r["exit_reason"]] = exit_ct.get(r["exit_reason"], 0) + 1

    print(f"\n{sep()}")
    print(f"  LAST {days} DAYS SUMMARY" + (f"  [{strategy.upper()}]" if strategy else ""))
    print(sep())
    print(f"  Period        : {start} → {now_ist().strftime('%Y-%m-%d')}")
    print(f"  Total Trades  : {n}  (Wins:{len(wins)} Losses:{len(losses)} WR:{wr:.0f}%)")
    print(f"  Profit Factor : {pf_str(gp, gl)}")
    print(f"  Gross Profit  : Rs {gp:.0f}")
    print(f"  Gross Loss    : Rs {gl:.0f}")
    print(f"  Net P&L       : Rs {net:+.0f}")

    print(f"\n  {'─'*30} DIRECTION ANALYSIS {'─'*4}")
    for direction, drows in [("CE", ce_rows), ("PE", pe_rows)]:
        if not drows: continue
        dw  = len([r for r in drows if r["pnl_rs"] > 0])
        dwr = dw / len(drows) * 100
        dnet = sum(r["pnl_rs"] for r in drows)
        print(f"  {direction}  : {len(drows)} trades | WR:{dwr:.0f}% | Net: Rs {dnet:+.0f}")

    print(f"\n  {'─'*30} ADX RANGE ANALYSIS {'─'*4}")
    for bucket, brows in adx_buckets.items():
        if not brows: continue
        bw  = len([v for v in brows if v > 0])
        bwr = bw / len(brows) * 100
        bnet = sum(brows)
        print(f"  ADX {bucket:<6} : {len(brows)} trades | WR:{bwr:.0f}% | Net: Rs {bnet:+.0f}")

    print(f"\n  {'─'*30} TIME OF DAY ANALYSIS {'─'*2}")
    for bucket, brows in time_buckets.items():
        if not brows: continue
        bw   = len([v for v in brows if v > 0])
        bwr  = bw / len(brows) * 100
        bnet = sum(brows)
        print(f"  {bucket} : {len(brows)} trades | WR:{bwr:.0f}% | Net: Rs {bnet:+.0f}")

    print(f"\n  {'─'*30} EXIT BREAKDOWN {'─'*8}")
    for reason, cnt in sorted(exit_ct.items(), key=lambda x: -x[1]):
        erws = [r for r in rows if r["exit_reason"] == reason and r["pnl_rs"] > 0]
        erwr = len(erws) / cnt * 100 if cnt else 0
        print(f"  {reason:<22}: {cnt} trades | WR:{erwr:.0f}%")

    print(f"\n{sep()}\n")

# ── Interactive query ─────────────────────────────────────────

def interactive_query():
    db = get_db()
    print(f"\n{sep()}")
    print("  Interactive SQL Query Mode")
    print("  Type SQL query, press Enter. Type 'exit' to quit.")
    print("  Tables: trades, breadth_log, daily_summary")
    print(sep())
    while True:
        try:
            sql = input("\n  SQL> ").strip()
            if sql.lower() in ("exit", "quit", "q"):
                break
            if not sql:
                continue
            rows = db.query(sql)
            if not rows:
                print("  (no results)")
            else:
                # Print header
                keys = list(rows[0].keys())
                print("  " + " | ".join(f"{k:>12}" for k in keys))
                print("  " + "-" * (15 * len(keys)))
                for r in rows[:50]:
                    print("  " + " | ".join(f"{str(r[k] or '')!s:>12}" for k in keys))
                if len(rows) > 50:
                    print(f"  ... {len(rows)-50} more rows")
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"  Error: {e}")
    print("\n  Exiting query mode.\n")

# ── Main ──────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="OpenAlgo Trading Report")
    parser.add_argument("--date",     type=str, help="Date YYYY-MM-DD (default: today)")
    parser.add_argument("--strategy", type=str, help="Filter by strategy (scalper/trend_rider)")
    parser.add_argument("--last",     type=int, help="Last N days summary")
    parser.add_argument("--query",    action="store_true", help="Interactive SQL mode")
    args = parser.parse_args()

    if args.query:
        interactive_query()
    elif args.last:
        report_last_n(args.last, args.strategy)
    else:
        date = args.date or datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d")
        report_day(date, args.strategy)

if __name__ == "__main__":
    main()
