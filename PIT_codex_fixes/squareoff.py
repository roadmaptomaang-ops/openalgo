"""
Shared square-off signal for all engines.
=========================================
A tiny flag-file protocol so you can tell every running engine to close its
open position(s) via its OWN exit logic (which records the trade card + DB
row) and stop — instead of flattening from outside the engine (which loses
the record, as happened 2026-07-13).

Usage:
  * Trigger from the shell:  touch a dated flag via ~/openalgo/squareoff.sh
  * Each engine calls squareoff_requested() in its monitor loop; when True it
    runs its normal hard-exit path (reason "Square-Off") and stops.

The flag carries today's date so a stale flag from a previous day is ignored,
and the run_*.sh scripts clear it on startup so a mid-day restart doesn't
immediately re-square-off.
"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

FLAG = Path.home() / "openalgo" / "control" / "squareoff.flag"
_IST = ZoneInfo("Asia/Kolkata")


def _today() -> str:
    return datetime.now(_IST).strftime("%Y-%m-%d")


def squareoff_requested() -> bool:
    """True only if a square-off was requested for TODAY (stale flags ignored)."""
    try:
        return FLAG.exists() and FLAG.read_text().strip() == _today()
    except Exception:
        return False


def request_squareoff() -> None:
    FLAG.parent.mkdir(parents=True, exist_ok=True)
    FLAG.write_text(_today())


def clear_squareoff() -> None:
    try:
        FLAG.unlink()
    except FileNotFoundError:
        pass
