"""
cooling_simulator.py — Cooling Period Simulator
================================================
Simulates what would have happened if the strategy had imposed
mandatory rest periods after specific trigger events.

Trigger events:
  - SL hit
  - TARGET hit
  - N consecutive losses

Cooldown durations tested: 5, 10, 15, 20 minutes

For each (trigger × duration) combination:
  - Identifies trades that would have been skipped
  - Recomputes P&L, WR, PF, Expectancy on the reduced trade set
  - Compares against actual
  - Issues verdict: ADOPT / TEST / SKIP

Results persisted to whatif_results table (shared with What-If engine).

Usage:
    from cooling_simulator import run_cooling_simulator
    result = run_cooling_simulator()
    result = run_cooling_simulator(strategy="scalper", date_from="2026-01-01")
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from analytics_core import (
    load_trades, profit_factor, win_rate, expectancy,
    max_drawdown, bootstrap_analytics_tables, get_db,
    safe_div, normalize_exit_reason
)

COOLDOWNS_MINUTES = [5, 10, 15, 20]

TRIGGER_SL         = "SL"
TRIGGER_TARGET     = "TARGET"
TRIGGER_CONSEC_2L  = "CONSEC_2L"   # 2 consecutive losses
TRIGGER_CONSEC_3L  = "CONSEC_3L"   # 3 consecutive losses


def _parse_time(t_str: str) -> datetime:
    """Parse HH:MM or HH:MM:SS to today-anchored datetime for arithmetic."""
    if not t_str: return None
    try:
        parts = t_str.split(":")
        h, m = int(parts[0]), int(parts[1])
        s = int(parts[2]) if len(parts) > 2 else 0
        return datetime(2000, 1, 1, h, m, s)
    except Exception:
        return None


def _time_diff_minutes(t1_str: str, t2_str: str) -> float:
    """Minutes between two HH:MM time strings. Returns None if either invalid."""
    t1 = _parse_time(t1_str)
    t2 = _parse_time(t2_str)
    if t1 is None or t2 is None: return None
    diff = (t2 - t1).total_seconds() / 60
    return diff


def _simulate_cooldown(
    trades: list[dict],
    trigger: str,
    cooldown_min: int,
) -> dict:
    """
    Core simulation: given sorted trades, apply cooldown rule and return
    which trades would have been skipped.

    Returns dict with removed_ids, kept_trades, skipped_trades.
    """
    kept    = []
    skipped = []
    cooldown_until = None   # datetime: no trades until this time

    consecutive_losses = 0

    for i, t in enumerate(trades):
        entry_dt = _parse_time(t.get("entry_time", ""))
        exit_dt  = _parse_time(t.get("exit_time", ""))

        # Check if this trade falls within a cooldown window
        in_cooldown = (
            cooldown_until is not None
            and entry_dt is not None
            and entry_dt < cooldown_until
        )

        if in_cooldown:
            skipped.append(t)
            # Consecutive loss tracking still needs to happen
            # (can't know outcome of skipped trade, so we don't update streak)
            continue

        # Trade is kept — now check if it triggers a new cooldown
        kept.append(t)
        exit_reason = normalize_exit_reason(t.get("exit_reason"))
        is_loss = t["is_loser"]

        # Update consecutive loss counter
        if is_loss:
            consecutive_losses += 1
        else:
            consecutive_losses = 0

        # Determine if this trade triggers a cooldown
        trigger_fires = False
        if trigger == TRIGGER_SL and exit_reason in ("SL_HIT", "INITIAL_SL"):
            trigger_fires = True
        elif trigger == TRIGGER_TARGET and exit_reason in (
            "TARGET_HIT", "TRAILING_SL", "HIGH_WATER_EXIT"
        ):
            trigger_fires = True
        elif trigger == TRIGGER_CONSEC_2L and consecutive_losses >= 2:
            trigger_fires = True
        elif trigger == TRIGGER_CONSEC_3L and consecutive_losses >= 3:
            trigger_fires = True

        if trigger_fires and exit_dt is not None:
            cooldown_until = exit_dt + timedelta(minutes=cooldown_min)
        elif trigger_fires:
            # No exit time — use entry time as fallback
            if entry_dt:
                cooldown_until = entry_dt + timedelta(minutes=cooldown_min)

    return {
        "kept":    kept,
        "skipped": skipped,
    }


def _compute_stats(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0, "win_rate": 0, "profit_factor": None,
                "expectancy": 0, "net_pnl": 0, "max_dd": 0}
    wr  = win_rate(trades) or 0
    pf  = profit_factor(trades)
    exp = expectancy(trades) or 0
    net = sum(t["pnl_rs"] for t in trades)
    dd  = max_drawdown(trades)
    return {
        "n":             len(trades),
        "win_rate":      wr,
        "profit_factor": pf,
        "expectancy":    exp,
        "net_pnl":       net,
        "max_dd":        dd,
    }


def _verdict(actual_pnl: float, sim_pnl: float, n_removed: int, n_total: int) -> str:
    """
    ADOPT  → sim clearly better AND meaningful sample removed
    TEST   → sim better but marginal, or small sample
    SKIP   → no improvement
    """
    if n_total == 0: return "SKIP"
    pct_removed = n_removed / n_total

    delta = sim_pnl - actual_pnl
    if delta <= 0:
        return "SKIP"
    if delta > 0 and pct_removed < 0.05:
        return "SKIP"   # removed too few trades to matter
    if delta >= actual_pnl * 0.15 or (delta > 0 and n_removed >= 3):
        return "ADOPT"
    return "TEST"


def _save_result(run_date, filter_name, param, date_from, date_to,
                 actual_stats, sim_stats, verdict):
    db = get_db()
    try:
        db.query(
            """INSERT INTO whatif_results
               (run_date, filter_name, filter_param,
                date_range_start, date_range_end,
                actual_trades, filtered_trades, removed_trades,
                actual_pnl, sim_pnl, pnl_delta,
                actual_wr, sim_wr,
                actual_pf, sim_pf,
                actual_expectancy, sim_expectancy,
                verdict)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_date, filter_name, json.dumps(param),
             date_from, date_to,
             actual_stats["n"], sim_stats["n"],
             actual_stats["n"] - sim_stats["n"],
             actual_stats["net_pnl"], sim_stats["net_pnl"],
             sim_stats["net_pnl"] - actual_stats["net_pnl"],
             actual_stats["win_rate"], sim_stats["win_rate"],
             actual_stats["profit_factor"], sim_stats["profit_factor"],
             actual_stats["expectancy"], sim_stats["expectancy"],
             verdict)
        )
    except Exception as e:
        print(f"[COOLING SAVE ERROR] {e}")


TRIGGER_LABELS = {
    TRIGGER_SL:        "After Stop Loss",
    TRIGGER_TARGET:    "After Target / Trail",
    TRIGGER_CONSEC_2L: "After 2 Consecutive Losses",
    TRIGGER_CONSEC_3L: "After 3 Consecutive Losses",
}


def run_cooling_simulator(
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

    trades = load_trades(
        strategy=strategy, instrument=instrument,
        date_from=date_from, date_to=date_to,
    )

    if not trades:
        return {"error": "No trades found."}

    actual_stats = _compute_stats(trades)
    today = datetime.now().strftime("%Y-%m-%d")

    # Group trades by date to apply cooldown intra-day
    # (cooldown resets each morning — don't bleed across sessions)
    by_date = {}
    for t in trades:
        by_date.setdefault(t["date"], []).append(t)

    results = []

    for trigger in [TRIGGER_SL, TRIGGER_TARGET, TRIGGER_CONSEC_2L, TRIGGER_CONSEC_3L]:
        for cd_min in COOLDOWNS_MINUTES:
            # Simulate across all days
            all_kept    = []
            all_skipped = []
            for day_trades in sorted(by_date.values(), key=lambda x: x[0]["date"]):
                sim = _simulate_cooldown(day_trades, trigger, cd_min)
                all_kept.extend(sim["kept"])
                all_skipped.extend(sim["skipped"])

            sim_stats = _compute_stats(all_kept)
            v = _verdict(
                actual_stats["net_pnl"], sim_stats["net_pnl"],
                len(all_skipped), len(trades)
            )

            pnl_delta = sim_stats["net_pnl"] - actual_stats["net_pnl"]
            wr_delta  = (sim_stats["win_rate"] - actual_stats["win_rate"]) if sim_stats["win_rate"] else None
            pf_delta  = None
            if sim_stats["profit_factor"] and actual_stats["profit_factor"]:
                pf_delta = sim_stats["profit_factor"] - actual_stats["profit_factor"]
            exp_delta = sim_stats["expectancy"] - actual_stats["expectancy"]

            entry = {
                "trigger":        trigger,
                "trigger_label":  TRIGGER_LABELS[trigger],
                "cooldown_min":   cd_min,
                "trades_removed": len(all_skipped),
                "trades_kept":    len(all_kept),
                "actual": {
                    "n":             actual_stats["n"],
                    "net_pnl":       actual_stats["net_pnl"],
                    "win_rate":      actual_stats["win_rate"],
                    "profit_factor": actual_stats["profit_factor"],
                    "expectancy":    actual_stats["expectancy"],
                    "max_dd":        actual_stats["max_dd"],
                },
                "simulated": {
                    "n":             sim_stats["n"],
                    "net_pnl":       sim_stats["net_pnl"],
                    "win_rate":      sim_stats["win_rate"],
                    "profit_factor": sim_stats["profit_factor"],
                    "expectancy":    sim_stats["expectancy"],
                    "max_dd":        sim_stats["max_dd"],
                },
                "delta": {
                    "pnl":           pnl_delta,
                    "win_rate":      wr_delta,
                    "profit_factor": pf_delta,
                    "expectancy":    exp_delta,
                    "max_dd":        sim_stats["max_dd"] - actual_stats["max_dd"],
                },
                "skipped_trades": [
                    {
                        "date":        t["date"],
                        "entry_time":  t.get("entry_time"),
                        "symbol":      t.get("symbol"),
                        "pnl_rs":      t["pnl_rs"],
                        "exit_reason": t.get("exit_reason"),
                    }
                    for t in all_skipped
                ],
                "verdict": v,
            }
            results.append(entry)

            if save_results:
                try:
                    _save_result(
                        today,
                        f"cooldown_{trigger}_{cd_min}min",
                        {"trigger": trigger, "cooldown_min": cd_min},
                        date_from, date_to,
                        actual_stats, sim_stats, v
                    )
                except Exception:
                    pass

    # Best single recommendation
    adopt_results = [r for r in results if r["verdict"] == "ADOPT"]
    best = None
    if adopt_results:
        best = max(adopt_results, key=lambda r: r["delta"]["pnl"])

    # Summary matrix: best cooldown per trigger
    trigger_summary = {}
    for trigger in [TRIGGER_SL, TRIGGER_TARGET, TRIGGER_CONSEC_2L, TRIGGER_CONSEC_3L]:
        trigger_rows = [r for r in results if r["trigger"] == trigger]
        if trigger_rows:
            best_for_trigger = max(trigger_rows, key=lambda r: r["delta"]["pnl"])
            trigger_summary[trigger] = best_for_trigger

    return {
        "module":          "cooling_simulator",
        "generated_at":    today,
        "strategy_filter": strategy,
        "date_range": {
            "start": trades[0]["date"] if trades else None,
            "end":   trades[-1]["date"] if trades else None,
        },
        "actual_stats":    actual_stats,
        "results":         results,
        "trigger_summary": trigger_summary,
        "best_recommendation": best,
        "cooldown_durations":  COOLDOWNS_MINUTES,
        "triggers":            list(TRIGGER_LABELS.keys()),
        "trigger_labels":      TRIGGER_LABELS,
    }
