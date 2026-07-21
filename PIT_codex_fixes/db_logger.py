"""
db_logger.py — SQLite database layer for OpenAlgo trading system
================================================================
Used by trade_logger.py to persist every trade, breadth snapshot
and daily summary into ~/openalgo/trading.db

Tables:
    trades          — one row per completed trade
    breadth_log     — market breadth snapshot every 60s
    daily_summary   — end of day summary per strategy

No external dependencies — uses Python built-in sqlite3.
"""

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

import pytz

IST = pytz.timezone("Asia/Kolkata")

DB_PATH = Path.home() / "openalgo" / "trading.db"


# ── Schema ────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    -- identity
    date            TEXT NOT NULL,
    entry_time      TEXT,
    exit_time       TEXT,
    strategy        TEXT,          -- 'scalper' | 'trend_rider'
    instrument      TEXT,          -- 'NIFTY' | 'SENSEX' | 'EQUITY'
    direction       TEXT,          -- 'CE' | 'PE' | 'BUY'
    symbol          TEXT,
    strike_type     TEXT,          -- 'ITM' | 'OTM' | 'EQUITY'
    -- prices
    entry_price     REAL,
    exit_price      REAL,
    quantity        INTEGER,
    -- performance
    pnl_pts         REAL,
    pnl_rs          REAL,
    mfe_pts         REAL,
    mae_pts         REAL,
    capture_pct     REAL,
    exit_reason     TEXT,
    hold_seconds    INTEGER,
    -- entry indicators
    adx_entry       REAL,
    atr_entry       REAL,
    ema9_entry      REAL,
    ema21_entry     REAL,
    vwap_entry      REAL,
    orb_high        REAL,
    orb_low         REAL,
    max_pain        REAL,
    max_pain_bias   TEXT,
    oi_bias         TEXT,
    momentum        TEXT,
    -- trade management
    be_triggered    TEXT,          -- 'YES' | 'NO'
    trail_count     INTEGER,
    high_water_pts  REAL,
    trend_score     REAL,
    running_total   REAL,
    -- diagnosis
    diag_tag        TEXT,
    diag_msg        TEXT,
    -- metadata
    created_at      TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_trades_date     ON trades(date);
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);
CREATE INDEX IF NOT EXISTS idx_trades_instrument ON trades(instrument);
CREATE INDEX IF NOT EXISTS idx_trades_exit_reason ON trades(exit_reason);

CREATE TABLE IF NOT EXISTS breadth_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT NOT NULL,
    time            TEXT NOT NULL,
    pct_above_vwap  REAL,
    pct_above_ema21 REAL,
    advancing       INTEGER,
    declining       INTEGER,
    bias            TEXT,          -- 'BULLISH' | 'BEARISH' | 'MIXED'
    created_at      TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_breadth_date ON breadth_log(date);

CREATE TABLE IF NOT EXISTS daily_summary (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT NOT NULL,
    strategy        TEXT NOT NULL,
    total_trades    INTEGER,
    wins            INTEGER,
    losses          INTEGER,
    win_rate        REAL,
    gross_profit    REAL,
    gross_loss      REAL,
    profit_factor   REAL,
    avg_winner      REAL,
    avg_loser       REAL,
    best_trade      REAL,
    worst_trade     REAL,
    avg_mfe         REAL,
    avg_mae         REAL,
    avg_capture     REAL,
    final_pnl       REAL,
    created_at      TEXT DEFAULT (datetime('now', 'localtime')),
    UNIQUE(date, strategy)
);

CREATE INDEX IF NOT EXISTS idx_summary_date ON daily_summary(date);
"""


class DBLogger:
    """
    Thread-safe SQLite writer.
    One instance shared across all strategy threads.
    """

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self._path = db_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")   # safe concurrent writes
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            conn.executescript(_DDL)
            conn.commit()
            conn.close()

    # ── Writers ──────────────────────────────────────────────

    def insert_trade(self, row: dict) -> None:
        """Insert one completed trade row."""
        cols = [
            "date", "entry_time", "exit_time", "strategy", "instrument",
            "direction", "symbol", "strike_type",
            "entry_price", "exit_price", "quantity",
            "pnl_pts", "pnl_rs", "mfe_pts", "mae_pts", "capture_pct",
            "exit_reason", "hold_seconds",
            "adx_entry", "atr_entry", "ema9_entry", "ema21_entry", "vwap_entry",
            "orb_high", "orb_low", "max_pain", "max_pain_bias", "oi_bias",
            "momentum", "be_triggered", "trail_count", "high_water_pts",
            "trend_score", "running_total", "diag_tag", "diag_msg",
        ]
        values = [row.get(c) for c in cols]
        sql = f"INSERT INTO trades ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))})"
        with self._lock:
            try:
                conn = self._connect()
                conn.execute(sql, values)
                conn.commit()
                conn.close()
            except Exception as e:
                print(f"[DB ERROR insert_trade] {e}")

    def insert_breadth(
        self, date: str, time: str,
        pct_above_vwap: float, pct_above_ema21: float,
        advancing: int, declining: int, bias: str
    ) -> None:
        sql = """INSERT INTO breadth_log
                 (date,time,pct_above_vwap,pct_above_ema21,advancing,declining,bias)
                 VALUES (?,?,?,?,?,?,?)"""
        with self._lock:
            try:
                conn = self._connect()
                conn.execute(sql, (date, time, pct_above_vwap,
                                   pct_above_ema21, advancing, declining, bias))
                conn.commit()
                conn.close()
            except Exception as e:
                print(f"[DB ERROR insert_breadth] {e}")

    def upsert_summary(self, date: str, strategy: str, stats: dict) -> None:
        """Insert or replace daily summary."""
        cols = [
            "date", "strategy", "total_trades", "wins", "losses",
            "win_rate", "gross_profit", "gross_loss", "profit_factor",
            "avg_winner", "avg_loser", "best_trade", "worst_trade",
            "avg_mfe", "avg_mae", "avg_capture", "final_pnl",
        ]
        values = [date, strategy] + [stats.get(c) for c in cols[2:]]
        sql = f"""INSERT OR REPLACE INTO daily_summary
                  ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))})"""
        with self._lock:
            try:
                conn = self._connect()
                conn.execute(sql, values)
                conn.commit()
                conn.close()
            except Exception as e:
                print(f"[DB ERROR upsert_summary] {e}")

    # ── Query helpers (used by report.py) ────────────────────

    def query(self, sql: str, params: tuple = ()) -> list:
        with self._lock:
            try:
                conn = self._connect()
                rows = conn.execute(sql, params).fetchall()
                conn.close()
                return [dict(r) for r in rows]
            except Exception as e:
                print(f"[DB ERROR query] {e}")
                return []


# Singleton — import and use anywhere
_db: Optional[DBLogger] = None

def get_db() -> DBLogger:
    global _db
    if _db is None:
        _db = DBLogger()
    return _db
