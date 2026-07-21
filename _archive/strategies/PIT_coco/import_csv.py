"""
import_csv.py — Import historical CSV data into trading.db
===========================================================
Run once to backfill all historical trades from master CSVs.
Safe to run multiple times — skips duplicates.

Usage:
    python3 import_csv.py
"""

import csv
import sys
from pathlib import Path

# Make this suite runnable standalone from its own directory,
# regardless of folder name (PIT, PIT_coco, etc.).
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path.home() / "openalgo"))
from db_logger import get_db

CSV_FILES = [
    Path.home() / "openalgo" / "logs" / "scalper_master.csv",
    Path.home() / "openalgo" / "logs" / "trend_rider_master.csv",
]

DB_COLUMNS = [
    "date", "entry_time", "exit_time", "strategy", "instrument",
    "direction", "symbol", "strike_type",
    "entry_price", "exit_price", "quantity",
    "pnl_pts", "pnl_rs", "mfe_pts", "mae_pts", "capture_pct",
    "exit_reason", "hold_seconds",
    "adx_entry", "atr_entry", "ema9_entry", "ema21_entry", "vwap_entry",
    "orb_high", "orb_low", "max_pain", "max_pain_bias", "oi_bias",
    "momentum", "be_triggered", "trail_count", "high_water_pts",
    "trend_score", "running_total",
]

def _clean(row: dict) -> dict:
    """Normalise a CSV row to DB-compatible types."""
    clean = {}
    for col in DB_COLUMNS:
        val = row.get(col, "")
        if val == "" or val is None:
            clean[col] = None
        else:
            # Numeric columns
            if col in ("entry_price","exit_price","pnl_pts","pnl_rs",
                       "mfe_pts","mae_pts","capture_pct","adx_entry",
                       "atr_entry","ema9_entry","ema21_entry","vwap_entry",
                       "orb_high","orb_low","max_pain","high_water_pts",
                       "trend_score","running_total"):
                try:    clean[col] = float(val)
                except: clean[col] = None
            elif col in ("quantity","hold_seconds","trail_count"):
                try:    clean[col] = int(float(val))
                except: clean[col] = None
            else:
                clean[col] = str(val).strip()
    return clean

def already_exists(db, date, entry_time, symbol, strategy) -> bool:
    rows = db.query(
        "SELECT id FROM trades WHERE date=? AND entry_time=? AND symbol=? AND strategy=?",
        (date, entry_time, symbol, strategy)
    )
    return len(rows) > 0

def main():
    db = get_db()
    total_imported = 0
    total_skipped  = 0

    for csv_path in CSV_FILES:
        if not csv_path.exists():
            print(f"  NOT FOUND: {csv_path}")
            continue

        print(f"\nImporting: {csv_path.name}")
        with open(csv_path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                clean = _clean(row)

                # Skip if already in DB
                if already_exists(db,
                    clean.get("date"), clean.get("entry_time"),
                    clean.get("symbol"), clean.get("strategy")):
                    total_skipped += 1
                    continue

                try:
                    db.insert_trade(clean)
                    total_imported += 1
                except Exception as e:
                    print(f"  ERROR: {e} | row: {clean.get('symbol')} {clean.get('date')}")

        print(f"  Done.")

    print(f"\n{'='*50}")
    print(f"Import complete!")
    print(f"  Imported : {total_imported} trades")
    print(f"  Skipped  : {total_skipped} (already in DB)")

    # Verify
    rows = db.query("SELECT strategy, COUNT(*) as cnt FROM trades GROUP BY strategy")
    print(f"\nDB now contains:")
    for r in rows:
        print(f"  {r['strategy']:<20}: {r['cnt']} trades")
    total = db.query("SELECT COUNT(*) as n FROM trades")[0]["n"]
    print(f"  {'TOTAL':<20}: {total} trades")

if __name__ == "__main__":
    main()