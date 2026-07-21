"""
sage_evening_verdict.py — Evening Hypothesis Evaluator
======================================================
Runs after market close (15:30+ IST).
Reads today's morning hypothesis from sage.db, compares
against actual trading.db results, and scores SAGE.

Writes:
  logs/YYYY-MM-DD/evening_verdict.json
  Updates sage_master.csv with actual results
  Updates daily_hypotheses in sage.db

Usage:
    uv run python3 sage_evening_verdict.py
    uv run python3 sage_evening_verdict.py --date 2026-06-16
"""

import csv
import json
from datetime import datetime
from pathlib import Path
import sys
import sqlite3

import pytz

sys.path.insert(0, str(Path(__file__).parent))
from sage_db import get_sage_db
from sage_enricher import _safe_float, backfill_stories

IST        = pytz.timezone("Asia/Kolkata")
LOGS_DIR   = Path(__file__).parent / "logs"
MASTER_CSV = LOGS_DIR / "sage_master.csv"
TRADING_DB = Path.home() / "openalgo" / "trading.db"


def _load_today_trades(date: str) -> list:
    if not TRADING_DB.exists():
        return []
    conn = sqlite3.connect(str(TRADING_DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM trades WHERE date=? ORDER BY entry_time",
        (date,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _actual_regime(trades: list, actual_pnl: float) -> str:
    if not trades:
        return "NO_TRADES"
    adx_vals = [_safe_float(t.get("adx_entry"), 25.0) for t in trades]
    avg_adx  = sum(adx_vals) / len(adx_vals)
    if avg_adx >= 26 and actual_pnl > 500:
        return "TRENDING"
    if avg_adx <= 21 or actual_pnl < -800:
        return "RANGE"
    return "MIXED"


def _actual_direction(trades: list) -> str:
    dirs = [t.get("direction", "") for t in trades]
    ce = dirs.count("CE") + dirs.count("BUY")
    pe = dirs.count("PE")
    if ce > pe * 1.5:
        return "CE"
    if pe > ce * 1.5:
        return "PE"
    return "NEUTRAL"


def _update_master_csv(date: str, update: dict) -> None:
    if not MASTER_CSV.exists():
        return
    rows = []
    with open(MASTER_CSV, newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        for row in reader:
            if row.get("date") == date:
                row.update(update)
            rows.append(row)

    with open(MASTER_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def generate_evening_verdict(date: str = None) -> dict:
    if date is None:
        date = datetime.now(IST).strftime("%Y-%m-%d")

    db = get_sage_db()

    # load morning hypothesis
    hyp_rows = db.query(
        "SELECT * FROM daily_hypotheses WHERE date=?", (date,)
    )
    if not hyp_rows:
        print(f"[SAGE] No morning hypothesis found for {date}. "
              "Run sage_morning_brief.py first.")
        return {"error": "No hypothesis found for this date."}

    hyp = hyp_rows[0]

    # enrich new trades
    backfill_stories(verbose=False)

    # load actual trades
    trades   = _load_today_trades(date)
    n_trades = len(trades)

    if n_trades == 0:
        verdict = {
            "date":    date,
            "status":  "NO_TRADES",
            "message": "No trades recorded today.",
        }
        (LOGS_DIR / date).mkdir(parents=True, exist_ok=True)
        (LOGS_DIR / date / "evening_verdict.json").write_text(
            json.dumps(verdict, indent=2), encoding="utf-8"
        )
        return verdict

    actual_pnl = sum(_safe_float(t.get("pnl_rs")) for t in trades)
    wins       = sum(1 for t in trades if _safe_float(t.get("pnl_rs")) > 50)
    wr         = wins / n_trades
    act_regime = _actual_regime(trades, actual_pnl)
    act_dir    = _actual_direction(trades)

    # score the hypothesis
    pred_regime = hyp.get("regime", "")
    pred_dir    = hyp.get("direction_bias", "NEUTRAL")
    pred_low    = _safe_float(hyp.get("pnl_low"))
    pred_high   = _safe_float(hyp.get("pnl_high"))

    regime_correct    = int(pred_regime == act_regime)
    dir_correct       = int(pred_dir == act_dir or pred_dir == "NEUTRAL")
    pnl_in_range      = int(pred_low <= actual_pnl <= pred_high)
    hypothesis_correct = int(regime_correct and dir_correct)

    # build verdict text
    notes = []
    notes.append(f"Predicted {pred_regime} → Actual {act_regime} "
                 f"({'✓' if regime_correct else '✗'})")
    notes.append(f"Predicted bias {pred_dir} → Actual {act_dir} "
                 f"({'✓' if dir_correct else '✗'})")
    notes.append(f"P&L range ₹{int(pred_low):,}–₹{int(pred_high):,} → "
                 f"Actual ₹{int(actual_pnl):,} "
                 f"({'IN RANGE' if pnl_in_range else 'OUT OF RANGE'})")

    if hypothesis_correct:
        overall = "CORRECT"
    elif regime_correct or dir_correct:
        overall = "PARTIAL"
    else:
        overall = "WRONG"

    verdict = {
        "date":               date,
        "status":             overall,
        "hypothesis_correct": hypothesis_correct,
        "regime_predicted":   pred_regime,
        "regime_actual":      act_regime,
        "regime_correct":     bool(regime_correct),
        "direction_predicted": pred_dir,
        "direction_actual":   act_dir,
        "direction_correct":  bool(dir_correct),
        "pnl_predicted_low":  pred_low,
        "pnl_predicted_high": pred_high,
        "actual_pnl":         round(actual_pnl, 0),
        "pnl_in_range":       bool(pnl_in_range),
        "actual_trades":      n_trades,
        "actual_win_rate":    round(wr, 3),
        "notes":              notes,
        "sage_score":         round(
            (regime_correct * 40 + dir_correct * 30 + pnl_in_range * 30) / 100, 2
        ),
    }

    # write files
    day_dir = LOGS_DIR / date
    day_dir.mkdir(parents=True, exist_ok=True)
    (day_dir / "evening_verdict.json").write_text(
        json.dumps(verdict, indent=2), encoding="utf-8"
    )

    # update sage.db
    db.upsert_hypothesis(date, {
        "actual_pnl":         round(actual_pnl, 0),
        "actual_trades":      n_trades,
        "actual_regime":      act_regime,
        "hypothesis_correct": hypothesis_correct,
        "direction_correct":  dir_correct,
        "pnl_in_range":       pnl_in_range,
        "verdict_notes":      json.dumps(notes),
    })

    # update master CSV
    _update_master_csv(date, {
        "actual_pnl":         round(actual_pnl, 0),
        "actual_trades":      n_trades,
        "hypothesis_correct": hypothesis_correct,
        "direction_correct":  dir_correct,
        "pnl_in_range":       pnl_in_range,
    })

    print(f"[SAGE] Evening verdict: {overall}  "
          f"(regime {'✓' if regime_correct else '✗'}  "
          f"dir {'✓' if dir_correct else '✗'}  "
          f"P&L {'in range' if pnl_in_range else 'out of range'})")
    print(f"[SAGE] Actual: {n_trades} trades  ₹{int(actual_pnl):,}  WR {round(wr*100,1)}%")
    return verdict


def accuracy_summary() -> None:
    """Print rolling accuracy of SAGE hypotheses."""
    db = get_sage_db()
    rows = db.query(
        """SELECT date, hypothesis_correct, direction_correct,
                  pnl_in_range, actual_pnl, regime, direction_bias
           FROM daily_hypotheses
           WHERE hypothesis_correct IS NOT NULL
           ORDER BY date"""
    )
    if not rows:
        print("[SAGE] No verdicts yet.")
        return

    n = len(rows)
    correct  = sum(1 for r in rows if r["hypothesis_correct"])
    dir_ok   = sum(1 for r in rows if r["direction_correct"])
    in_range = sum(1 for r in rows if r["pnl_in_range"])

    print(f"\n{'='*55}")
    print(f"  SAGE ACCURACY SUMMARY  ·  {n} days evaluated")
    print(f"{'='*55}")
    print(f"  Regime + Direction correct : {correct}/{n}  "
          f"({round(correct/n*100,1)}%)")
    print(f"  Direction correct          : {dir_ok}/{n}  "
          f"({round(dir_ok/n*100,1)}%)")
    print(f"  P&L in predicted range     : {in_range}/{n}  "
          f"({round(in_range/n*100,1)}%)")
    print(f"{'='*55}")
    print(f"  {'Date':<12} {'Regime':<10} {'Dir':<8} {'Hyp':>5} {'Dir':>5} {'P&L':>5}")
    for r in rows[-10:]:
        print(f"  {r['date']:<12} {r['regime']:<10} {r['direction_bias']:<8} "
              f"{'✓' if r['hypothesis_correct'] else '✗':>5} "
              f"{'✓' if r['direction_correct'] else '✗':>5} "
              f"{'✓' if r['pnl_in_range'] else '✗':>5}")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="SAGE Evening Verdict")
    p.add_argument("--date",     type=str, default=None)
    p.add_argument("--summary",  action="store_true")
    a = p.parse_args()

    if a.summary:
        accuracy_summary()
    else:
        generate_evening_verdict(date=a.date)
