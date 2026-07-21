"""
sage_enricher.py — Trade Story Builder
=======================================
Reads every trade from trading.db (PIT's DB), builds a feature
vector from the columns that exist, and writes enriched Trade
Stories into sage.db.

For the 142 historical trades: partial enrichment (no VIX, no
real-time breadth — only what was logged at trade time).

For future trades: full enrichment called by the strategy right
after trade closes (passes live market context).

Feature vector v1 (from existing columns only):
  [0]  adx_norm           ADX / 50 (normalised 0–1)
  [1]  ema_spread         (ema9 - ema21) / ema21
  [2]  vwap_distance      (entry_price - vwap) / vwap
  [3]  orb_position       (entry_price - orb_low) / (orb_high - orb_low)
  [4]  time_slot          entry hour + minute/60  (continuous 9.3–15.5)
  [5]  direction_num      1=CE/BUY  -1=PE
  [6]  oi_bias_num        1=bullish  -1=bearish  0=neutral
  [7]  trade_num_norm     trade_num_today / 5 (capped)
  [8]  running_pnl_norm   running_pnl_before / 5000 (scaled)

Feature vector v2 (added when enricher captures live context):
  [9]  vix_norm           vix / 30
  [10] nifty_chg          nifty_change_pct / 3.0
  [11] breadth            breadth_pct (0–1)
  [12] session_age        session_age_min / 390
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from sage_db import get_sage_db

TRADING_DB = Path.home() / "openalgo" / "trading.db"


def _safe_float(v, default=0.0):
    try:
        return float(v) if v is not None else default
    except Exception:
        return default


def _parse_time(t_str: str) -> float:
    """Convert HH:MM or HH:MM:SS to float hours (e.g. 10:23 → 10.383)."""
    if not t_str:
        return 0.0
    try:
        parts = t_str.split(":")
        h, m = int(parts[0]), int(parts[1])
        return h + m / 60.0
    except Exception:
        return 0.0


def _direction_num(direction: str) -> float:
    if direction in ("CE", "BUY"):
        return 1.0
    if direction == "PE":
        return -1.0
    return 0.0


def _oi_bias_num(oi_bias: str) -> float:
    if not oi_bias:
        return 0.0
    oi_lower = oi_bias.lower()
    if any(w in oi_lower for w in ("bull", "call", "long", "up")):
        return 1.0
    if any(w in oi_lower for w in ("bear", "put", "short", "down")):
        return -1.0
    return 0.0


def _build_feature_v1(row: dict, trade_num: int, running_pnl: float) -> list:
    adx   = _safe_float(row.get("adx_entry"), 25.0)
    ema9  = _safe_float(row.get("ema9_entry"))
    ema21 = _safe_float(row.get("ema21_entry"), 1.0)
    ep    = _safe_float(row.get("entry_price"), 1.0)
    vwap  = _safe_float(row.get("vwap_entry"), ep)
    orb_h = _safe_float(row.get("orb_high"), ep)
    orb_l = _safe_float(row.get("orb_low"), ep)

    ema_spread    = (ema9 - ema21) / max(abs(ema21), 0.001)
    vwap_dist     = (ep - vwap) / max(abs(vwap), 0.001)
    orb_range     = max(orb_h - orb_l, 0.001)
    orb_pos       = max(0.0, min(1.0, (ep - orb_l) / orb_range))
    time_slot     = _parse_time(row.get("entry_time"))
    dir_num       = _direction_num(row.get("direction", ""))
    oi_num        = _oi_bias_num(row.get("oi_bias", ""))
    trade_norm    = min(trade_num, 5) / 5.0
    pnl_norm      = max(-1.0, min(1.0, running_pnl / 5000.0))

    return [
        round(adx / 50.0, 4),
        round(ema_spread, 4),
        round(vwap_dist, 4),
        round(orb_pos, 4),
        round(time_slot / 15.5, 4),   # normalise to 0–1 over full day
        round(dir_num, 4),
        round(oi_num, 4),
        round(trade_norm, 4),
        round(pnl_norm, 4),
    ]


def _build_feature_v2(v1: list, vix: float, nifty_chg: float,
                       breadth_pct: float, session_age_min: int) -> list:
    return v1 + [
        round(min(vix, 30.0) / 30.0, 4),
        round(max(-1.0, min(1.0, nifty_chg / 3.0)), 4),
        round(max(0.0, min(1.0, breadth_pct)), 4),
        round(min(session_age_min, 390) / 390.0, 4),
    ]


def _outcome(pnl_rs: float) -> str:
    if pnl_rs > 50:
        return "WIN"
    if pnl_rs < -50:
        return "LOSS"
    return "BE"


def _load_trading_db_trades() -> list:
    if not TRADING_DB.exists():
        return []
    conn = sqlite3.connect(str(TRADING_DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM trades ORDER BY date, entry_time"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def backfill_stories(verbose: bool = True) -> int:
    """
    Backfill Trade Stories for all historical trades in trading.db.
    Safe to run multiple times — uses INSERT OR IGNORE on trade_id.
    Returns count of new stories written.
    """
    db = get_sage_db()
    trades = _load_trading_db_trades()
    if not trades:
        print("[SAGE enricher] No trades found in trading.db")
        return 0

    written = 0
    # track per-day state for trade_num and running_pnl
    day_state: dict = {}  # date -> {"count": int, "pnl": float}

    for row in trades:
        trade_id = row.get("id")
        if trade_id is None:
            continue

        existing = db.query("SELECT id FROM trade_stories WHERE trade_id=?", (trade_id,))
        if existing:
            continue

        date = row.get("date", "")
        if date not in day_state:
            day_state[date] = {"count": 0, "pnl": 0.0}

        day_state[date]["count"] += 1
        trade_num    = day_state[date]["count"]
        running_pnl  = day_state[date]["pnl"]

        fv = _build_feature_v1(row, trade_num, running_pnl)

        story = {
            "trade_id":          trade_id,
            "date":              date,
            "entry_time":        row.get("entry_time"),
            "exit_time":         row.get("exit_time"),
            "strategy":          row.get("strategy"),
            "instrument":        row.get("instrument"),
            "direction":         row.get("direction"),
            "symbol":            row.get("symbol"),
            "pnl_pts":           row.get("pnl_pts"),
            "pnl_rs":            row.get("pnl_rs"),
            "mfe_pts":           row.get("mfe_pts"),
            "mae_pts":           row.get("mae_pts"),
            "capture_pct":       row.get("capture_pct"),
            "exit_reason":       row.get("exit_reason"),
            "hold_seconds":      row.get("hold_seconds"),
            "outcome":           _outcome(_safe_float(row.get("pnl_rs"))),
            "adx_entry":         row.get("adx_entry"),
            "ema9_entry":        row.get("ema9_entry"),
            "ema21_entry":       row.get("ema21_entry"),
            "vwap_entry":        row.get("vwap_entry"),
            "orb_high":          row.get("orb_high"),
            "orb_low":           row.get("orb_low"),
            "oi_bias":           row.get("oi_bias"),
            "momentum":          row.get("momentum"),
            "trend_score":       row.get("trend_score"),
            "trade_num_today":   trade_num,
            "running_pnl_before": running_pnl,
            "feature_vector":    json.dumps(fv),
            "feature_version":   "v1",
            "enriched":          0,
        }

        db.upsert_story(trade_id, story)
        day_state[date]["pnl"] += _safe_float(row.get("pnl_rs"))
        written += 1

    if verbose:
        print(f"[SAGE enricher] Backfilled {written} new stories "
              f"({len(trades)} total trades in DB)")
    return written


def enrich_live_trade(
    trade_id: int,
    vix: float,
    nifty_change_pct: float,
    breadth_pct: float,
    session_age_min: int,
) -> bool:
    """
    Call this right after a trade closes during live trading.
    Upgrades the story from v1 to v2 feature vector with live context.
    """
    db = get_sage_db()
    story = db.query("SELECT * FROM trade_stories WHERE trade_id=?", (trade_id,))
    if not story:
        print(f"[SAGE enricher] Story not found for trade_id={trade_id}. "
              "Run backfill first.")
        return False

    s = story[0]
    existing_fv = json.loads(s.get("feature_vector") or "[]")

    # if already v2, keep it
    if len(existing_fv) >= 13:
        return True

    fv_v2 = _build_feature_v2(
        existing_fv[:9], vix, nifty_change_pct, breadth_pct, session_age_min
    )

    db.upsert_story(trade_id, {
        "vix":              vix,
        "nifty_change_pct": nifty_change_pct,
        "breadth_pct":      breadth_pct,
        "session_age_min":  session_age_min,
        "feature_vector":   json.dumps(fv_v2),
        "feature_version":  "v2",
        "enriched":         1,
    })
    return True


def story_count() -> dict:
    db = get_sage_db()
    total  = db.query("SELECT COUNT(*) as n FROM trade_stories")[0]["n"]
    v2     = db.query("SELECT COUNT(*) as n FROM trade_stories WHERE enriched=1")[0]["n"]
    wins   = db.query("SELECT COUNT(*) as n FROM trade_stories WHERE outcome='WIN'")[0]["n"]
    losses = db.query("SELECT COUNT(*) as n FROM trade_stories WHERE outcome='LOSS'")[0]["n"]
    return {"total": total, "v2_enriched": v2, "wins": wins, "losses": losses}


if __name__ == "__main__":
    backfill_stories(verbose=True)
    counts = story_count()
    print(f"Stories: {counts['total']} total | "
          f"{counts['v2_enriched']} fully enriched | "
          f"{counts['wins']}W {counts['losses']}L")
