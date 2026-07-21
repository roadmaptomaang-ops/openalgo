"""
analytics_core.py — Shared analytics foundation for OpenAlgo research platform
===============================================================================
All four priority modules import from here.
Handles:
  - DB access (wraps db_logger.DBLogger)
  - Schema bootstrap for analytics tables
  - Reusable statistical primitives
  - Trade normalization and enrichment
  - Rolling window utilities

No display logic lives here. Pure computation.
"""

import sqlite3
import statistics
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
import sys

# ── resolve db_logger from ~/openalgo ────────────────────────────────────────
sys.path.insert(0, str(Path.home() / "openalgo"))
try:
    from db_logger import get_db, DBLogger, DB_PATH
except ImportError:
    # Fallback: construct DBLogger directly for environments where openalgo
    # isn't installed yet (e.g. standalone testing)
    DB_PATH = Path.home() / "openalgo" / "trading.db"
    class _StubDB:
        def query(self, sql, params=()):
            conn = sqlite3.connect(str(DB_PATH))
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(sql, params).fetchall()
                return [dict(r) for r in rows]
            except Exception as e:
                print(f"[CORE DB ERROR] {e}")
                return []
            finally:
                conn.close()
    def get_db(): return _StubDB()

# ── Analytics tables DDL ──────────────────────────────────────────────────────
_ANALYTICS_DDL = """
CREATE TABLE IF NOT EXISTS edge_decay_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date   TEXT NOT NULL,
    window_label    TEXT NOT NULL,          -- '7d' | '14d' | '30d' | 'all'
    trade_count     INTEGER,
    win_rate        REAL,
    profit_factor   REAL,
    expectancy      REAL,
    avg_trade       REAL,
    avg_winner      REAL,
    avg_loser       REAL,
    max_drawdown    REAL,
    sharpe_proxy    REAL,                   -- expectancy / stdev_trade
    decay_flag      TEXT,                   -- 'OK' | 'WARN' | 'ALERT'
    decay_reason    TEXT,
    created_at      TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_edge_date ON edge_decay_snapshots(snapshot_date);

CREATE TABLE IF NOT EXISTS loss_autopsy (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id        INTEGER,               -- FK → trades.id
    date            TEXT,
    symbol          TEXT,
    pnl_rs          REAL,
    primary_cause   TEXT,                  -- one of the 8 failure categories
    secondary_cause TEXT,
    adx_at_entry    REAL,
    atr_at_entry    REAL,
    entry_time      TEXT,
    duration_min    REAL,
    mae_pts         REAL,
    regime_tag      TEXT,
    notes           TEXT,
    created_at      TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_autopsy_date   ON loss_autopsy(date);
CREATE INDEX IF NOT EXISTS idx_autopsy_cause  ON loss_autopsy(primary_cause);

CREATE TABLE IF NOT EXISTS whatif_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date        TEXT NOT NULL,
    filter_name     TEXT NOT NULL,
    filter_param    TEXT,                  -- JSON of parameter values used
    date_range_start TEXT,
    date_range_end  TEXT,
    actual_trades   INTEGER,
    filtered_trades INTEGER,
    removed_trades  INTEGER,
    actual_pnl      REAL,
    sim_pnl         REAL,
    pnl_delta       REAL,
    actual_wr       REAL,
    sim_wr          REAL,
    actual_pf       REAL,
    sim_pf          REAL,
    actual_expectancy REAL,
    sim_expectancy  REAL,
    verdict         TEXT,                  -- 'ADOPT' | 'TEST' | 'SKIP'
    created_at      TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_whatif_date ON whatif_results(run_date);
"""


def bootstrap_analytics_tables(db_path: Path = DB_PATH) -> None:
    """Create analytics tables if they don't exist. Safe to call repeatedly."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_ANALYTICS_DDL)
    conn.commit()
    conn.close()


# ── Trade loader & enrichment ─────────────────────────────────────────────────

def load_trades(
    date: str = None,
    date_from: str = None,
    date_to: str = None,
    strategy: str = None,
    instrument: str = None,
) -> list[dict]:
    """
    Load trades from DB with optional filters.
    Always returns enriched dicts (adds derived fields).
    """
    db = get_db()
    clauses = []
    params = []

    if date:
        clauses.append("date = ?"); params.append(date)
    if date_from:
        clauses.append("date >= ?"); params.append(date_from)
    if date_to:
        clauses.append("date <= ?"); params.append(date_to)
    if strategy:
        clauses.append("strategy = ?"); params.append(strategy)
    if instrument:
        clauses.append("instrument = ?"); params.append(instrument)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = db.query(
        f"SELECT * FROM trades {where} ORDER BY date, entry_time",
        tuple(params)
    )
    return [_enrich(r) for r in rows]


def _enrich(r: dict) -> dict:
    """Add derived fields to a raw trade row."""
    pnl = r.get("pnl_rs") or 0.0
    r["is_winner"]   = pnl > 0
    r["is_loser"]    = pnl <= 0
    r["duration_min"] = round((r.get("hold_seconds") or 0) / 60, 1)

    # Entry hour (integer) for time-bucket analysis
    et = r.get("entry_time") or ""
    try:
        r["entry_hour"] = int(et.split(":")[0]) if et else None
    except Exception:
        r["entry_hour"] = None

    # Capture pct — guard None
    r["capture_pct"] = r.get("capture_pct") or 0.0

    # ADX / ATR guard
    r["adx_entry"] = r.get("adx_entry") or 0.0
    r["atr_entry"] = r.get("atr_entry") or 0.0

    return r


# ── Statistical primitives ────────────────────────────────────────────────────

def safe_div(a, b, fallback=None):
    return a / b if b else fallback

def profit_factor(trades: list[dict]) -> Optional[float]:
    gp = sum(t["pnl_rs"] for t in trades if t["pnl_rs"] > 0)
    gl = abs(sum(t["pnl_rs"] for t in trades if t["pnl_rs"] <= 0))
    return safe_div(gp, gl)

def win_rate(trades: list[dict]) -> Optional[float]:
    if not trades: return None
    return sum(1 for t in trades if t["is_winner"]) / len(trades)

def expectancy(trades: list[dict]) -> Optional[float]:
    """Expectancy = (WR * AvgW) + ((1-WR) * AvgL)  — signed avg loser."""
    if not trades: return None
    winners = [t["pnl_rs"] for t in trades if t["is_winner"]]
    losers  = [t["pnl_rs"] for t in trades if t["is_loser"]]
    wr = len(winners) / len(trades)
    avg_w = statistics.mean(winners) if winners else 0
    avg_l = statistics.mean(losers)  if losers  else 0
    return wr * avg_w + (1 - wr) * avg_l

def max_drawdown(trades: list[dict]) -> float:
    """Max peak-to-trough drawdown on running cumulative P&L."""
    running = 0.0; peak = 0.0; max_dd = 0.0
    for t in trades:
        running += t["pnl_rs"]
        if running > peak: peak = running
        dd = peak - running
        if dd > max_dd: max_dd = dd
    return max_dd

def sharpe_proxy(trades: list[dict]) -> Optional[float]:
    """
    Simplified daily Sharpe proxy: expectancy / stdev(trade_pnl).
    Not annualised — used for relative decay comparison only.
    """
    if len(trades) < 4: return None
    exp = expectancy(trades)
    pnls = [t["pnl_rs"] for t in trades]
    std  = statistics.stdev(pnls)
    return safe_div(exp, std)

def rolling_windows(trades: list[dict], windows=(7, 14, 30)) -> dict:
    """
    Return sub-lists of trades for each rolling day window,
    counting backwards from the most recent trade date.
    """
    if not trades: return {w: [] for w in windows}
    dates = sorted(set(t["date"] for t in trades))
    last  = max(dates)
    last_dt = datetime.strptime(last, "%Y-%m-%d")
    result = {}
    for w in windows:
        cutoff = (last_dt - timedelta(days=w)).strftime("%Y-%m-%d")
        result[w] = [t for t in trades if t["date"] >= cutoff]
    result["all"] = trades
    return result

def consecutive_runs(trades: list[dict]):
    """Return (max_consec_wins, max_consec_losses, current_streak_str)."""
    if not trades: return 0, 0, "—"
    max_w = max_l = cur = 0
    prev_win = None
    for t in trades:
        w = t["is_winner"]
        if w == prev_win:
            cur += 1
        else:
            cur = 1
        if w:
            max_w = max(max_w, cur)
        else:
            max_l = max(max_l, cur)
        prev_win = w
    # Current streak
    streak_dir = "W" if prev_win else "L"
    return max_w, max_l, f"{cur}{streak_dir}"

def pnl_series(trades: list[dict]) -> list[float]:
    """Running cumulative P&L series."""
    running = 0.0; series = [0.0]
    for t in trades:
        running += t["pnl_rs"]
        series.append(running)
    return series

def grade(win_r, pf, exp, dd, n) -> str:
    """
    Simple A–F grade.
    Requires min 5 trades to grade anything above F.
    """
    if n < 5: return "N/A"
    score = 0
    if win_r is not None:
        if win_r >= 0.60: score += 3
        elif win_r >= 0.50: score += 2
        elif win_r >= 0.40: score += 1
    if pf is not None:
        if pf >= 2.0: score += 3
        elif pf >= 1.5: score += 2
        elif pf >= 1.0: score += 1
    if exp is not None:
        if exp > 500: score += 2
        elif exp > 0: score += 1
    if score >= 7: return "A"
    if score >= 5: return "B"
    if score >= 3: return "C"
    if score >= 1: return "D"
    return "F"
