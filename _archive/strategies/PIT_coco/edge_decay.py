"""
edge_decay.py — Edge Decay Detector
=====================================
Detects strategy degradation before major drawdown occurs.

Computes rolling metrics across 7d / 14d / 30d / all-time windows
and compares them to baseline. Issues WARN / ALERT when thresholds breach.

Writes snapshots to edge_decay_snapshots table for trend tracking.
Returns structured result dict consumed by the HTML report renderer.

Usage:
    from edge_decay import run_edge_decay
    result = run_edge_decay()          # uses all trades in DB
    result = run_edge_decay(strategy="scalper")
"""

import statistics
from datetime import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from analytics_core import (
    load_trades, profit_factor, win_rate, expectancy,
    max_drawdown, sharpe_proxy, rolling_windows,
    consecutive_runs, pnl_series, grade,
    safe_div, bootstrap_analytics_tables, get_db, DB_PATH
)


# ── Decay thresholds ──────────────────────────────────────────────────────────
# WARN fires when recent window has deteriorated by this % vs baseline (all-time)
# ALERT fires at double the WARN threshold

THRESHOLDS = {
    "win_rate":       {"warn": 0.08,  "alert": 0.15},   # absolute drop (e.g. 60%→52%)
    "profit_factor":  {"warn": 0.30,  "alert": 0.55},   # absolute drop
    "expectancy":     {"warn": 0.25,  "alert": 0.50},   # relative drop
    "avg_trade":      {"warn": 0.30,  "alert": 0.55},   # relative drop
}

MIN_TRADES_FOR_SIGNAL = 10   # don't fire decay warnings on tiny samples


def _compute_window_stats(trades: list[dict]) -> dict:
    """Compute all metrics for a list of trades."""
    n = len(trades)
    if n == 0:
        return {"n": 0, "win_rate": None, "profit_factor": None,
                "expectancy": None, "avg_trade": None, "avg_winner": None,
                "avg_loser": None, "max_drawdown": None, "sharpe": None,
                "grade": "N/A"}

    winners = [t["pnl_rs"] for t in trades if t["is_winner"]]
    losers  = [t["pnl_rs"] for t in trades if t["is_loser"]]
    pnls    = [t["pnl_rs"] for t in trades]

    wr  = win_rate(trades)
    pf  = profit_factor(trades)
    exp = expectancy(trades)
    dd  = max_drawdown(trades)
    sp  = sharpe_proxy(trades)
    avg_w = statistics.mean(winners) if winners else 0
    avg_l = statistics.mean(losers)  if losers  else 0
    avg_t = statistics.mean(pnls)

    return {
        "n":             n,
        "win_rate":      wr,
        "profit_factor": pf,
        "expectancy":    exp,
        "avg_trade":     avg_t,
        "avg_winner":    avg_w,
        "avg_loser":     avg_l,
        "max_drawdown":  dd,
        "sharpe":        sp,
        "grade":         grade(wr, pf, exp, dd, n),
    }


def _assess_decay(baseline: dict, recent: dict) -> tuple[str, list[str]]:
    """
    Compare recent window stats to baseline.
    Returns (flag, [reasons]).
    flag ∈ {'OK', 'WARN', 'ALERT'}
    """
    if recent["n"] < MIN_TRADES_FOR_SIGNAL:
        return "INSUFFICIENT_DATA", ["< 10 trades in window — no signal"]

    reasons = []
    max_flag = "OK"

    def _flag(level):
        nonlocal max_flag
        order = {"OK": 0, "WARN": 1, "ALERT": 2}
        if order.get(level, 0) > order.get(max_flag, 0):
            max_flag = level

    # Win rate — absolute drop
    if baseline["win_rate"] and recent["win_rate"] is not None:
        drop = baseline["win_rate"] - recent["win_rate"]
        t = THRESHOLDS["win_rate"]
        if drop >= t["alert"]:
            reasons.append(f"Win rate dropped {drop*100:.1f}pp vs baseline (ALERT threshold: {t['alert']*100:.0f}pp)")
            _flag("ALERT")
        elif drop >= t["warn"]:
            reasons.append(f"Win rate dropped {drop*100:.1f}pp vs baseline (WARN threshold: {t['warn']*100:.0f}pp)")
            _flag("WARN")

    # Profit Factor — absolute drop
    if baseline["profit_factor"] and recent["profit_factor"] is not None:
        drop = baseline["profit_factor"] - recent["profit_factor"]
        t = THRESHOLDS["profit_factor"]
        if drop >= t["alert"]:
            reasons.append(f"Profit factor dropped {drop:.2f} vs baseline")
            _flag("ALERT")
        elif drop >= t["warn"]:
            reasons.append(f"Profit factor dropped {drop:.2f} vs baseline")
            _flag("WARN")
        if recent["profit_factor"] < 1.0:
            reasons.append("Profit factor below 1.0 — strategy is net negative in this window")
            _flag("ALERT")

    # Expectancy — relative drop
    if baseline["expectancy"] and recent["expectancy"] is not None:
        if baseline["expectancy"] > 0:
            rel_drop = (baseline["expectancy"] - recent["expectancy"]) / abs(baseline["expectancy"])
            t = THRESHOLDS["expectancy"]
            if rel_drop >= t["alert"]:
                reasons.append(f"Expectancy degraded {rel_drop*100:.0f}% relative to baseline")
                _flag("ALERT")
            elif rel_drop >= t["warn"]:
                reasons.append(f"Expectancy degraded {rel_drop*100:.0f}% relative to baseline")
                _flag("WARN")
        if recent["expectancy"] < 0:
            reasons.append("Expectancy is negative — strategy losing money per trade on average")
            _flag("ALERT")

    # Avg trade — relative drop
    if baseline["avg_trade"] and recent["avg_trade"] is not None and baseline["avg_trade"] > 0:
        rel_drop = (baseline["avg_trade"] - recent["avg_trade"]) / abs(baseline["avg_trade"])
        t = THRESHOLDS["avg_trade"]
        if rel_drop >= t["alert"]:
            reasons.append(f"Average trade size dropped {rel_drop*100:.0f}% relative to baseline")
            _flag("ALERT")
        elif rel_drop >= t["warn"]:
            reasons.append(f"Average trade size dropped {rel_drop*100:.0f}% relative to baseline")
            _flag("WARN")

    if not reasons:
        reasons.append("All metrics within normal range")

    return max_flag, reasons


def _save_snapshot(date: str, label: str, stats: dict, flag: str, reasons: list[str]):
    """Persist snapshot to analytics table."""
    db = get_db()
    reason_str = " | ".join(reasons)
    db.execute(
        """INSERT INTO edge_decay_snapshots
           (snapshot_date, window_label, trade_count, win_rate, profit_factor,
            expectancy, avg_trade, avg_winner, avg_loser, max_drawdown,
            sharpe_proxy, decay_flag, decay_reason)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (date, label, stats["n"], stats["win_rate"], stats["profit_factor"],
         stats["expectancy"], stats["avg_trade"], stats["avg_winner"],
         stats["avg_loser"], stats["max_drawdown"], stats["sharpe"],
         flag, reason_str)
    )


def _equity_curve_data(trades: list[dict]) -> list[dict]:
    """Return per-trade equity curve with drawdown annotation."""
    running = 0.0; peak = 0.0
    curve = []
    for i, t in enumerate(trades):
        running += t["pnl_rs"]
        if running > peak: peak = running
        dd = peak - running
        curve.append({
            "trade_n":   i + 1,
            "date":      t["date"],
            "pnl":       t["pnl_rs"],
            "cumulative": running,
            "drawdown":  -dd,
            "is_winner": t["is_winner"],
        })
    return curve


def _trend_direction(values: list[float]) -> str:
    """Simple linear trend: positive / negative / flat."""
    if len(values) < 3: return "FLAT"
    n = len(values)
    xs = list(range(n))
    x_mean = sum(xs) / n
    y_mean = sum(values) / n
    num = sum((xs[i] - x_mean) * (values[i] - y_mean) for i in range(n))
    den = sum((xs[i] - x_mean) ** 2 for i in range(n))
    if den == 0: return "FLAT"
    slope = num / den
    rel = abs(slope) / (abs(y_mean) + 1e-9)
    if rel < 0.02: return "FLAT"
    return "IMPROVING" if slope > 0 else "DETERIORATING"


def run_edge_decay(
    strategy: str = None,
    instrument: str = None,
    save_snapshot: bool = True,
) -> dict:
    """
    Main entry point. Returns full structured result for HTML renderer.
    """
    bootstrap_analytics_tables()
    all_trades = load_trades(strategy=strategy, instrument=instrument)

    if not all_trades:
        return {"error": "No trades found in database."}

    windows = rolling_windows(all_trades, windows=[7, 14, 30])
    windows["all"] = all_trades

    today = datetime.now().strftime("%Y-%m-%d")
    baseline = _compute_window_stats(all_trades)

    window_results = {}
    for label, trades in [("7d", windows[7]), ("14d", windows[14]),
                          ("30d", windows[30]), ("all", all_trades)]:
        stats = _compute_window_stats(trades)
        flag, reasons = _assess_decay(baseline, stats) if label != "all" else ("BASELINE", ["Baseline reference"])
        window_results[label] = {
            "stats":   stats,
            "flag":    flag,
            "reasons": reasons,
        }
        if save_snapshot and label != "all":
            try:
                _save_snapshot(today, label, stats, flag, reasons)
            except Exception:
                pass  # non-fatal

    # Historical snapshots for trend chart
    db = get_db()
    snapshots = db.query(
        """SELECT snapshot_date, window_label, win_rate, profit_factor,
                  expectancy, decay_flag
           FROM edge_decay_snapshots
           WHERE window_label = '7d'
           ORDER BY snapshot_date DESC LIMIT 30"""
    )

    # Win rate trend over time (by date)
    by_date = {}
    for t in all_trades:
        d = t["date"]
        by_date.setdefault(d, []).append(t)
    dates_sorted = sorted(by_date.keys())

    daily_metrics = []
    for d in dates_sorted:
        day_trades = by_date[d]
        wr = win_rate(day_trades)
        pf = profit_factor(day_trades)
        exp = expectancy(day_trades)
        net = sum(t["pnl_rs"] for t in day_trades)
        daily_metrics.append({
            "date": d,
            "n": len(day_trades),
            "win_rate": round(wr * 100, 1) if wr else None,
            "profit_factor": round(pf, 2) if pf else None,
            "expectancy": round(exp, 0) if exp else None,
            "net_pnl": round(net, 0),
        })

    # Trend directions
    wr_series = [d["win_rate"] for d in daily_metrics if d["win_rate"] is not None]
    pf_series = [d["profit_factor"] for d in daily_metrics if d["profit_factor"] is not None]
    exp_series = [d["expectancy"] for d in daily_metrics if d["expectancy"] is not None]

    wr_trend  = _trend_direction(wr_series)
    pf_trend  = _trend_direction(pf_series)
    exp_trend = _trend_direction(exp_series)

    # Overall system status
    recent_flag  = window_results["7d"]["flag"]
    flag_order   = {"OK": 0, "WARN": 1, "ALERT": 2, "INSUFFICIENT_DATA": -1, "BASELINE": -1}
    system_status = recent_flag if recent_flag in flag_order else "INSUFFICIENT_DATA"

    # Equity curve
    equity_curve = _equity_curve_data(all_trades)

    max_w, max_l, streak = consecutive_runs(all_trades)

    return {
        "module":          "edge_decay",
        "generated_at":    today,
        "strategy_filter": strategy,
        "total_trades":    len(all_trades),
        "date_range": {
            "start": all_trades[0]["date"] if all_trades else None,
            "end":   all_trades[-1]["date"] if all_trades else None,
        },
        "baseline":         baseline,
        "windows":          window_results,
        "daily_metrics":    daily_metrics,
        "equity_curve":     equity_curve,
        "trends": {
            "win_rate":      wr_trend,
            "profit_factor": pf_trend,
            "expectancy":    exp_trend,
        },
        "streaks": {
            "max_wins":   max_w,
            "max_losses": max_l,
            "current":    streak,
        },
        "system_status": system_status,
        "thresholds":    THRESHOLDS,
    }


def _cli():
    import argparse
    p = argparse.ArgumentParser(description="Edge Decay Detector")
    p.add_argument("--strategy", type=str, default=None)
    p.add_argument("--instrument", type=str, default=None)
    p.add_argument("--json", action="store_true", help="dump full result as JSON")
    a = p.parse_args()
    r = run_edge_decay(strategy=a.strategy, instrument=a.instrument)
    if a.json:
        import json as _j; print(_j.dumps(r, indent=2, default=str)); return
    if "error" in r:
        print(r["error"]); return
    print("=" * 60)
    print(f"  EDGE DECAY  ·  {r['total_trades']} trades  ·  status: {r['system_status']}")
    print(f"  Range: {r['date_range']['start']} -> {r['date_range']['end']}")
    print("=" * 60)
    for label in ("7d", "14d", "30d", "all"):
        w = r["windows"].get(label, {}); s = w.get("stats", {})
        wr = f"{(s.get('win_rate') or 0)*100:.1f}%" if s.get("win_rate") is not None else "-"
        pf = f"{s['profit_factor']:.2f}" if s.get("profit_factor") else "-"
        print(f"  {label:>4} | n={s.get('n',0):>3} | WR {wr:>6} | PF {pf:>5} | "
              f"grade {s.get('grade','N/A')} | flag {w.get('flag','-')}")
        if w.get("reasons"):
            print(f"        - {w['reasons'][0]}")
    t = r["trends"]
    print(f"  Trends: WR {t['win_rate']} · PF {t['profit_factor']} · Exp {t['expectancy']}")
    sk = r["streaks"]
    print(f"  Streaks: {sk['max_wins']}W max · {sk['max_losses']}L max · current {sk['current']}")


if __name__ == "__main__":
    _cli()
