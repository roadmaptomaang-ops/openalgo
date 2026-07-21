"""
Shared Trade Logger — used by BOTH scalper and trend_rider
============================================================
Folder structure:
    ~/openalgo/logs/
    ├── <strategy>_master.csv          (every trade ever, for analysis)
    └── YYYY-MM-DD/
        └── <strategy>/
            ├── trades.log      (clean trade cards + summary)
            ├── activity.log    (everything, for debugging)
            └── trades.csv      (that day's trades, structured)

Usage in a strategy file:
    from trade_logger import TradeLogger
    tl = TradeLogger("scalper")        # or "trend_rider"
    tl.activity("NIFTY", "signal check ...")   # routine noise
    tl.trade("NIFTY", "ENTRY ...")             # important events
    tl.trade_card(card_text)                   # the detailed block
    tl.csv_row({...})                          # structured row → per-day + master
"""

import os
import csv
from datetime import datetime
from pathlib import Path

import pytz
from db_logger import get_db

IST = pytz.timezone("Asia/Kolkata")

# Canonical CSV columns — same for both strategies so the master files
# can be compared side by side in Excel.
CSV_COLUMNS = [
    "date", "entry_time", "exit_time", "strategy", "instrument",
    "direction", "symbol", "strike_type",
    "entry_price", "exit_price", "quantity",
    "pnl_pts", "pnl_rs", "mfe_pts", "mae_pts", "capture_pct",
    "exit_reason", "hold_seconds",
    "adx_entry", "atr_entry", "ema9_entry", "ema21_entry", "vwap_entry",
    "orb_high", "orb_low",
    "max_pain", "max_pain_bias", "oi_bias", "momentum",
    "be_triggered", "trail_count", "high_water_pts",
    "trend_score", "running_total",
]

MONTHS = ["01-January", "02-February", "03-March", "04-April",
          "05-May", "06-June", "07-July", "08-August",
          "09-September", "10-October", "11-November", "12-December"]


class TradeLogger:
    def __init__(self, strategy: str, root: str = "~/openalgo/logs"):
        self.strategy = strategy
        self.root = Path(os.path.expanduser(root))
        self.root.mkdir(parents=True, exist_ok=True)

    # ── folder + file path helpers ───────────────────────────
    def _now(self) -> datetime:
        return datetime.now(IST)

    def _date_folder(self) -> Path:
        # logs/YYYY-MM-DD/<strategy>/
        now = self._now()
        folder = self.root / now.strftime("%Y-%m-%d") / self.strategy
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _trade_log_path(self) -> Path:
        return self._date_folder() / "trades.log"

    def _activity_log_path(self) -> Path:
        return self._date_folder() / "activity.log"

    def _day_csv_path(self) -> Path:
        return self._date_folder() / "trades.csv"

    def _master_csv_path(self) -> Path:
        return self.root / f"{self.strategy}_master.csv"

    # ── log writers ──────────────────────────────────────────
    def _stamp(self, name: str, msg: str) -> str:
        return f"[{self._now().strftime('%H:%M:%S')}] [{name}] {msg}"

    def trade(self, name: str, msg: str) -> None:
        """Important events → trade log + terminal."""
        line = self._stamp(name, msg)
        print(line)
        self._append(self._trade_log_path(), line)

    def activity(self, name: str, msg: str) -> None:
        """Routine noise → activity log + terminal."""
        line = self._stamp(name, msg)
        print(line)
        self._append(self._activity_log_path(), line)

    def trade_card(self, text: str) -> None:
        """Multi-line detailed trade block → trade log + terminal."""
        print(text)
        self._append(self._trade_log_path(), text)

    def raw_trade(self, text: str) -> None:
        """Write to trade log without timestamp prefix (headers etc.)."""
        print(text)
        self._append(self._trade_log_path(), text)

    def log_breadth(
        self,
        pct_above_vwap: float,
        pct_above_ema21: float,
        advancing: int,
        declining: int,
        bias: str,
    ) -> None:
        """Write breadth snapshot to SQLite DB."""
        now = self._now()
        try:
            get_db().insert_breadth(
                date=now.strftime("%Y-%m-%d"),
                time=now.strftime("%H:%M"),
                pct_above_vwap=pct_above_vwap,
                pct_above_ema21=pct_above_ema21,
                advancing=advancing,
                declining=declining,
                bias=bias,
            )
        except Exception as e:
            print(f"[DB ERROR breadth] {e}")

    def log_summary(self, date: str, strategy: str, stats: dict) -> None:
        """Write daily summary to SQLite DB."""
        try:
            get_db().upsert_summary(date, strategy, stats)
        except Exception as e:
            print(f"[DB ERROR summary] {e}")

    @staticmethod
    def diagnose(
        exit_reason: str,
        pnl_pts: float,
        mfe_pts: float,
        mae_pts: float,
        be_triggered: bool,
        trail_count: int,
        adx_entry: float,
        adx_exit: float,
        high_water: float,
    ) -> tuple:
        """
        Return (tag, explanation) summarising WHY the trade played out
        the way it did. Used in both scalper and trend rider trade cards.
        """
        # ── Win diagnoses ────────────────────────────────────
        if pnl_pts > 0:
            if exit_reason == "Target Hit":
                return ("CLEAN WIN", "Target hit with full momentum. Strategy worked as intended.")
            if exit_reason == "High Water Exit" and mfe_pts > 0:
                cap = pnl_pts / mfe_pts * 100
                if cap >= 70:
                    return ("GOOD EXIT", "High water exit captured most of the move. Trailing working well.")
                return ("EARLY EXIT", f"High water exit left {100-cap:.0f}% of the move on the table. Consider widening drop threshold.")
            if exit_reason == "Trailing SL":
                return ("TRAIL WIN", "Trailing SL protected profits and exited at a good level.")
            return ("WIN", "Trade closed profitably.")

        # ── Loss/scratch diagnoses ───────────────────────────
        if exit_reason in ("SL Hit", "Initial SL"):
            if mfe_pts <= 1.0:
                return ("BAD ENTRY", "Price moved against immediately. Entry timing was off or signal was weak.")
            if mfe_pts > 0 and be_triggered:
                return ("REVERSAL AFTER BE", "Break-even triggered but market reversed sharply. Strong counter-move, not a strategy issue.")
            if adx_entry < 22:
                return ("LOW ADX ENTRY", f"ADX was only {adx_entry:.1f} at entry — market was borderline choppy. Avoid entries below ADX 22.")
            if mae_pts < -1.5 * abs(pnl_pts):
                return ("HELD TOO LONG", "MAE shows trade went deep before SL. Consider tighter SL or earlier exit on reversal signals.")
            return ("SL HIT", "Market moved against position and hit stop. Normal loss — review if setup was valid.")

        if exit_reason == "High Water Exit":
            if mfe_pts > 0:
                cap = pnl_pts / mfe_pts * 100
                return ("GAVE BACK PROFIT", f"Reached MFE of {mfe_pts:.1f}pts but exit at {pnl_pts:.1f}pts ({cap:.0f}% capture). High water drop triggered on reversal.")
            return ("NO MOMENTUM", "Trade barely moved in our favour before reversing. Weak signal.")

        if exit_reason == "Trailing SL":
            if be_triggered:
                return ("TRAIL SCRATCH", "Break-even reached but trailing SL exit at loss — sharp reversal after peak. Normal on choppy days.")
            return ("TRAIL LOSS", "Trailing SL hit before break-even. Trade never gained meaningful traction.")

        if exit_reason == "Hard Exit":
            if pnl_pts < 0:
                return ("TIME EXIT LOSS", "Position closed at 3:30 PM at a loss. Trade never recovered — review entry timing.")
            return ("TIME EXIT", "Position closed at hard exit time.")

        return ("UNKNOWN", "Review logs manually.")

    @staticmethod
    def _append(path: Path, text: str) -> None:
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(text + "\n")
        except Exception:
            pass

    # ── CSV writer (per-day + master) ────────────────────────
    def csv_row(self, row: dict) -> None:
        """Write one completed-trade row to BOTH per-day and master CSV."""
        clean = {col: row.get(col, "") for col in CSV_COLUMNS}
        for path in (self._day_csv_path(), self._master_csv_path()):
            new_file = not path.exists()
            try:
                with open(path, "a", newline="", encoding="utf-8") as fh:
                    writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
                    if new_file:
                        writer.writeheader()
                    writer.writerow(clean)
            except Exception as e:
                print(f"[CSV ERROR] {e}")

        # ── Also write to SQLite DB ───────────────────────────
        try:
            get_db().insert_trade(clean)
        except Exception as e:
            print(f"[DB ERROR trade_logger] {e}")
