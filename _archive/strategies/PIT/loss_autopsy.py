"""
loss_autopsy.py — Loss Autopsy Engine
======================================
For every losing trade, determines the primary failure cause from
available indicator data. No AI generation — pure rule-based classification
grounded in what the DB actually stores.

Failure categories (in order of classification priority):
  1. WEAK_TREND         — ADX below threshold at entry
  2. LATE_ENTRY         — entry time in deteriorating session window
  3. MOMENTUM_COLLAPSE  — momentum field is bearish/negative
  4. TIGHT_STOP         — MAE exceeded expected stop distance
  5. PREMATURE_ENTRY    — trade reversed quickly (short hold, large MAE)
  6. CHOPPY_REGIME      — multiple signals point to low-conviction environment
  7. BAD_SESSION        — time-based session with historically poor performance
  8. UNDEFINED          — insufficient data to classify

Aggregates:
  - Top failure causes (ranked by count + total loss)
  - Loss by time bucket
  - Loss by instrument
  - Loss by strategy
  - Behavioral vs system weakness breakdown

Writes to loss_autopsy table.

Usage:
    from loss_autopsy import run_loss_autopsy
    result = run_loss_autopsy()
"""

import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from analytics_core import (
    load_trades, profit_factor, win_rate, expectancy,
    bootstrap_analytics_tables, get_db, safe_div
)


# ── Classification thresholds ─────────────────────────────────────────────────
ADX_WEAK_THRESHOLD    = 22.0   # ADX below this = weak trend
ADX_STRONG_THRESHOLD  = 28.0   # ADX above this = strong trend
PREMATURE_MAX_HOLD    = 8      # minutes — if trade closed this fast with loss
QUICK_REVERSAL_MAE    = 6.0    # pts — MAE within first ~8 minutes
BAD_SESSION_HOURS     = {13}   # hours historically weak (1 PM)


CAUSE_LABELS = {
    "WEAK_TREND":        "Weak Trend at Entry (Low ADX)",
    "LATE_ENTRY":        "Late Entry / Extended Move",
    "MOMENTUM_COLLAPSE": "Momentum Collapse After Entry",
    "TIGHT_STOP":        "Stop Too Tight for Volatility",
    "PREMATURE_ENTRY":   "Premature Entry / Quick Reversal",
    "CHOPPY_REGIME":     "Choppy / Low Conviction Environment",
    "BAD_SESSION":       "Weak Session Time Window",
    "UNDEFINED":         "Insufficient Data to Classify",
}

CAUSE_TYPE = {
    # behavioral = trader can improve
    # system = strategy rule / filter issue
    "WEAK_TREND":        "system",
    "LATE_ENTRY":        "behavioral",
    "MOMENTUM_COLLAPSE": "system",
    "TIGHT_STOP":        "system",
    "PREMATURE_ENTRY":   "behavioral",
    "CHOPPY_REGIME":     "system",
    "BAD_SESSION":       "behavioral",
    "UNDEFINED":         "unknown",
}


def _classify_loss(t: dict) -> tuple[str, str]:
    """
    Returns (primary_cause, secondary_cause) for a losing trade.
    Rule-based — only uses fields actually in the DB.
    """
    adx   = t.get("adx_entry") or 0.0
    atr   = t.get("atr_entry") or 0.0
    mae   = abs(t.get("mae_pts") or 0.0)
    hold  = t.get("duration_min") or 999
    mom   = (t.get("momentum") or "").upper()
    hour  = t.get("entry_hour")
    exit_r = (t.get("exit_reason") or "").upper()

    primary   = "UNDEFINED"
    secondary = None

    # Priority 1: ADX clearly weak
    if 0 < adx < ADX_WEAK_THRESHOLD:
        primary = "WEAK_TREND"
        if hold < PREMATURE_MAX_HOLD:
            secondary = "PREMATURE_ENTRY"
        return primary, secondary

    # Priority 2: Premature entry (fast exit with meaningful MAE)
    if hold < PREMATURE_MAX_HOLD and mae >= QUICK_REVERSAL_MAE:
        primary = "PREMATURE_ENTRY"
        if adx < ADX_STRONG_THRESHOLD:
            secondary = "WEAK_TREND"
        return primary, secondary

    # Priority 3: Momentum collapse (momentum field available)
    if mom in ("BEARISH", "NEGATIVE", "WEAK", "DOWN", "DECLINING"):
        primary = "MOMENTUM_COLLAPSE"
        if adx < ADX_STRONG_THRESHOLD:
            secondary = "WEAK_TREND"
        return primary, secondary

    # Priority 4: Tight stop — MAE >> ATR / expected range
    if atr > 0 and mae > atr * 0.8:
        primary = "TIGHT_STOP"
        return primary, secondary

    # Priority 5: Bad session hour
    if hour in BAD_SESSION_HOURS:
        primary = "BAD_SESSION"
        if adx < ADX_STRONG_THRESHOLD:
            secondary = "WEAK_TREND"
        return primary, secondary

    # Priority 6: Long hold, SL exit — late entry into extended move
    if hold > 20 and exit_r == "SL":
        primary = "LATE_ENTRY"
        return primary, secondary

    # Priority 7: Multi-signal low conviction
    signals = 0
    if adx < ADX_STRONG_THRESHOLD: signals += 1
    if mae > 5: signals += 1
    if hold < 15 and exit_r == "SL": signals += 1
    if signals >= 2:
        primary = "CHOPPY_REGIME"
        return primary, secondary

    return "UNDEFINED", None


def _save_autopsy(trade: dict, cause: str, secondary: str, regime_tag: str):
    db = get_db()
    try:
        db.query(
            """INSERT INTO loss_autopsy
               (trade_id, date, symbol, pnl_rs, primary_cause, secondary_cause,
                adx_at_entry, atr_at_entry, entry_time, duration_min, mae_pts,
                regime_tag, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (trade.get("id"), trade.get("date"), trade.get("symbol"),
             trade.get("pnl_rs"), cause, secondary,
             trade.get("adx_entry"), trade.get("atr_entry"),
             trade.get("entry_time"), trade.get("duration_min"),
             abs(trade.get("mae_pts") or 0), regime_tag, None)
        )
    except Exception as e:
        print(f"[AUTOPSY SAVE ERROR] {e}")


def _regime_tag(t: dict) -> str:
    adx = t.get("adx_entry") or 0
    atr = t.get("atr_entry") or 0
    if adx >= ADX_STRONG_THRESHOLD and atr > 0:
        return "TRENDING_VOLATILE"
    if adx >= ADX_STRONG_THRESHOLD:
        return "TRENDING"
    if adx >= ADX_WEAK_THRESHOLD:
        return "MODERATE"
    return "WEAK_CHOPPY"


def run_loss_autopsy(
    strategy: str = None,
    instrument: str = None,
    date_from: str = None,
    date_to: str = None,
    save_results: bool = True,
) -> dict:
    """
    Main entry point. Returns full structured result for HTML renderer.
    """
    bootstrap_analytics_tables()

    all_trades = load_trades(
        strategy=strategy, instrument=instrument,
        date_from=date_from, date_to=date_to,
    )

    if not all_trades:
        return {"error": "No trades found."}

    losses = [t for t in all_trades if t["is_loser"]]
    winners = [t for t in all_trades if t["is_winner"]]

    if not losses:
        return {"error": "No losing trades found — nothing to autopsy."}

    # Classify each loss
    autopsied = []
    for t in losses:
        cause, secondary = _classify_loss(t)
        regime = _regime_tag(t)
        autopsied.append({
            "trade":         t,
            "cause":         cause,
            "secondary":     secondary,
            "regime":        regime,
            "cause_label":   CAUSE_LABELS.get(cause, cause),
            "cause_type":    CAUSE_TYPE.get(cause, "unknown"),
        })
        if save_results:
            try:
                _save_autopsy(t, cause, secondary, regime)
            except Exception:
                pass

    # ── Cause frequency ranking ───────────────────────────────────────────────
    cause_stats = defaultdict(lambda: {"count": 0, "total_loss": 0.0, "trades": []})
    for a in autopsied:
        c = a["cause"]
        cause_stats[c]["count"] += 1
        cause_stats[c]["total_loss"] += a["trade"]["pnl_rs"]   # negative
        cause_stats[c]["trades"].append(a["trade"])

    cause_ranking = []
    for cause, data in sorted(cause_stats.items(),
                              key=lambda x: x[1]["total_loss"]):  # most negative first
        sub_trades = data["trades"]
        cause_ranking.append({
            "cause":       cause,
            "label":       CAUSE_LABELS.get(cause, cause),
            "type":        CAUSE_TYPE.get(cause, "unknown"),
            "count":       data["count"],
            "pct_of_losses": round(data["count"] / len(losses) * 100, 1),
            "total_loss":  round(data["total_loss"], 0),
            "avg_loss":    round(data["total_loss"] / data["count"], 0),
            "win_rate_in_cause": 0.0,   # all are losses by definition
        })

    # ── Behavioral vs System split ────────────────────────────────────────────
    behavioral_loss = sum(
        a["trade"]["pnl_rs"] for a in autopsied
        if CAUSE_TYPE.get(a["cause"]) == "behavioral"
    )
    system_loss = sum(
        a["trade"]["pnl_rs"] for a in autopsied
        if CAUSE_TYPE.get(a["cause"]) == "system"
    )
    behavioral_count = sum(1 for a in autopsied if CAUSE_TYPE.get(a["cause"]) == "behavioral")
    system_count     = sum(1 for a in autopsied if CAUSE_TYPE.get(a["cause"]) == "system")

    # ── Loss by time bucket ───────────────────────────────────────────────────
    time_buckets = {
        "09:15–10:00": [],
        "10:00–11:00": [],
        "11:00–12:00": [],
        "12:00–13:00": [],
        "13:00–14:00": [],
        "14:00–15:30": [],
    }
    for a in autopsied:
        et = a["trade"].get("entry_time") or ""
        if   "09:15" <= et < "10:00": time_buckets["09:15–10:00"].append(a["trade"])
        elif "10:00" <= et < "11:00": time_buckets["10:00–11:00"].append(a["trade"])
        elif "11:00" <= et < "12:00": time_buckets["11:00–12:00"].append(a["trade"])
        elif "12:00" <= et < "13:00": time_buckets["12:00–13:00"].append(a["trade"])
        elif "13:00" <= et < "14:00": time_buckets["13:00–14:00"].append(a["trade"])
        else:                          time_buckets["14:00–15:30"].append(a["trade"])

    loss_by_time = []
    for bucket, bucket_trades in time_buckets.items():
        if bucket_trades:
            loss_by_time.append({
                "bucket":     bucket,
                "count":      len(bucket_trades),
                "total_loss": round(sum(t["pnl_rs"] for t in bucket_trades), 0),
                "avg_loss":   round(sum(t["pnl_rs"] for t in bucket_trades) / len(bucket_trades), 0),
            })

    # ── Loss by instrument ────────────────────────────────────────────────────
    instr_losses = defaultdict(list)
    for a in autopsied:
        instr_losses[a["trade"].get("instrument", "UNKNOWN")].append(a["trade"])

    loss_by_instrument = []
    for inst, inst_trades in sorted(instr_losses.items(),
                                    key=lambda x: sum(t["pnl_rs"] for t in x[1])):
        all_inst = [t for t in all_trades if t.get("instrument") == inst]
        loss_by_instrument.append({
            "instrument":  inst,
            "loss_count":  len(inst_trades),
            "total_loss":  round(sum(t["pnl_rs"] for t in inst_trades), 0),
            "total_trades": len(all_inst),
            "loss_rate":   round(len(inst_trades) / len(all_inst) * 100, 1) if all_inst else 0,
        })

    # ── ADX performance bracket ───────────────────────────────────────────────
    adx_brackets = [
        ("< 20",  0,  20),
        ("20–25", 20, 25),
        ("25–30", 25, 30),
        ("30–40", 30, 40),
        ("40+",   40, 999),
    ]
    adx_analysis = []
    for label, lo, hi in adx_brackets:
        bracket_trades = [t for t in all_trades
                          if lo <= (t.get("adx_entry") or 0) < hi]
        if bracket_trades:
            wr  = win_rate(bracket_trades)
            pf  = profit_factor(bracket_trades)
            net = sum(t["pnl_rs"] for t in bracket_trades)
            adx_analysis.append({
                "bracket":  label,
                "n":        len(bracket_trades),
                "win_rate": round((wr or 0) * 100, 1),
                "pf":       round(pf, 2) if pf else None,
                "net_pnl":  round(net, 0),
            })

    # ── Recommendations ───────────────────────────────────────────────────────
    recommendations = []

    # Top cause
    if cause_ranking:
        top = cause_ranking[0]
        if top["cause"] == "WEAK_TREND":
            recommendations.append({
                "priority": "HIGH",
                "action":   f"Enforce ADX ≥ {ADX_STRONG_THRESHOLD} entry filter",
                "rationale": f"{top['count']} losses ({top['pct_of_losses']}%) classified as weak-trend entries. Total damage: ₹{abs(top['total_loss']):,.0f}",
                "test":     "Apply ADX filter in What-If engine to quantify improvement.",
            })
        elif top["cause"] == "BAD_SESSION":
            recommendations.append({
                "priority": "HIGH",
                "action":   "Stop trading between 13:00–14:00",
                "rationale": f"{top['count']} losses in identified weak session window.",
                "test":     "Run Cooling Simulator for session-time filter.",
            })
        elif top["cause"] == "PREMATURE_ENTRY":
            recommendations.append({
                "priority": "HIGH",
                "action":   "Add confirmation delay or secondary signal before entry",
                "rationale": f"{top['count']} losses from quick reversals (< {PREMATURE_MAX_HOLD} min hold).",
                "test":     "Review entry indicators at time of these specific trades.",
            })

    # Behavioral if significant
    if behavioral_count > 0 and behavioral_count >= system_count:
        recommendations.append({
            "priority": "MEDIUM",
            "action":   "Review execution discipline — majority of losses are behavioral",
            "rationale": f"{behavioral_count} behavioral losses (₹{abs(behavioral_loss):,.0f}) vs {system_count} system losses.",
            "test":     "Log reason for entry at trade time. Compare to post-hoc classification.",
        })

    today = datetime.now().strftime("%Y-%m-%d")

    return {
        "module":          "loss_autopsy",
        "generated_at":    today,
        "strategy_filter": strategy,
        "date_range": {
            "start": all_trades[0]["date"] if all_trades else None,
            "end":   all_trades[-1]["date"] if all_trades else None,
        },
        "summary": {
            "total_trades":    len(all_trades),
            "total_losses":    len(losses),
            "total_winners":   len(winners),
            "win_rate":        round((win_rate(all_trades) or 0) * 100, 1),
            "gross_loss":      round(sum(t["pnl_rs"] for t in losses), 0),
            "behavioral_loss": round(behavioral_loss, 0),
            "system_loss":     round(system_loss, 0),
            "behavioral_count": behavioral_count,
            "system_count":     system_count,
            "classified_pct":  round(
                sum(1 for a in autopsied if a["cause"] != "UNDEFINED") / len(autopsied) * 100, 1
            ) if autopsied else 0,
        },
        "cause_ranking":      cause_ranking,
        "loss_by_time":       loss_by_time,
        "loss_by_instrument": loss_by_instrument,
        "adx_analysis":       adx_analysis,
        "autopsied_trades":   [
            {
                "trade_id":    a["trade"].get("id"),
                "date":        a["trade"]["date"],
                "entry_time":  a["trade"].get("entry_time"),
                "symbol":      a["trade"].get("symbol"),
                "pnl_rs":      a["trade"]["pnl_rs"],
                "cause":       a["cause"],
                "cause_label": a["cause_label"],
                "cause_type":  a["cause_type"],
                "secondary":   a["secondary"],
                "regime":      a["regime"],
                "adx":         a["trade"].get("adx_entry"),
                "hold_min":    a["trade"].get("duration_min"),
                "mae":         abs(a["trade"].get("mae_pts") or 0),
            }
            for a in autopsied
        ],
        "recommendations": recommendations,
        "cause_labels":    CAUSE_LABELS,
        "cause_types":     CAUSE_TYPE,
    }
