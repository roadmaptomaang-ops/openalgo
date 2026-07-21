"""
sage_db.py — SAGE's own SQLite persistence layer
=================================================
Separate from trading.db (PIT). SAGE writes its own
intelligence artifacts here: enriched trade stories,
daily hypotheses, pattern registry, similarity log.

DB path: ~/openalgo/sage.db
"""

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

DB_PATH = Path.home() / "openalgo" / "sage.db"

_DDL = """
CREATE TABLE IF NOT EXISTS trade_stories (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id        INTEGER UNIQUE,         -- FK to trading.db trades.id
    date            TEXT NOT NULL,
    entry_time      TEXT,
    exit_time       TEXT,
    strategy        TEXT,
    instrument      TEXT,
    direction       TEXT,
    symbol          TEXT,
    -- outcome
    pnl_pts         REAL,
    pnl_rs          REAL,
    mfe_pts         REAL,
    mae_pts         REAL,
    capture_pct     REAL,
    exit_reason     TEXT,
    hold_seconds    INTEGER,
    outcome         TEXT,                   -- 'WIN' | 'LOSS' | 'BE'
    -- entry context (from existing columns)
    adx_entry       REAL,
    ema9_entry      REAL,
    ema21_entry     REAL,
    vwap_entry      REAL,
    orb_high        REAL,
    orb_low         REAL,
    oi_bias         TEXT,
    momentum        TEXT,
    trend_score     REAL,
    trade_num_today INTEGER,                -- nth trade of the day
    running_pnl_before REAL,               -- P&L before this trade
    -- enriched context (populated by enricher going forward)
    vix             REAL,
    nifty_change_pct REAL,                 -- NIFTY % from open at entry
    breadth_pct     REAL,                  -- % stocks above VWAP at entry
    session_age_min INTEGER,               -- minutes since market open
    -- feature vector (JSON-serialized list of floats)
    feature_vector  TEXT,
    feature_version TEXT DEFAULT 'v1',
    enriched        INTEGER DEFAULT 0,     -- 1 if full context captured
    created_at      TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_stories_date      ON trade_stories(date);
CREATE INDEX IF NOT EXISTS idx_stories_strategy  ON trade_stories(strategy);
CREATE INDEX IF NOT EXISTS idx_stories_outcome   ON trade_stories(outcome);
CREATE INDEX IF NOT EXISTS idx_stories_trade_id  ON trade_stories(trade_id);

CREATE TABLE IF NOT EXISTS daily_hypotheses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT NOT NULL UNIQUE,
    -- morning prediction
    regime          TEXT,                  -- TRENDING | RANGE | VOLATILE | THIN
    direction_bias  TEXT,                  -- CE | PE | NEUTRAL | EQUITY_LONG
    pnl_low         REAL,
    pnl_high        REAL,
    confidence      REAL,                  -- 0.0–1.0
    patterns_matched TEXT,                 -- JSON list of pattern ids
    similar_days    INTEGER,               -- how many past days matched
    reasoning       TEXT,                  -- JSON list of reason strings
    -- rules generated
    entry_window_start TEXT,
    entry_window_end   TEXT,
    adx_floor       REAL,
    max_trades      INTEGER,
    -- evening verdict (filled after close)
    actual_pnl      REAL,
    actual_trades   INTEGER,
    actual_regime   TEXT,
    hypothesis_correct INTEGER,            -- 1 | 0 | NULL (pending)
    direction_correct  INTEGER,
    pnl_in_range       INTEGER,
    verdict_notes   TEXT,
    -- pit comparison
    pit_recommendation TEXT,
    sage_recommendation TEXT,
    agreed          INTEGER,               -- 1 | 0 | NULL
    created_at      TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_hyp_date ON daily_hypotheses(date);

CREATE TABLE IF NOT EXISTS pattern_registry (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_id      TEXT UNIQUE NOT NULL,  -- e.g. 'P001'
    name            TEXT NOT NULL,
    description     TEXT,
    -- membership
    story_ids       TEXT,                  -- JSON list of trade_story ids
    trade_count     INTEGER DEFAULT 0,
    -- performance
    win_rate        REAL,
    avg_pnl_rs      REAL,
    avg_mfe         REAL,
    avg_hold_sec    INTEGER,
    profit_factor   REAL,
    -- defining features (JSON dict of feature: range)
    feature_profile TEXT,
    -- status
    status          TEXT DEFAULT 'ACTIVE', -- ACTIVE | DECAYING | RETIRED
    last_updated    TEXT,
    created_at      TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS similarity_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    query_date      TEXT NOT NULL,
    query_time      TEXT,
    strategy        TEXT,
    instrument      TEXT,
    query_features  TEXT,                  -- JSON
    matched_story_ids TEXT,               -- JSON list
    match_scores    TEXT,                  -- JSON list of cosine scores
    matched_win_rate REAL,
    matched_avg_pnl  REAL,
    recommendation  TEXT,                  -- ENTER | SKIP | CAUTION
    reason          TEXT,
    created_at      TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_simlog_date ON similarity_log(query_date);
"""


class SageDB:
    def __init__(self, db_path: Path = DB_PATH) -> None:
        self._path = db_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            conn.executescript(_DDL)
            conn.commit()
            conn.close()

    def query(self, sql: str, params: tuple = ()) -> list:
        with self._lock:
            try:
                conn = self._connect()
                rows = conn.execute(sql, params).fetchall()
                conn.close()
                return [dict(r) for r in rows]
            except Exception as e:
                print(f"[SAGE DB ERROR query] {e}")
                return []

    def execute(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            try:
                conn = self._connect()
                cur = conn.execute(sql, params)
                conn.commit()
                rowid = cur.lastrowid
                conn.close()
                return rowid
            except Exception as e:
                print(f"[SAGE DB ERROR execute] {e}")
                return -1

    def upsert_hypothesis(self, date: str, data: dict) -> None:
        existing = self.query("SELECT id FROM daily_hypotheses WHERE date=?", (date,))
        if existing:
            sets = ", ".join(f"{k}=?" for k in data.keys() if k != "date")
            vals = [v for k, v in data.items() if k != "date"] + [date]
            self.execute(
                f"UPDATE daily_hypotheses SET {sets}, updated_at=datetime('now','localtime') WHERE date=?",
                tuple(vals)
            )
        else:
            data["date"] = date
            cols = ", ".join(data.keys())
            placeholders = ", ".join(["?"] * len(data))
            self.execute(
                f"INSERT INTO daily_hypotheses ({cols}) VALUES ({placeholders})",
                tuple(data.values())
            )

    def upsert_pattern(self, pattern_id: str, data: dict) -> None:
        """Insert or update a mined pattern keyed on its stable pattern_id."""
        existing = self.query(
            "SELECT id FROM pattern_registry WHERE pattern_id=?", (pattern_id,)
        )
        data = {k: v for k, v in data.items() if k != "pattern_id"}
        if existing:
            sets = ", ".join(f"{k}=?" for k in data.keys())
            vals = list(data.values()) + [pattern_id]
            self.execute(
                f"UPDATE pattern_registry SET {sets}, "
                f"last_updated=datetime('now','localtime') WHERE pattern_id=?",
                tuple(vals),
            )
        else:
            data["pattern_id"] = pattern_id
            data["last_updated"] = None
            cols = ", ".join(data.keys())
            ph = ", ".join(["?"] * len(data))
            self.execute(
                f"INSERT INTO pattern_registry ({cols}) VALUES ({ph})",
                tuple(data.values()),
            )

    def log_similarity(self, data: dict) -> None:
        """Append one similarity-query record to similarity_log."""
        cols = ", ".join(data.keys())
        ph = ", ".join(["?"] * len(data))
        self.execute(
            f"INSERT INTO similarity_log ({cols}) VALUES ({ph})",
            tuple(data.values()),
        )

    def upsert_story(self, trade_id: int, data: dict) -> None:
        existing = self.query("SELECT id FROM trade_stories WHERE trade_id=?", (trade_id,))
        if existing:
            data.pop("trade_id", None)
            sets = ", ".join(f"{k}=?" for k in data.keys())
            vals = list(data.values()) + [trade_id]
            self.execute(
                f"UPDATE trade_stories SET {sets} WHERE trade_id=?",
                tuple(vals)
            )
        else:
            data["trade_id"] = trade_id
            cols = ", ".join(data.keys())
            placeholders = ", ".join(["?"] * len(data))
            self.execute(
                f"INSERT INTO trade_stories ({cols}) VALUES ({placeholders})",
                tuple(data.values())
            )


_db: Optional[SageDB] = None


def get_sage_db() -> SageDB:
    global _db
    if _db is None:
        _db = SageDB()
    return _db
