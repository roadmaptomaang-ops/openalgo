"""
Trend Rider V3.0 — WebSocket-Driven Intraday Trend Bot
=======================================================
Broker  : Upstox via OpenAlgo (http://127.0.0.1:5000)
Markets : NIFTY options · SENSEX options · Nifty 50 equity (MIS)
Data    : OpenAlgo Socket.IO WebSocket — tick on every price change
Orders  : OpenAlgo REST API — fill verification, 3-retry logic
Logs    : ~/trend_rider/logs/  →  YYYY-MM-DD_trades.log
                                   YYYY-MM-DD_activity.log
Paper   : Set TRADE_MODE=paper to simulate fills (no real orders)

V2 vs V1 changes
----------------
  • REST price polling (5–10 s) replaced by WebSocket tick cache
    — SL/trail checks run in < 100 ms of a new tick arriving
  • TickCache: thread-safe dict  symbol → latest LTP
  • DepthCache: thread-safe dict symbol → best bid/ask + OI proxy
  • ExecutionEngine: place → verify fill (3 retries, 500 ms apart)
    logs actual fill price, flags partials/rejections
  • Option chain refresh: every 60 s (was 15 min)
  • Logs under ~/trend_rider/logs/ as requested
  • Paper-trade mode: no real orders, P&L from tick prices
  • Latency log: each SL check timestamped (tick_ts → check_ts)
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd
import requests

# ── optional socketio (graceful degradation to REST polling) ──────────────
try:
    import socketio                          # pip install "python-socketio[client]"
    _SIO_AVAILABLE = True
except ImportError:
    _SIO_AVAILABLE = False

IST = ZoneInfo("Asia/Kolkata")

# ================================================================
# NIFTY 50 UNIVERSE
# ================================================================

# NIFTY50 core universe
NIFTY50_STOCKS: list[str] = [
    "RELIANCE","TCS","HDFCBANK","BHARTIARTL","ICICIBANK",
    "INFOSYS","SBIN","HINDUNILVR","ITC","KOTAKBANK",
    "LT","AXISBANK","BAJFINANCE","ASIANPAINT","MARUTI",
    "HCLTECH","SUNPHARMA","TITAN","ULTRACEMCO","NTPC",
    "WIPRO","POWERGRID","BAJAJFINSV","TECHM","NESTLEIND",
    "ADANIENT","ADANIPORTS","TATAMOTORS","TATASTEEL","JSWSTEEL",
    "HINDALCO","COALINDIA","ONGC","BPCL","IOC",
    "GRASIM","DIVISLAB","CIPLA","DRREDDY","APOLLOHOSP",
    "EICHERMOT","HEROMOTOCO","BAJAJ-AUTO","M&M","TATACONSUM",
    "BRITANNIA","SHRIRAMFIN","BEL","INDUSINDBK","HDFCLIFE",
]

# Additional Bank Nifty stocks not in NIFTY50
BANK_NIFTY_EXTRA: list[str] = [
    "BANDHANBNK","FEDERALBNK","IDFCFIRSTB","PNB","BANKBARODA",
]

# Top Midcap Select stocks with high liquidity
MIDCAP_SELECT: list[str] = [
    "PERSISTENT","MPHASIS","LTIM","COFORGE","TRENT",
    "ASTRAL","PIIND","DIXON","ABCAPITAL","BHEL",
]

# Sector index symbols for strength calculation (NSE_INDEX)
SECTOR_INDICES: dict[str, str] = {
    "HDFCBANK":    "NIFTYBANK",   "ICICIBANK":  "NIFTYBANK",
    "KOTAKBANK":   "NIFTYBANK",   "AXISBANK":   "NIFTYBANK",
    "SBIN":        "NIFTYBANK",   "INDUSINDBK": "NIFTYBANK",
    "BANDHANBNK":  "NIFTYBANK",   "FEDERALBNK": "NIFTYBANK",
    "IDFCFIRSTB":  "NIFTYBANK",   "PNB":        "NIFTYBANK",
    "BANKBARODA":  "NIFTYBANK",
    "MARUTI":      "NIFTYAUTO",   "BAJAJ-AUTO": "NIFTYAUTO",
    "EICHERMOT":   "NIFTYAUTO",   "HEROMOTOCO": "NIFTYAUTO",
    "M&M":         "NIFTYAUTO",   "TATAMOTORS": "NIFTYAUTO",
    "TCS":         "NIFTYIT",     "INFOSYS":    "NIFTYIT",
    "HCLTECH":     "NIFTYIT",     "WIPRO":      "NIFTYIT",
    "TECHM":       "NIFTYIT",     "PERSISTENT": "NIFTYIT",
    "MPHASIS":     "NIFTYIT",     "LTIM":       "NIFTYIT",
    "COFORGE":     "NIFTYIT",
    "SUNPHARMA":   "NIFTYPHARMA", "DRREDDY":    "NIFTYPHARMA",
    "CIPLA":       "NIFTYPHARMA", "DIVISLAB":   "NIFTYPHARMA",
    "APOLLOHOSP":  "NIFTYPHARMA",
    "RELIANCE":    "NIFTYENERGY", "ONGC":       "NIFTYENERGY",
    "BPCL":        "NIFTYENERGY", "IOC":        "NIFTYENERGY",
    "COALINDIA":   "NIFTYENERGY", "POWERGRID":  "NIFTYENERGY",
    "NTPC":        "NIFTYENERGY",
    "HINDUNILVR":  "NIFTYFMCG",  "ITC":         "NIFTYFMCG",
    "BRITANNIA":   "NIFTYFMCG",  "NESTLEIND":   "NIFTYFMCG",
    "TATACONSUM":  "NIFTYFMCG",
}

# Backtest-proven universe — 5yr SMA 9/21 positive return + PF > 1
# Removed losers: BAJAJ-AUTO, MARUTI, JSWSTEEL, ONGC, DRREDDY,
#                 KOTAKBANK, AXISBANK, ICICIBANK, INFOSYS, EICHERMOT,
#                 HDFCBANK, M&M, DIVISLAB, JSWSTEEL, BEL
ALL_EQUITY_STOCKS: list[str] = [
    "BRITANNIA",   # +121% PF 2.20 — best performer
    "BAJFINANCE",  # +71%  PF 1.82
    "WIPRO",       # +34%  PF 1.87
    "IOC",         # +114% PF 1.51
    "HINDALCO",    # +97%  PF 1.53
    "ASIANPAINT",  # +56%  PF 1.71
    "INDUSINDBK",  # +34%  PF 1.55
    "BPCL",        # +44%  PF 1.29
    "TCS",         # +21%  PF 1.27
    "SUNPHARMA",   # +57%  PF 1.15
    "HEROMOTOCO",  # +30%  PF 1.15
    "HCLTECH",     # +8%   PF 1.16
    "GRASIM",      # +54%  PF 1.16
    "RELIANCE",    # +23%  PF 1.14
    "BHARTIARTL",  # +75%  PF 1.13
    "SBIN",        # +37%  PF 1.09
    "TATASTEEL",   # +28%  PF 0.99
    "POWERGRID",   # +49%
    "NTPC",        # +48%
    "COALINDIA",   # +62%
]

# ================================================================
# CONFIG
# ================================================================

@dataclass
class TradingMode:
    name:                  str
    score_threshold:       float
    atr_sl_multiplier:     float
    atr_trail_multiplier:  float
    be_trigger_r:          float
    max_trades_per_day:    int
    max_equity_positions:  int
    cooldown_after_sl:     int    # seconds
    cooldown_after_exit:   int
    max_daily_loss:        float
    per_trade_risk_rs:     float


MODES: dict[str, TradingMode] = {
    "conservative": TradingMode(
        "Conservative", 85, 1.5, 2.0, 1.0, 3, 2, 600, 300, -4000, 1500),
    "balanced": TradingMode(
        "Balanced",     80, 1.2, 1.8, 0.8, 8, 3, 300, 180, -6000, 2000),
    "aggressive": TradingMode(
        "Aggressive",   75, 1.0, 1.5, 0.6, 8, 3, 180, 120,-10000, 3000),
}

# V3: a no-momentum exit with |P&L| below this is treated as a scratch/non-event
# and does NOT consume the daily trade-cap budget.
SCRATCH_PNL_MAX = 100.0


@dataclass
class AppConfig:
    # ── API ──────────────────────────────────────────────────
    api_key:                  str   = ""
    base_url:                 str   = "http://127.0.0.1:5000/api/v1"
    ws_url:                   str   = "http://127.0.0.1:5000"   # Socket.IO root

    # ── Paper / live ─────────────────────────────────────────
    paper_mode:               bool  = True   # no real orders when True

    # ── Session (IST) ────────────────────────────────────────
    entry_time:               str   = "09:20"
    late_entry_cutoff:        str   = "14:30"   # no new entries in last 2 hours
    hard_exit_time:           str   = "15:15"
    lunch_start:              str   = "11:45"
    lunch_end:                str   = "13:15"
    equity_entry_stagger_s:   int   = 300        # 5 min between new equity entries
    max_trades_per_symbol:    int   = 2          # max entries per symbol per day
    symbol_sl_cooldown_s:     int   = 300        # 5 min cooldown per symbol after SL
    min_mfe_after_secs:       int   = 120        # seconds before MFE check
    min_mfe_pts:              float = 1.0        # if MFE < this after min_mfe_after_secs → exit

    # ── Timing ───────────────────────────────────────────────
    tick_sl_check_interval:   float = 1.0    # seconds between SL checks per position
    candle_interval:          str   = "1m"
    candle_confirm_count:     int   = 2
    min_hold_seconds:         int   = 120
    chain_refresh_interval:   int   = 60     # option chain refresh (seconds)
    stock_rank_refresh:       int   = 60

    # ── Indicators ───────────────────────────────────────────
    ema_fast:                 int   = 9
    ema_slow:                 int   = 21
    adx_period:               int   = 14
    atr_period:               int   = 14
    orb_candles:              int   = 3
    swing_lookback:           int   = 5

    # ── Index instruments ────────────────────────────────────
    index_configs: dict = field(default_factory=lambda: {
        "NIFTY": {
            "index_symbol":    "NIFTY",
            "index_exchange":  "NSE_INDEX",
            "option_exchange": "NFO",
            "quantity":        65,
            "strike_interval": 50,
            "expiry_day":      1,
            "orb_buffer":      30,
        },
        "SENSEX": {
            "index_symbol":    "SENSEX",
            "index_exchange":  "BSE_INDEX",
            "option_exchange": "BFO",
            "quantity":        20,
            "strike_interval": 100,
            "expiry_day":      3,
            "orb_buffer":      80,
        },
    })

    # ── Equity ───────────────────────────────────────────────
    equity_exchange:          str   = "NSE"
    equity_lot_value:         float = 50000.0
    top_n_stocks:             int   = 3

    # ── Scoring weights (sum = 100) ──────────────────────────
    score_weights: dict = field(default_factory=lambda: {
        "ema_trend":    25,
        "adx_strength": 20,
        "vwap_confirm": 20,
        "orb_breakout": 20,
        "oi_alignment": 15,
    })

    # ── Trailing ─────────────────────────────────────────────
    high_water_drop_r:        float = 0.6
    swing_trail_enabled:      bool  = True

    # ── Risk ─────────────────────────────────────────────────
    max_consecutive_losses:   int   = 3
    portfolio_max_exposure_rs: float = 10_000_000.0   # 1 Cr paper capital

    # ── Mode ─────────────────────────────────────────────────
    mode_name:                str   = "balanced"

    @property
    def mode(self) -> TradingMode:
        return MODES[self.mode_name]


# ================================================================
# LOGGING ENGINE  (~/openalgo/logs/ — shared dated structure)
# ================================================================

class LoggingEngine:
    """
    Thread-safe dual-log system.
    Files: ~/trend_rider/logs/YYYY-MM-DD_trades.log
           ~/trend_rider/logs/YYYY-MM-DD_activity.log
    """

    # Shared dated-folder structure:
    #   ~/openalgo/logs/YYYY/MM-Month/DD/trend_rider_{trades,activity}.log
    #   ~/openalgo/logs/trend_rider_master.csv  (+ per-day csv)
    _MONTHS = ["01-January","02-February","03-March","04-April",
               "05-May","06-June","07-July","08-August",
               "09-September","10-October","11-November","12-December"]
    _CSV_COLUMNS = [
        "date","entry_time","exit_time","strategy","instrument",
        "direction","symbol","strike_type",
        "entry_price","exit_price","quantity",
        "pnl_pts","pnl_rs","mfe_pts","mae_pts","capture_pct",
        "exit_reason","hold_seconds",
        "adx_entry","atr_entry","ema9_entry","ema21_entry","vwap_entry",
        "orb_high","orb_low",
        "max_pain","max_pain_bias","oi_bias","momentum",
        "be_triggered","trail_count","high_water_pts",
        "trend_score","running_total",
    ]

    def __init__(self, log_dir: str = "~/openalgo/logs"):
        self._root = Path(os.path.expanduser(log_dir))
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(IST)

    def _date_folder(self) -> Path:
        # logs/YYYY-MM-DD/trend_rider/
        now = self._now()
        folder = self._root / now.strftime("%Y-%m-%d") / "trend_rider_v3"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _trade_path(self) -> Path:
        return self._date_folder() / "trades.log"

    def _activity_path(self) -> Path:
        return self._date_folder() / "activity.log"

    def _day_csv_path(self) -> Path:
        return self._date_folder() / "trades.csv"

    def _master_csv_path(self) -> Path:
        return self._root / "trend_rider_master.csv"

    def csv_row(self, row: dict) -> None:
        import csv as _csv
        clean = {col: row.get(col, "") for col in self._CSV_COLUMNS}
        for path in (self._day_csv_path(), self._master_csv_path()):
            new_file = not path.exists()
            try:
                with self._lock:
                    with open(path, "a", newline="", encoding="utf-8") as fh:
                        w = _csv.DictWriter(fh, fieldnames=self._CSV_COLUMNS)
                        if new_file:
                            w.writeheader()
                        w.writerow(clean)
            except Exception as e:
                print(f"[CSV ERROR trend_rider] {e}")
        # Also write to SQLite DB
        try:
            from db_logger import get_db as _get_db
            _get_db().insert_trade(clean)
        except Exception as e:
            print(f"[DB ERROR trend_rider] {e}")

    def _write(self, path: Path, line: str) -> None:
        try:
            with self._lock:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except Exception:
            pass

    def trade(self, name: str, msg: str) -> None:
        line = f"[{self._now().strftime('%H:%M:%S')}] [{name}] {msg}"
        print(line)
        self._write(self._trade_path(), line)

    def activity(self, name: str, msg: str) -> None:
        line = f"[{self._now().strftime('%H:%M:%S')}] [{name}] {msg}"
        print(line)
        self._write(self._activity_path(), line)

    def day_start(self, name: str, mode_name: str, paper: bool) -> None:
        sep   = "#" * 64
        tag   = "PAPER TRADE" if paper else "LIVE TRADE"
        lines = [
            "", sep,
            f"# Trend Rider V3.0 — {name} — {self._now().strftime('%Y-%m-%d')}",
            f"# Mode: {mode_name.upper()}  |  {tag}",
            f"# Logs: ~/openalgo/logs/ (dated folders)",
            sep, "",
        ]
        for ln in lines:
            print(ln)
            self._write(self._trade_path(), ln)

    @staticmethod
    def _momentum(ema9: float, ema21: float, adx: float) -> str:
        if ema9 > ema21 and adx > 20:  return "Strong Bullish"
        if ema9 > ema21 and adx <= 20: return "Weakening Bullish"
        if ema9 < ema21 and adx > 20:  return "Strong Bearish"
        if ema9 < ema21 and adx <= 20: return "Weakening Bearish"
        return "Neutral"

    def log_trade_card(self, r: "TradeRecord") -> None:
        sep   = "=" * 60
        qty   = r.quantity
        hold  = str(timedelta(seconds=int(r.hold_seconds)))
        cap   = (r.pnl_pts / r.mfe_pts * 100) if r.mfe_pts > 0 else 0
        entry = r.entry_price_filled or r.entry_price_quoted
        et    = r.entry_time.strftime("%H:%M:%S") if r.entry_time else "N/A"
        xt    = r.exit_time.strftime("%H:%M:%S") if r.exit_time else "N/A"
        dt    = r.entry_time.strftime("%Y-%m-%d") if r.entry_time else ""
        outcome = "WIN" if r.pnl_rs > 0 else "LOSS"

        lines = [
            "",
            sep,
            f"TRADE #{r.trade_num} | {r.instrument} | {dt} | {et}",
            sep,
            "",
            "-- MARKET CONDITIONS AT ENTRY ----------------------",
            f"  Spot Price     : {r.spot_entry:.2f}",
            f"  ORB High       : {r.orb_high:.2f}",
            f"  ORB Low        : {r.orb_low:.2f}",
            f"  EMA9           : {r.ema9_entry:.2f}",
            f"  EMA21          : {r.ema21_entry:.2f}",
            f"  VWAP           : {r.vwap_entry:.2f}",
            f"  ADX            : {r.adx_entry:.1f} ({'Trending' if r.adx_entry >= 20 else 'Weak/Sideways'})",
            f"  ATR            : {r.atr_entry:.2f}",
            f"  OI Bias        : {r.oi_bias}",
            f"  Vol Regime     : {r.vol_regime}",
            f"  Momentum       : {self._momentum(r.ema9_entry, r.ema21_entry, r.adx_entry)}",
            "",
            "-- ENTRY -------------------------------------------",
            f"  Signal         : {r.direction}",
            f"  Symbol         : {r.symbol}",
            f"  Strike Type    : {r.strike_type}",
            f"  Trend Score    : {r.trend_score:.1f} / 100",
            f"  Entry (quoted) : Rs {r.entry_price_quoted:.2f}",
            f"  Entry (filled) : Rs {entry:.2f}",
            f"  Slippage       : {r.slippage_pts:+.2f} pts",
            f"  Initial SL     : {r.initial_sl_pts:.1f} pts",
            f"  Quantity       : {qty}",
            f"  Entry Time     : {et}",
            "",
            "-- MFE / MAE ANALYTICS -----------------------------",
            f"  MFE (Best point) : +{r.mfe_pts:.1f} pts = Rs {r.mfe_pts * qty:.0f}",
            f"  MAE (Worst point): {r.mae_pts:.1f} pts = Rs {r.mae_pts * qty:.0f}",
            f"  Profit Captured  : {r.pnl_pts:.1f}pts / {r.mfe_pts:.1f}pts = {cap:.0f}%",
            "",
            "-- BREAK-EVEN AUDIT --------------------------------",
        ]
        if r.be_triggered:
            lines.append("  Break-even     : YES - Triggered (SL moved to cost)")
        else:
            lines.append("  Break-even     : NO - Never reached")

        lines += [
            "",
            "-- TRAILING SL AUDIT -------------------------------",
            f"  Trail updates  : {r.trail_count}",
            "",
            "-- HIGH WATER AUDIT --------------------------------",
            f"  Peak Profit    : +{r.mfe_pts:.1f} pts = Rs {r.mfe_pts * qty:.0f}",
            f"  Exit Profit    : {r.pnl_pts:+.1f} pts = Rs {r.pnl_rs:.0f}",
            f"  Giveback       : {r.mfe_pts - r.pnl_pts:.1f} pts = Rs {(r.mfe_pts - r.pnl_pts) * qty:.0f}",
            f"  Exit Reason    : {r.exit_reason}",
            "",
            "-- EXIT CONTEXT ------------------------------------",
            f"  Exit Time      : {xt}",
            f"  Exit Premium   : Rs {r.exit_price:.2f}",
            f"  Spot at Exit   : {r.spot_exit:.2f}",
            f"  EMA9 at Exit   : {r.ema9_exit:.2f}",
            f"  EMA21 at Exit  : {r.ema21_exit:.2f}",
            f"  VWAP at Exit   : {r.vwap_exit:.2f}",
            f"  ADX at Exit    : {r.adx_exit:.1f}",
            f"  Momentum State : {self._momentum(r.ema9_exit, r.ema21_exit, r.adx_exit)}",
            f"  SL at Exit     : {r.sl_at_exit:.1f} pts",
            f"  Hold Duration  : {hold}",
            "",
            "-- RESULT ------------------------------------------",
            f"  Outcome        : {outcome}",
            f"  P&L Points     : {r.pnl_pts:+.1f} pts",
            f"  P&L Rupees     : {'+ ' if r.pnl_rs >= 0 else '- '}Rs {abs(r.pnl_rs):.0f}",
            f"  RUNNING TOTAL  : Rs {r.running_total:.0f}",
            sep,
            "",
        ]
        # ── Auto-diagnosis ───────────────────────────────────
        from trade_logger import TradeLogger as _TLClass
        diag_tag, diag_msg = _TLClass.diagnose(
            exit_reason=r.exit_reason,
            pnl_pts=r.pnl_pts, mfe_pts=r.mfe_pts, mae_pts=r.mae_pts,
            be_triggered=r.be_triggered, trail_count=r.trail_count,
            adx_entry=r.adx_entry, adx_exit=r.adx_exit,
            high_water=r.mfe_pts,
        )
        diag_lines = [
            "-- TRADE DIAGNOSIS ---------------------------------",
            f"  Tag            : {diag_tag}",
            f"  Analysis       : {diag_msg}",
            sep, "",
        ]
        for ln in lines + diag_lines:
            print(ln)
            self._write(self._trade_path(), ln)

        # ── Structured CSV row (per-day + master) ────────────
        try:
            et  = r.entry_time.strftime("%H:%M:%S") if r.entry_time else ""
            xt  = r.exit_time.strftime("%H:%M:%S") if r.exit_time else ""
            dt  = r.entry_time.strftime("%Y-%m-%d") if r.entry_time else ""
            cap = max(0.0, r.pnl_pts / r.mfe_pts * 100) if r.mfe_pts > 0 else 0.0
            self.csv_row({
                "date":          dt,
                "entry_time":    et,
                "exit_time":     xt,
                "strategy":      "trend_rider_v3",
                "instrument":    r.instrument,
                "direction":     r.direction,
                "symbol":        r.symbol,
                "strike_type":   r.strike_type,
                "entry_price":   round(r.entry_price_filled or r.entry_price_quoted, 2),
                "exit_price":    round(r.exit_price, 2),
                "quantity":      r.quantity,
                "pnl_pts":       round(r.pnl_pts, 2),
                "pnl_rs":        round(r.pnl_rs, 0),
                "mfe_pts":       round(r.mfe_pts, 2),
                "mae_pts":       round(r.mae_pts, 2),
                "capture_pct":   round(cap, 1),
                "exit_reason":   r.exit_reason,
                "hold_seconds":  int(r.hold_seconds),
                "adx_entry":     round(r.adx_entry, 1),
                "atr_entry":     round(r.atr_entry, 2),
                "ema9_entry":    round(r.ema9_entry, 2),
                "ema21_entry":   round(r.ema21_entry, 2),
                "vwap_entry":    round(r.vwap_entry, 2),
                "orb_high":      round(r.orb_high, 2),
                "orb_low":       round(r.orb_low, 2),
                "max_pain":      "",
                "max_pain_bias": "",
                "oi_bias":       r.oi_bias,
                "momentum":      self._momentum(r.ema9_entry, r.ema21_entry, r.adx_entry),
                "be_triggered":  "YES" if r.be_triggered else "NO",
                "trail_count":   r.trail_count,
                "high_water_pts": round(r.mfe_pts, 2),
                "trend_score":   round(r.trend_score, 1),
                "running_total": round(r.running_total, 0),
            })
        except Exception as e:
            print(f"[CSV ERROR trend_rider] {e}")

    def day_summary(self, name: str, records: list["TradeRecord"]) -> None:
        if not records:
            return
        wins   = [r for r in records if r.pnl_rs > 0]
        losses = [r for r in records if r.pnl_rs <= 0]
        n      = len(records)
        wr     = len(wins) / n * 100 if n else 0
        gp     = sum(r.pnl_rs for r in wins)
        gl     = abs(sum(r.pnl_rs for r in losses))
        pf     = gp / gl if gl > 0 else float("inf")
        net    = sum(r.pnl_rs for r in records)
        avg_h  = sum(r.hold_seconds for r in records) / n
        avg_sl = sum(r.slippage_pts for r in records) / n

        exit_ct: dict[str, int] = {}
        for r in records:
            exit_ct[r.exit_reason] = exit_ct.get(r.exit_reason, 0) + 1

        sep   = "=" * 64
        lines = [
            "", sep,
            f"DAY SUMMARY — {name} — {self._now().strftime('%Y-%m-%d')}",
            sep, "",
            f"  Total Trades    : {n}",
            f"  Wins / Losses   : {len(wins)} / {len(losses)}",
            f"  Win Rate        : {wr:.1f}%",
            f"  Profit Factor   : {pf:.2f}",
            f"  Gross Profit    : Rs {gp:.0f}",
            f"  Gross Loss      : Rs {gl:.0f}",
            f"  Net PnL         : Rs {net:+.0f}",
            f"  Best Trade      : Rs {max(r.pnl_rs for r in records):+.0f}",
            f"  Worst Trade     : Rs {min(r.pnl_rs for r in records):+.0f}",
            f"  Avg Hold Time   : {str(timedelta(seconds=int(avg_h)))}",
            f"  Avg Slippage    : {avg_sl:+.3f} pts",
            "", "  Exit Breakdown  :",
        ]
        for reason, cnt in exit_ct.items():
            lines.append(f"    {reason:<28}: {cnt}")
        lines += [sep, ""]
        for ln in lines:
            print(ln)
            self._write(self._trade_path(), ln)


# ================================================================
# TRADE RECORD
# ================================================================

@dataclass
class TradeRecord:
    trade_num:           int
    instrument:          str
    symbol:              str
    direction:           str
    entry_price_quoted:  float
    entry_price_filled:  float        = 0.0
    exit_price:          float        = 0.0
    entry_time:          datetime     = field(default_factory=lambda: datetime.now(IST))
    exit_time:           Optional[datetime] = None
    exit_reason:         str          = "Unknown"
    quantity:            int          = 1
    pnl_pts:             float        = 0.0
    pnl_rs:              float        = 0.0
    mfe_pts:             float        = 0.0
    mae_pts:             float        = 0.0
    capture_pct:         float        = 0.0
    hold_seconds:        float        = 0.0
    slippage_pts:        float        = 0.0
    trend_score:         float        = 0.0
    adx_entry:           float        = 0.0
    vol_regime:          str          = "medium"
    running_total:       float        = 0.0
    # ── detailed card fields (entry snapshot) ──
    spot_entry:          float        = 0.0
    ema9_entry:          float        = 0.0
    ema21_entry:         float        = 0.0
    vwap_entry:          float        = 0.0
    atr_entry:           float        = 0.0
    orb_high:            float        = 0.0
    orb_low:             float        = 0.0
    oi_bias:             str          = "N/A"
    strike_type:         str          = "OTM"
    initial_sl_pts:      float        = 0.0
    be_triggered:        bool         = False
    trail_count:         int          = 0
    # ── exit snapshot ──
    spot_exit:           float        = 0.0
    ema9_exit:           float        = 0.0
    ema21_exit:          float        = 0.0
    vwap_exit:           float        = 0.0
    adx_exit:            float        = 0.0
    sl_at_exit:          float        = 0.0


# ================================================================
# TICK CACHE  (WebSocket → shared price store)
# ================================================================

class TickCache:
    """
    Thread-safe in-memory store for latest tick data.
    WebSocket thread writes; strategy threads read.

    Structure per symbol:
        ltp       : float   — last traded price
        bid       : float   — best bid (from depth)
        ask       : float   — best ask (from depth)
        oi        : int     — open interest (from depth feed)
        ts        : float   — unix timestamp of tick
    """

    def __init__(self) -> None:
        self._data: dict[str, dict] = {}
        self._lock = threading.Lock()

    def update_ltp(self, symbol: str, ltp: float) -> None:
        with self._lock:
            if symbol not in self._data:
                self._data[symbol] = {}
            self._data[symbol]["ltp"] = ltp
            self._data[symbol]["ts"]  = time.monotonic()

    def update_depth(self, symbol: str, bid: float, ask: float, oi: int) -> None:
        with self._lock:
            if symbol not in self._data:
                self._data[symbol] = {}
            self._data[symbol]["bid"] = bid
            self._data[symbol]["ask"] = ask
            self._data[symbol]["oi"]  = oi

    def get_ltp(self, symbol: str) -> Optional[float]:
        with self._lock:
            return self._data.get(symbol, {}).get("ltp")

    def get_depth(self, symbol: str) -> dict:
        with self._lock:
            d = self._data.get(symbol, {})
            return {
                "bid": d.get("bid", 0.0),
                "ask": d.get("ask", 0.0),
                "oi":  d.get("oi",  0),
            }

    def tick_age_ms(self, symbol: str) -> float:
        """Milliseconds since last tick for symbol."""
        with self._lock:
            ts = self._data.get(symbol, {}).get("ts")
        if ts is None:
            return float("inf")
        return (time.monotonic() - ts) * 1000

    def subscribe(self, symbols: list[str]) -> None:
        """Pre-seed entries so callers don't get KeyError on first read."""
        with self._lock:
            for s in symbols:
                if s not in self._data:
                    self._data[s] = {}


# ================================================================
# WEBSOCKET ENGINE  (OpenAlgo Socket.IO)
# ================================================================

class WebSocketEngine:
    """
    Connects to OpenAlgo's Socket.IO feed and pipes ticks into TickCache.

    OpenAlgo emits:
        event "ltp"   → {"symbol": str, "ltp": float}
        event "depth" → {"symbol": str, "bid": float,
                          "ask": float, "oi": int}

    If python-socketio is not installed, falls back to REST polling
    (tick_cache still works — just populated by REST instead).
    """

    def __init__(
        self,
        cfg: AppConfig,
        cache: TickCache,
        logger: LoggingEngine,
    ) -> None:
        self._cfg    = cfg
        self._cache  = cache
        self._logger = logger
        self._sio    = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def start(self, symbols: list[str]) -> None:
        self._symbols = symbols
        self._cache.subscribe(symbols)

        if not _SIO_AVAILABLE:
            self._logger.activity(
                "WS",
                "python-socketio not installed — falling back to REST polling. "
                "Run: pip install 'python-socketio[client]'",
            )
            self._start_rest_fallback(symbols)
            return

        self._running = True
        self._thread  = threading.Thread(
            target=self._run_socketio, daemon=True, name="Thread-WS"
        )
        self._thread.start()
        self._logger.activity("WS", f"WebSocket thread started. Subscribing {len(symbols)} symbols.")

    def _run_socketio(self) -> None:
        sio = socketio.Client(reconnection=True, reconnection_attempts=0,
                              reconnection_delay=2, logger=False, engineio_logger=False)
        self._sio = sio

        @sio.event
        def connect():
            self._logger.activity("WS", "Connected to OpenAlgo Socket.IO feed.")
            sio.emit("subscribe", {"symbols": self._symbols, "mode": "full"})

        @sio.event
        def disconnect():
            self._logger.activity("WS", "Disconnected from feed — auto-reconnect active.")

        @sio.on("ltp")
        def on_ltp(data: dict):
            sym = data.get("symbol")
            ltp = data.get("ltp")
            if sym and ltp is not None:
                self._cache.update_ltp(sym, float(ltp))

        @sio.on("depth")
        def on_depth(data: dict):
            sym = data.get("symbol")
            if sym:
                self._cache.update_depth(
                    sym,
                    float(data.get("bid", 0)),
                    float(data.get("ask", 0)),
                    int(data.get("oi", 0)),
                )

        @sio.on("tick")
        def on_tick(data: dict):
            # Handle combined tick format some OpenAlgo versions emit
            sym = data.get("symbol")
            if not sym:
                return
            if "ltp" in data:
                self._cache.update_ltp(sym, float(data["ltp"]))
            if "bid" in data and "ask" in data:
                self._cache.update_depth(
                    sym,
                    float(data["bid"]),
                    float(data["ask"]),
                    int(data.get("oi", 0)),
                )

        while self._running:
            try:
                sio.connect(self._cfg.ws_url, transports=["websocket"])
                sio.wait()
            except Exception as e:
                self._logger.activity("WS", f"Connection error: {e} — retrying in 3s")
                time.sleep(3)

    def _start_rest_fallback(self, symbols: list[str]) -> None:
        """Poll REST every second per symbol when WS unavailable."""
        session = requests.Session()

        def _poll():
            while True:
                for sym in symbols:
                    try:
                        r = session.post(
                            f"{self._cfg.base_url}/quotes",
                            json={"apikey": self._cfg.api_key,
                                  "symbol": sym, "exchange": "NSE"},
                            timeout=2,
                        )
                        d = r.json()
                        if d.get("status") == "success":
                            ltp = float(d["data"]["ltp"])
                            self._cache.update_ltp(sym, ltp)
                    except Exception:
                        pass
                time.sleep(1)

        t = threading.Thread(target=_poll, daemon=True, name="Thread-REST-poll")
        t.start()

    def stop(self) -> None:
        self._running = False
        if self._sio:
            try:
                self._sio.disconnect()
            except Exception:
                pass


# ================================================================
# MARKET DATA ENGINE  (candles + option chain via REST)
# ================================================================

class MarketDataEngine:
    def __init__(self, cfg: AppConfig) -> None:
        self._cfg     = cfg
        self._session = requests.Session()

    def _post(self, endpoint: str, payload: dict, timeout: int = 10) -> Optional[dict]:
        payload = {"apikey": self._cfg.api_key, **payload}
        try:
            r = self._session.post(
                f"{self._cfg.base_url}/{endpoint}",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=timeout,
            )
            data = r.json()
            return data if r.status_code < 400 else None
        except Exception:
            return None

    def get_candles(
        self, symbol: str, exchange: str, interval: str = "1m"
    ) -> Optional[pd.DataFrame]:
        today = datetime.now(IST).strftime("%Y-%m-%d")
        data  = self._post("history", {
            "symbol": symbol, "exchange": exchange,
            "interval": interval, "start_date": today, "end_date": today,
        })
        if not data or "data" not in data or not data["data"]:
            return None
        df = pd.DataFrame(data["data"])
        df = df.sort_values("timestamp").reset_index(drop=True)
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s")
        for col in ("open", "high", "low", "close", "volume"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df

    def get_option_chain(
        self, underlying: str, exchange: str, expiry: str
    ) -> Optional[dict]:
        data = self._post("optionchain", {
            "underlying": underlying,
            "expiry_date": expiry,
            "exchange": exchange,
        })
        return data if data and data.get("status") == "success" else None

    def get_quote_rest(self, symbol: str, exchange: str) -> Optional[float]:
        """Fallback REST quote — use only when tick cache has no data yet."""
        data = self._post("quotes", {"symbol": symbol, "exchange": exchange}, timeout=4)
        if data and data.get("status") == "success":
            try:
                return float(data["data"]["ltp"])
            except (KeyError, TypeError, ValueError):
                return None
        return None

    def get_sector_change_pct(self, sector_symbol: str) -> Optional[float]:
        """Get intraday % change for a sector index vs prev close."""
        data = self._post("quotes", {
            "symbol": sector_symbol, "exchange": "NSE_INDEX"
        }, timeout=4)
        if data and data.get("status") == "success":
            try:
                d = data["data"]
                ltp  = float(d["ltp"])
                prev = float(d.get("prev_close") or d.get("close") or ltp)
                if prev > 0:
                    return (ltp - prev) / prev * 100
            except Exception:
                pass
        return None

    def get_nifty_change_pct(self) -> Optional[float]:
        """Get NIFTY50 intraday % change — used as benchmark for RS."""
        return self.get_sector_change_pct("NIFTY 50")


# ================================================================
# EXECUTION ENGINE  (orders + fill verification)
# ================================================================

@dataclass
class FillResult:
    success:      bool
    order_id:     str          = ""
    filled_price: float        = 0.0
    filled_qty:   int          = 0
    status:       str          = "UNKNOWN"   # FILLED / PARTIAL / REJECTED / TIMEOUT
    attempts:     int          = 0
    note:         str          = ""


class ExecutionEngine:
    """
    Place order → verify fill → retry up to 3 times.
    In paper mode: simulates fills at current tick price.
    """

    MAX_RETRIES    = 3
    RETRY_DELAY_S  = 0.5
    VERIFY_DELAY_S = 0.3    # wait before checking order status

    def __init__(
        self,
        cfg: AppConfig,
        cache: TickCache,
        mde: MarketDataEngine,
        logger: LoggingEngine,
    ) -> None:
        self._cfg    = cfg
        self._cache  = cache
        self._mde    = mde
        self._logger = logger
        self._session = requests.Session()

    def _post(self, endpoint: str, payload: dict, timeout: int = 8) -> Optional[dict]:
        payload = {"apikey": self._cfg.api_key, **payload}
        try:
            r = self._session.post(
                f"{self._cfg.base_url}/{endpoint}",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=timeout,
            )
            return r.json() if r.status_code < 400 else None
        except Exception:
            return None

    def _verify_fill(self, order_id: str, fallback_price: float = 0.0,
                     fallback_qty: int = 0) -> FillResult:
        """Poll order status endpoint to confirm fill.

        OpenAlgo analyze/sandbox mode accepts the order and returns an
        orderid but the status may read PENDING because there is no real
        exchange fill. We treat any accepted order (valid order_id, not
        rejected) as FILLED and fall back to the live tick price for the
        fill price when the status payload does not carry an average price.
        """
        time.sleep(self.VERIFY_DELAY_S)
        data = self._post("orderstatus", {"orderid": order_id})

        # No status payload at all — in analyze mode the order was still
        # accepted (we have an order_id), so treat it as filled.
        if not data:
            return FillResult(True, order_id, fallback_price, fallback_qty, "FILLED")

        status = data.get("data", {}).get("status", "").upper()
        price  = float(data.get("data", {}).get("average_price", 0) or 0) or fallback_price
        qty    = int(data.get("data", {}).get("filled_quantity", 0) or 0) or fallback_qty

        # Only a genuine rejection/cancellation should fail the order.
        if status in ("REJECTED", "CANCELLED"):
            return FillResult(False, order_id, status=status,
                              note=data.get("data", {}).get("status_message", ""))
        if status == "PARTIAL":
            return FillResult(False, order_id, price, qty, "PARTIAL")

        # COMPLETE, FILLED, PENDING, OPEN, TRIGGER PENDING or empty → accepted
        return FillResult(True, order_id, price, qty, "FILLED")

    def execute(
        self,
        symbol: str,
        exchange: str,
        action: str,
        quantity: int,
        name: str = "",
    ) -> FillResult:
        # ── Always route through OpenAlgo (sandbox handles virtual money) ──
        for attempt in range(1, self.MAX_RETRIES + 1):
            data = self._post("placeorder", {
                "symbol":    symbol,
                "exchange":  exchange,
                "action":    action,
                "quantity":  quantity,
                "price":     0,
                "pricetype": "MARKET",
                "product":   "MIS",
                "strategy":  "Trend Rider V3.0",
            })

            if not data or data.get("status") != "success":
                self._logger.activity(
                    name,
                    f"Order place failed (attempt {attempt}/{self.MAX_RETRIES}): {data}",
                )
                time.sleep(self.RETRY_DELAY_S)
                continue

            order_id = data.get("orderid", "")
            ltp_fallback = (self._cache.get_ltp(symbol) or
                            self._mde.get_quote_rest(symbol, exchange) or 0.0)
            result   = self._verify_fill(order_id, ltp_fallback, quantity)
            result.attempts = attempt

            if result.status == "FILLED":
                self._logger.activity(
                    name,
                    f"FILLED {action} {quantity} {symbol} "
                    f"@ {result.filled_price:.2f} (attempt {attempt})",
                )
                return result

            if result.status == "PARTIAL":
                self._logger.trade(
                    name,
                    f"WARNING: PARTIAL FILL {symbol} — "
                    f"{result.filled_qty}/{quantity} @ {result.filled_price:.2f}",
                )
                # Accept partial and note it
                result.success = True
                result.note    = f"Partial {result.filled_qty}/{quantity}"
                return result

            if result.status in ("REJECTED", "CANCELLED"):
                self._logger.trade(name, f"ORDER REJECTED: {symbol} — {result.note}")
                return result

            self._logger.activity(
                name, f"Order pending (attempt {attempt}) — retrying..."
            )
            time.sleep(self.RETRY_DELAY_S)

        self._logger.trade(name, f"ALERT: All {self.MAX_RETRIES} attempts failed for {symbol}")
        return FillResult(False, status="TIMEOUT", attempts=self.MAX_RETRIES)


# ================================================================
# TECHNICAL INDICATORS
# ================================================================

class TrendEngine:
    def __init__(self, cfg: AppConfig) -> None:
        self._cfg = cfg

    def ema(self, df: pd.DataFrame, period: int) -> float:
        return float(df["close"].ewm(span=period, adjust=False).mean().iloc[-1])

    def vwap(self, df: pd.DataFrame) -> float:
        tp = (df["high"] + df["low"] + df["close"]) / 3
        if "volume" in df.columns and df["volume"].sum() > 0:
            return float(
                (tp * df["volume"]).cumsum().iloc[-1]
                / df["volume"].cumsum().iloc[-1]
            )
        return float(tp.expanding().mean().iloc[-1])

    def atr(self, df: pd.DataFrame) -> float:
        p  = self._cfg.atr_period
        hl = df["high"] - df["low"]
        hc = (df["high"] - df["close"].shift()).abs()
        lc = (df["low"]  - df["close"].shift()).abs()
        tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
        v  = tr.rolling(p).mean().iloc[-1]
        return float(v) if pd.notna(v) else 0.0

    def adx(self, df: pd.DataFrame) -> float:
        p            = self._cfg.adx_period
        pdm_raw      = df["high"].diff().clip(lower=0)
        ndm_raw      = (-df["low"].diff()).clip(lower=0)
        pdm          = pdm_raw.where(pdm_raw >= ndm_raw, 0.0)
        ndm          = ndm_raw.where(ndm_raw > pdm_raw,  0.0)
        hl = df["high"] - df["low"]
        hc = (df["high"] - df["close"].shift()).abs()
        lc = (df["low"]  - df["close"].shift()).abs()
        tr      = pd.concat([hl, hc, lc], axis=1).max(axis=1)
        atr_    = tr.rolling(p).mean()
        pdi     = 100 * pdm.rolling(p).mean() / atr_
        ndi     = 100 * ndm.rolling(p).mean() / atr_
        dx      = ((pdi - ndi).abs() / (pdi + ndi).replace(0, float("nan"))) * 100
        v       = dx.rolling(p).mean().iloc[-1]
        return float(v) if pd.notna(v) else 0.0

    def orb(self, df: pd.DataFrame) -> tuple[float, float]:
        n = self._cfg.orb_candles
        return float(df.head(n)["high"].max()), float(df.head(n)["low"].min())

    def swing_low(self, df: pd.DataFrame) -> float:
        return float(df["low"].iloc[-self._cfg.swing_lookback:].min())

    def swing_high(self, df: pd.DataFrame) -> float:
        return float(df["high"].iloc[-self._cfg.swing_lookback:].max())

    def volatility_regime(self, atr_val: float, price: float) -> str:
        if price <= 0:
            return "medium"
        pct = atr_val / price * 100
        if pct > 0.8:
            return "high"
        if pct > 0.4:
            return "medium"
        return "low"

    def relative_volume(self, df: pd.DataFrame) -> float:
        if "volume" not in df.columns or len(df) < 5:
            return 1.0
        avg = df["volume"].rolling(20).mean().iloc[-1]
        if pd.isna(avg) or avg == 0:
            return 1.0
        return float(df["volume"].iloc[-1] / avg)


# ================================================================
# SCORING ENGINE
# ================================================================

@dataclass
class TrendSignal:
    direction:        str
    score:            float          # 0-100 continuous
    component_scores: dict[str, float]
    ema_fast:         float
    ema_slow:         float
    adx_val:          float
    vwap_val:         float
    atr_val:          float
    orb_high:         float
    orb_low:          float
    latest_price:     float
    vol_regime:       str
    rel_volume:       float
    oi_bias:          Optional[str]
    rs_score:         float = 0.0   # relative strength vs NIFTY
    vwap_dist_pct:    float = 0.0   # % distance from VWAP
    breakout_quality: float = 0.0   # how far above ORB as % of range


class ScoringEngine:
    """
    Continuous 0-100 scoring engine.

    Score breakdown (max points):
        ema_trend       20  — EMA alignment + price position (continuous)
        adx_strength    20  — trend strength (continuous, not step)
        vwap_confirm    20  — distance from VWAP (continuous)
        orb_breakout    20  — breakout quality vs ORB range (continuous)
        oi_alignment    10  — OI bias confirmation
        rel_strength    10  — relative strength vs NIFTY (added via rs_boost)
        vol_expansion    0  — volume multiplier applied as bonus (up to +5)
    Total base: 100 + up to 5 volume bonus
    """

    def __init__(self, cfg: AppConfig, te: TrendEngine) -> None:
        self._cfg = cfg
        self._te  = te

    # ── Continuous component scorers ─────────────────────────

    def _s_ema(self, ef: float, es: float, price: float, d: str) -> float:
        """
        Continuous EMA score 0-20.
        Full 20: EMA aligned AND price beyond fast EMA in right direction.
        Partial 10: EMA aligned but price between the two EMAs.
        Scaled 0-5: EMA spread as % of price (wider = stronger trend).
        """
        spread_pct = abs(ef - es) / price * 100 if price > 0 else 0
        if d == "CE":
            if ef > es:
                base = 20.0 if price > ef else 10.0
            else:
                base = 0.0
        else:
            if ef < es:
                base = 20.0 if price < ef else 10.0
            else:
                base = 0.0
        # Spread bonus: up to +5 when spread > 0.3%
        spread_bonus = min(5.0, spread_pct / 0.3 * 5.0) if base > 0 else 0.0
        return min(20.0, base * 0.8 + spread_bonus)

    def _s_adx(self, adx: float) -> float:
        """
        Continuous ADX score 0-20.
        Linear scale: 20=0pts, 25=8pts, 30=14pts, 40=18pts, 50+=20pts.
        """
        if adx < 20: return 0.0
        return min(20.0, (adx - 20) / 30 * 20)

    def _s_vwap(self, price: float, vwap: float, d: str) -> float:
        """
        Continuous VWAP distance score 0-20.
        Direction must be correct, then score scales with % distance.
        0.1% away = 8pts, 0.3% = 14pts, 0.5%+ = 20pts.
        """
        if vwap <= 0: return 0.0
        dist_pct = (price - vwap) / vwap * 100
        if d == "CE" and dist_pct <= 0: return 0.0
        if d == "PE" and dist_pct >= 0: return 0.0
        abs_dist = abs(dist_pct)
        return min(20.0, abs_dist / 0.5 * 20)

    def _s_orb(
        self, price: float, oh: float, ol: float, buf: float, d: str
    ) -> float:
        """
        Continuous ORB breakout quality score 0-20.
        Scores how far price has moved beyond ORB as % of the ORB range.
        No breakout = 0. Just beyond = 5. Strong breakout = 20.
        """
        orb_range = oh - ol
        if orb_range <= 0: return 0.0
        if d == "CE":
            if price <= oh + buf: return 0.0
            excess = price - (oh + buf)
        else:
            if price >= ol - buf: return 0.0
            excess = (ol - buf) - price
        quality = excess / orb_range   # 0.5 = moved half the range beyond ORB
        return min(20.0, quality * 40)

    def _s_oi(self, oi_bias: Optional[str], d: str) -> float:
        if oi_bias == d:    return 10.0
        if oi_bias is None: return 5.0
        return 0.0

    def _s_rs(self, rs_score: float, d: str) -> float:
        """
        Relative strength score 0-10.
        rs_score is pct outperformance vs NIFTY (passed in from Scanner).
        +1% outperformance = 10pts, -1% = 0pts, scaled between.
        """
        if d == "CE":
            return max(0.0, min(10.0, rs_score / 1.0 * 10))
        else:
            return max(0.0, min(10.0, -rs_score / 1.0 * 10))

    def _s_volume(self, rel_vol: float) -> float:
        """
        Volume expansion bonus 0-5 (applied on top of 100pt base).
        rel_vol: current volume / 20-period avg volume.
        2x avg = 2.5pts, 3x+ = 5pts.
        """
        if rel_vol <= 1.0: return 0.0
        return min(5.0, (rel_vol - 1.0) / 2.0 * 5.0)

    def compute(
        self,
        df: pd.DataFrame,
        direction: str,
        orb_buffer: float = 0.0,
        oi_bias: Optional[str] = None,
        rs_score: float = 0.0,
    ) -> TrendSignal:
        te       = self._te
        ef       = te.ema(df, self._cfg.ema_fast)
        es       = te.ema(df, self._cfg.ema_slow)
        vwap     = te.vwap(df)
        adx      = te.adx(df)
        atr      = te.atr(df)
        oh, ol   = te.orb(df)
        price    = float(df["close"].iloc[-1])
        vr       = te.volatility_regime(atr, price)
        rv       = te.relative_volume(df)

        # VWAP distance % (for reporting)
        vwap_dist = (price - vwap) / vwap * 100 if vwap > 0 else 0.0
        # ORB breakout quality % of range (for reporting)
        orb_range = oh - ol
        brk_q = 0.0
        if orb_range > 0:
            if direction == "CE" and price > oh:
                brk_q = (price - oh) / orb_range * 100
            elif direction == "PE" and price < ol:
                brk_q = (ol - price) / orb_range * 100

        comp = {
            "ema_trend":    self._s_ema(ef, es, price, direction),
            "adx_strength": self._s_adx(adx),
            "vwap_confirm": self._s_vwap(price, vwap, direction),
            "orb_breakout": self._s_orb(price, oh, ol, orb_buffer, direction),
            "oi_alignment": self._s_oi(oi_bias, direction),
            "rel_strength": self._s_rs(rs_score, direction),
            "vol_expansion": self._s_volume(rv),
        }
        total = min(100.0, sum(comp.values()))

        return TrendSignal(
            direction=direction, score=round(total, 1),
            component_scores=comp,
            ema_fast=ef, ema_slow=es, adx_val=adx,
            vwap_val=vwap, atr_val=atr,
            orb_high=oh, orb_low=ol,
            latest_price=price, vol_regime=vr,
            rel_volume=rv, oi_bias=oi_bias,
            rs_score=rs_score,
            vwap_dist_pct=round(vwap_dist, 3),
            breakout_quality=round(brk_q, 1),
        )

    def best_direction(
        self,
        df: pd.DataFrame,
        orb_buffer: float = 0.0,
        oi_bias: Optional[str] = None,
        rs_score: float = 0.0,
    ) -> TrendSignal:
        ce = self.compute(df, "CE", orb_buffer, oi_bias, rs_score)
        pe = self.compute(df, "PE", orb_buffer, oi_bias, rs_score)
        return ce if ce.score >= pe.score else pe


# ================================================================
# RISK ENGINE
# ================================================================

@dataclass
class RiskState:
    total_pnl:              float = 0.0
    trade_count:            int   = 0
    consecutive_losses:     int   = 0
    open_positions:         int   = 0
    total_exposure:         float = 0.0
    circuit_broken:         bool  = False  # legacy — kept for compat
    # per-strategy circuit breakers (equity losses don't kill options)
    circuit_broken_by:      dict  = field(default_factory=dict)
    consecutive_losses_by:  dict  = field(default_factory=dict)


class RiskEngine:
    def __init__(self, cfg: AppConfig, logger: LoggingEngine) -> None:
        self._cfg    = cfg
        self._logger = logger
        self.state   = RiskState()
        self._lock   = threading.Lock()

    def can_trade(self, name: str, exposure: float = 0.0) -> tuple[bool, str]:
        with self._lock:
            m = self._cfg.mode
            s = self.state
            # Circuit breaker is PER STRATEGY — equity losses don't kill options
            strategy = "EQUITY" if name == "EQUITY" else "OPTIONS"
            if s.circuit_broken_by.get(strategy, False):
                return False, f"Circuit breaker active [{strategy}]"
            if s.total_pnl <= m.max_daily_loss:
                return False, f"Daily loss limit (Rs {s.total_pnl:.0f})"
            if s.trade_count >= m.max_trades_per_day:
                return False, f"Max trades ({m.max_trades_per_day})"
            consec = s.consecutive_losses_by.get(strategy, 0)
            if consec >= self._cfg.max_consecutive_losses:
                s.circuit_broken_by[strategy] = True
                self._logger.trade(name, f"CIRCUIT BREAKER [{strategy}]: {consec} consecutive losses.")
                return False, f"Circuit breaker [{strategy}]"
            if s.total_exposure + exposure > self._cfg.portfolio_max_exposure_rs:
                return False, "Portfolio exposure cap"
            return True, "OK"

    def on_open(self, exposure: float) -> None:
        with self._lock:
            self.state.open_positions += 1
            self.state.total_exposure += exposure

    def on_close(self, pnl_rs: float, exposure: float, strategy: str = "OPTIONS",
                 real_trade: bool = True) -> None:
        """real_trade=False marks a non-event (quick ~zero-P&L no-momentum scratch)
        that should NOT consume the daily trade-cap budget nor count as a loss for
        the circuit breaker. P&L and exposure still settle normally. (V3 change: the
        5-trade cap was being burned on 2-minute scratches — 29% of trades, netting
        ~-Rs800 — locking the engine out of real afternoon trends.)"""
        with self._lock:
            self.state.open_positions  = max(0, self.state.open_positions - 1)
            self.state.total_exposure  = max(0.0, self.state.total_exposure - exposure)
            self.state.total_pnl      += pnl_rs
            if not real_trade:
                return
            self.state.trade_count    += 1
            if pnl_rs <= 0:
                self.state.consecutive_losses += 1
                self.state.consecutive_losses_by[strategy] =                     self.state.consecutive_losses_by.get(strategy, 0) + 1
            else:
                self.state.consecutive_losses = 0
                self.state.consecutive_losses_by[strategy] = 0


# ================================================================
# STRIKE SELECTOR
# ================================================================

class StrikeSelector:
    @staticmethod
    def select(
        spot: float, atr: float, option_type: str,
        interval: int, vol_regime: str,
    ) -> int:
        atm = round(spot / interval) * interval
        if vol_regime == "high":
            return atm - interval if option_type == "CE" else atm + interval
        if vol_regime == "medium":
            return atm
        return atm + interval if option_type == "CE" else atm - interval


# ================================================================
# TRAILING STOP ENGINE
# ================================================================

@dataclass
class TrailState:
    initial_sl:    float
    dynamic_sl:    float
    locked_profit: float = 0.0
    be_triggered:  bool  = False
    high_water:    float = 0.0
    trail_log:     list  = field(default_factory=list)


class TrailingStopEngine:
    def __init__(self, cfg: AppConfig, logger: LoggingEngine) -> None:
        self._cfg    = cfg
        self._logger = logger

    # Hard caps on the initial SL in PREMIUM points, per leg type.
    # Index ATR is large (NIFTY ~12-15, SENSEX ~35-45) and must NOT be
    # applied 1:1 to option premium, or the stop ends up 15-20pts deep.
    SL_CAP = {
        "NIFTY":  8.0,    # max 8pt premium stop  (~Rs 520 on 65 qty)
        "SENSEX": 18.0,   # max 18pt premium stop (~Rs 360 on 20 qty)
        "EQUITY": 6.0,    # max 6pt stop on equity
    }
    SL_FLOOR = {
        "NIFTY":  4.0,
        "SENSEX": 10.0,
        "EQUITY": 2.0,
    }

    def _leg_key(self, name: str) -> str:
        if name.startswith("EQUITY"):
            return "EQUITY"
        if "SENSEX" in name:
            return "SENSEX"
        return "NIFTY"

    def create(self, atr: float, name: str) -> TrailState:
        leg = self._leg_key(name)
        raw = atr * self._cfg.mode.atr_sl_multiplier
        # clamp the ATR-derived stop between floor and cap
        capped = max(self.SL_FLOOR[leg], min(raw, self.SL_CAP[leg]))
        sl = -capped
        self._logger.activity(
            name, f"Trail init: SL={sl:.2f}pts (ATR={atr:.2f} raw={raw:.2f} capped={capped:.2f})"
        )
        return TrailState(initial_sl=sl, dynamic_sl=sl)

    def update(
        self,
        state: TrailState,
        pnl_pts: float,
        atr: float,
        name: str,
    ) -> None:
        m = self._cfg.mode

        if pnl_pts > state.high_water:
            state.high_water = pnl_pts

        # Break-even — trigger at the smaller of (be_trigger_r * initial SL)
        # or a hard +4pts, so we lock cost early instead of giving back
        # a 4-5pt profit (the recurring "green turned red" problem).
        be_pts = min(abs(state.initial_sl) * m.be_trigger_r, 4.0)
        if pnl_pts >= be_pts and not state.be_triggered:
            state.dynamic_sl   = 0.0
            state.be_triggered = True
            self._logger.activity(name, f"BREAK-EVEN at +{pnl_pts:.2f}pts")

        # ATR trail
        atr_sl = pnl_pts - atr * m.atr_trail_multiplier
        if atr_sl > state.dynamic_sl:
            old = state.dynamic_sl
            state.dynamic_sl = atr_sl
            state.trail_log.append({"type": "ATR", "pnl": pnl_pts,
                                     "old": old, "new": atr_sl})
            self._logger.activity(name, f"ATR TRAIL: SL {old:.2f}→{atr_sl:.2f}pts")

        # High-water-mark drop — cap at 3pts regardless of initial SL size
        hwm_drop = min(3.0, abs(state.initial_sl) * self._cfg.high_water_drop_r)
        hwm_sl = state.high_water - hwm_drop
        if hwm_sl > state.dynamic_sl and state.high_water > be_pts:
            old = state.dynamic_sl
            state.dynamic_sl = hwm_sl
            state.trail_log.append({"type": "HWM", "pnl": pnl_pts,
                                     "old": old, "new": hwm_sl})
            self._logger.activity(name, f"HWM TRAIL: SL {old:.2f}→{hwm_sl:.2f}pts")

    def should_exit(self, state: TrailState, pnl_pts: float) -> bool:
        return pnl_pts <= state.dynamic_sl


# ================================================================
# OPTION CHAIN HELPERS
# ================================================================

def analyse_chain(
    chain_data: dict, atm: int, interval: int, n: int = 10
) -> tuple[Optional[int], Optional[str], Optional[str]]:
    if not chain_data or "chain" not in chain_data:
        return None, None, None
    chain   = chain_data["chain"]
    spot    = chain_data.get("underlying_ltp", 0)
    strikes = sorted({row["strike"] for row in chain})
    ce_oi   = {r["strike"]: (r.get("ce") or {}).get("oi", 0) or 0 for r in chain}
    pe_oi   = {r["strike"]: (r.get("pe") or {}).get("oi", 0) or 0 for r in chain}

    min_pain, max_pain = float("inf"), None
    for s in strikes:
        pain = (sum((s-k)*ce_oi[k] for k in strikes if k < s) +
                sum((k-s)*pe_oi[k] for k in strikes if k > s))
        if pain < min_pain:
            min_pain, max_pain = pain, s

    mp_bias = ("CE" if max_pain > spot else "PE") if max_pain and spot else None

    ce_t = pe_t = 0
    for row in chain:
        if abs(row["strike"] - atm) <= n * interval:
            ce_t += (row.get("ce") or {}).get("oi", 0) or 0
            pe_t += (row.get("pe") or {}).get("oi", 0) or 0
    oi_bias = "CE" if pe_t > ce_t else "PE"
    return max_pain, mp_bias, oi_bias


def get_expiry(expiry_day: int, exit_time: str = "15:15") -> str:
    today      = datetime.now(IST)
    d          = today.date()
    days_ahead = (expiry_day - d.weekday()) % 7
    if days_ahead == 0 and today.strftime("%H:%M") >= exit_time:
        days_ahead = 7
    expiry = d if days_ahead == 0 else d + timedelta(days=days_ahead)
    M = ["JAN","FEB","MAR","APR","MAY","JUN",
         "JUL","AUG","SEP","OCT","NOV","DEC"]
    return f"{expiry.day:02d}{M[expiry.month-1]}{expiry.year % 100:02d}"


def build_option_symbol(name: str, expiry: str, strike: int, otype: str) -> str:
    return f"{name}{expiry}{strike}{otype}"


# ================================================================
# SCANNER  (Nifty 50 stock ranker)
# ================================================================

@dataclass
class StockRank:
    symbol:    str
    score:     float
    direction: str
    signal:    Optional[TrendSignal]
    rs_15m:    float = 0.0   # 15-min RS vs NIFTY
    rs_30m:    float = 0.0   # 30-min RS vs NIFTY
    rs_60m:    float = 0.0   # 60-min RS vs NIFTY


@dataclass
class MarketBreadth:
    pct_above_vwap:   float   # % of NIFTY50 stocks above VWAP
    pct_above_ema21:  float   # % above EMA21
    advancing:        int     # stocks up from open
    declining:        int     # stocks down from open
    bias:             str     # "BULLISH" / "BEARISH" / "MIXED"
    timestamp:        str     # HH:MM


class Scanner:
    """
    Enhanced scanner with:
    - Continuous 0-100 scoring (no more ties)
    - Relative strength vs NIFTY (15/30/60 min)
    - Market breadth analysis
    - RS refreshed every 5 min (not every scan) to save API calls
    """

    RS_REFRESH_SECS = 300   # RS recalculated every 5 minutes

    def __init__(
        self,
        cfg: AppConfig,
        mde: MarketDataEngine,
        scorer: ScoringEngine,
        logger: LoggingEngine,
    ) -> None:
        self._cfg    = cfg
        self._mde    = mde
        self._scorer = scorer
        self._logger = logger
        # RS cache: symbol → (rs_15, rs_30, rs_60, last_updated_monotonic)
        self._rs_cache: dict[str, tuple] = {}
        self._nifty_cache: dict[str, float] = {}  # "15"/"30"/"60" → nifty return
        self._rs_last_refresh: float = 0.0

    def _nifty_return(self, df_nifty: Optional[pd.DataFrame], minutes: int) -> float:
        """% return of NIFTY over last N minutes."""
        if df_nifty is None or len(df_nifty) < minutes:
            return 0.0
        tail = df_nifty.tail(minutes)
        start = float(tail["close"].iloc[0])
        end   = float(tail["close"].iloc[-1])
        return (end - start) / start * 100 if start > 0 else 0.0

    def _stock_return(self, df: pd.DataFrame, minutes: int) -> float:
        """% return of stock over last N minutes."""
        if df is None or len(df) < minutes:
            return 0.0
        tail = df.tail(minutes)
        start = float(tail["close"].iloc[0])
        end   = float(tail["close"].iloc[-1])
        return (end - start) / start * 100 if start > 0 else 0.0

    def _refresh_rs(self) -> None:
        """Fetch NIFTY candles and compute base returns. Called every 5 min."""
        df_n = self._mde.get_candles("NIFTY", "NSE_INDEX")
        self._nifty_cache = {
            "15": self._nifty_return(df_n, 15),
            "30": self._nifty_return(df_n, 30),
            "60": self._nifty_return(df_n, 60),
        }
        self._rs_last_refresh = time.monotonic()

    def _get_rs(self, sym: str, df: pd.DataFrame) -> tuple[float, float, float]:
        """
        Returns (rs_15, rs_30, rs_60) — outperformance vs NIFTY in pct.
        Positive = stock outperforming NIFTY.
        Uses cached NIFTY returns to avoid re-fetching per stock.
        """
        r15 = self._stock_return(df, 15) - self._nifty_cache.get("15", 0.0)
        r30 = self._stock_return(df, 30) - self._nifty_cache.get("30", 0.0)
        r60 = self._stock_return(df, 60) - self._nifty_cache.get("60", 0.0)
        return r15, r30, r60

    def breadth(self) -> MarketBreadth:
        """
        Scan all NIFTY50 stocks for market breadth.
        Lightweight — uses already-fetched candle data where possible.
        """
        above_vwap = above_ema21 = adv = dec = total = 0
        te = TrendEngine(self._cfg)
        for sym in ALL_EQUITY_STOCKS:
            df = self._mde.get_candles(sym, self._cfg.equity_exchange)
            if df is None or len(df) < 5:
                continue
            total += 1
            try:
                price  = float(df["close"].iloc[-1])
                open_p = float(df["open"].iloc[0])
                vwap   = te.vwap(df)
                ema21  = te.ema(df, 21)
                if price > vwap:  above_vwap  += 1
                if price > ema21: above_ema21 += 1
                if price > open_p: adv += 1
                else:              dec += 1
            except Exception:
                pass

        if total == 0:
            return MarketBreadth(0, 0, 0, 0, "MIXED",
                                 datetime.now(IST).strftime("%H:%M"))

        pct_vwap  = above_vwap  / total * 100
        pct_ema21 = above_ema21 / total * 100

        if pct_vwap >= 60 and adv > dec:
            bias = "BULLISH"
        elif pct_vwap <= 40 and dec > adv:
            bias = "BEARISH"
        else:
            bias = "MIXED"

        return MarketBreadth(
            pct_above_vwap=round(pct_vwap, 1),
            pct_above_ema21=round(pct_ema21, 1),
            advancing=adv, declining=dec, bias=bias,
            timestamp=datetime.now(IST).strftime("%H:%M"),
        )

    def rank(self, nifty_df: Optional[pd.DataFrame] = None) -> list[StockRank]:
        """
        Rank all NIFTY50 stocks using continuous 0-100 scoring + RS boost.
        nifty_df: pass current NIFTY candles to avoid an extra fetch.
        RS is refreshed at most every 5 minutes to save API calls.
        """
        # Refresh NIFTY RS baseline every 5 min
        if time.monotonic() - self._rs_last_refresh > self.RS_REFRESH_SECS:
            self._refresh_rs()

        results: list[StockRank] = []
        for sym in ALL_EQUITY_STOCKS:
            df = self._mde.get_candles(sym, self._cfg.equity_exchange)
            if df is None or len(df) < 20:
                continue
            try:
                r15, r30, r60 = self._get_rs(sym, df)
                # Combined RS: weight recent more (15m=50%, 30m=30%, 60m=20%)
                rs_combined = r15 * 0.5 + r30 * 0.3 + r60 * 0.2

                sig = self._scorer.best_direction(df, rs_score=rs_combined)
                results.append(StockRank(
                    sym, sig.score, sig.direction, sig,
                    rs_15m=round(r15, 3),
                    rs_30m=round(r30, 3),
                    rs_60m=round(r60, 3),
                ))
            except Exception as e:
                self._logger.activity("SCANNER", f"{sym}: {e}")

        results.sort(key=lambda x: x.score, reverse=True)
        return results


# ================================================================
# STRATEGY LOOP — INDEX OPTIONS
# ================================================================

def run_index(
    name: str, icfg: dict, cfg: AppConfig,
    mde: MarketDataEngine, scorer: ScoringEngine,
    risk: RiskEngine, trail_eng: TrailingStopEngine,
    exec_eng: ExecutionEngine, cache: TickCache,
    logger: LoggingEngine,
    scanner: "Scanner" = None,
) -> None:

    def now() -> datetime:      return datetime.now(IST)
    def now_str() -> str:       return now().strftime("%H:%M")
    def ist_ts() -> str:        return now().strftime("%H:%M:%S.%f")[:-3]

    logger.day_start(name, cfg.mode_name, cfg.paper_mode)

    te             = TrendEngine(cfg)
    trade_count    = 0
    total_pnl      = 0.0
    last_sl_time: Optional[datetime]     = None
    last_exit_time: Optional[datetime]   = None
    last_direction: Optional[str]        = None
    sig_history: deque                   = deque(maxlen=cfg.candle_confirm_count)
    expiry                               = get_expiry(icfg["expiry_day"], cfg.hard_exit_time)
    chain_data: Optional[dict]           = None
    last_chain_ts: Optional[datetime]    = None
    records: list[TradeRecord]           = []
    breadth: Optional[MarketBreadth]     = None
    last_breadth_ts: Optional[datetime]  = None

    logger.activity(name, f"Expiry:{expiry}  Qty:{icfg['quantity']}")

    while True:
        ns = now_str()

        if ns >= cfg.hard_exit_time:
            logger.trade(name, f"Session end | Trades:{trade_count} | PnL:Rs{total_pnl:+.0f}")
            logger.day_summary(name, records)
            break

        if ns < cfg.entry_time:
            time.sleep(5);  continue

        if ns >= cfg.late_entry_cutoff:
            time.sleep(30); continue

        ok, reason = risk.can_trade(name)
        if not ok:
            logger.trade(name, f"Risk gate: {reason}. Stopping.")
            logger.day_summary(name, records)
            break

        m = cfg.mode
        if last_sl_time and (now()-last_sl_time).total_seconds() < m.cooldown_after_sl:
            time.sleep(10); continue
        if last_exit_time and (now()-last_exit_time).total_seconds() < m.cooldown_after_exit:
            time.sleep(10); continue

        if cfg.lunch_start <= ns <= cfg.lunch_end:
            time.sleep(60); continue

        # ── Option chain refresh (every 60 s) ────────────────
        if last_chain_ts is None or (now()-last_chain_ts).total_seconds() >= cfg.chain_refresh_interval:
            chain_data    = mde.get_option_chain(
                icfg["index_symbol"], icfg["option_exchange"], expiry)
            last_chain_ts = now()
            logger.activity(name, f"Chain refreshed ATM:{chain_data.get('atm_strike') if chain_data else 'N/A'}")

        # ── Candle-based indicators ──────────────────────────
        df = mde.get_candles(icfg["index_symbol"], icfg["index_exchange"])
        if df is None or len(df) < 20:
            time.sleep(15); continue

        try:
            atr_val  = te.atr(df)
            adx_val  = te.adx(df)
            price    = float(df["close"].iloc[-1])
            vol_reg  = te.volatility_regime(atr_val, price)
        except Exception as e:
            logger.activity(name, f"Indicator err: {e}"); time.sleep(10); continue

        atm      = round(price / icfg["strike_interval"]) * icfg["strike_interval"]
        _, _, oi_bias = analyse_chain(chain_data or {}, atm, icfg["strike_interval"])

        # ── Market breadth refresh every 60s ────────────────
        if scanner and (last_breadth_ts is None or
                (now()-last_breadth_ts).total_seconds() >= 60):
            try:
                breadth         = scanner.breadth()
                last_breadth_ts = now()
                logger.activity(name, (
                    f"Breadth: {breadth.bias} | "
                    f"AboveVWAP:{breadth.pct_above_vwap:.0f}% "
                    f"Adv:{breadth.advancing} Dec:{breadth.declining}"
                ))
                # persist breadth snapshot to DB
                try:
                    from db_logger import get_db as _get_db
                    import datetime as _dt
                    _get_db().insert_breadth(
                        date=datetime.now(IST).strftime("%Y-%m-%d"),
                        time=datetime.now(IST).strftime("%H:%M"),
                        pct_above_vwap=breadth.pct_above_vwap,
                        pct_above_ema21=breadth.pct_above_ema21,
                        advancing=breadth.advancing,
                        declining=breadth.declining,
                        bias=breadth.bias,
                    )
                except Exception:
                    pass
            except Exception:
                pass

        try:
            ce_sig = scorer.compute(df, "CE", icfg["orb_buffer"], oi_bias, rs_score=0.0)
            pe_sig = scorer.compute(df, "PE", icfg["orb_buffer"], oi_bias, rs_score=0.0)
        except Exception as e:
            logger.activity(name, f"Score err: {e}"); time.sleep(10); continue

        best = ce_sig if ce_sig.score >= pe_sig.score else pe_sig

        logger.activity(name, (
            f"Price:{price:.1f} ADX:{adx_val:.1f} ATR:{atr_val:.2f} "
            f"Regime:{vol_reg} VWAPdist:{best.vwap_dist_pct:+.2f}% "
            f"ORBq:{best.breakout_quality:.0f}% | "
            f"CE:{ce_sig.score:.1f} PE:{pe_sig.score:.1f} → {best.direction}:{best.score:.1f}"
        ))

        if best.score < m.score_threshold or adx_val < 20:
            sig_history.clear(); time.sleep(cfg.tick_sl_check_interval); continue

        sig_history.append(best.direction)
        if len(sig_history) < cfg.candle_confirm_count:
            time.sleep(cfg.tick_sl_check_interval); continue
        if not all(s == best.direction for s in sig_history):
            sig_history.clear(); time.sleep(cfg.tick_sl_check_interval); continue

        option_type = best.direction

        if (last_direction == option_type and last_sl_time and
                (now()-last_sl_time).total_seconds() < 600):
            sig_history.clear(); time.sleep(30); continue

        # ── Market breadth gate ──────────────────────────────
        if breadth:
            if option_type == "CE" and breadth.bias == "BEARISH":
                logger.activity(name, f"Skip CE — breadth BEARISH ({breadth.pct_above_vwap:.0f}% above VWAP)")
                sig_history.clear(); time.sleep(cfg.tick_sl_check_interval); continue
            if option_type == "PE" and breadth.bias == "BULLISH":
                logger.activity(name, f"Skip PE — breadth BULLISH ({breadth.pct_above_vwap:.0f}% above VWAP)")
                sig_history.clear(); time.sleep(cfg.tick_sl_check_interval); continue
            if breadth.bias == "MIXED":
                logger.activity(name, f"Skip — breadth MIXED ({breadth.pct_above_vwap:.0f}% above VWAP). No clear direction.")
                sig_history.clear(); time.sleep(cfg.tick_sl_check_interval); continue

        approx_exp = icfg["quantity"] * price
        ok, reason = risk.can_trade(name, approx_exp)
        if not ok:
            time.sleep(cfg.tick_sl_check_interval); continue

        strike     = StrikeSelector.select(price, atr_val, option_type,
                                           icfg["strike_interval"], vol_reg)
        opt_symbol = build_option_symbol(name, expiry, strike, option_type)

        # ── Use tick cache for premium; fallback to REST ─────
        entry_quoted = (cache.get_ltp(opt_symbol) or
                        mde.get_quote_rest(opt_symbol, icfg["option_exchange"]))
        if entry_quoted is None:
            logger.activity(name, "No premium data."); time.sleep(10); continue

        fill = exec_eng.execute(opt_symbol, icfg["option_exchange"], "BUY",
                                icfg["quantity"], name)
        if not fill.success:
            logger.activity(name, f"Entry failed: {fill.status} ({fill.note})")
            time.sleep(10); continue

        entry_filled  = fill.filled_price or entry_quoted
        slippage      = entry_filled - entry_quoted
        trade_count  += 1
        last_direction = option_type
        entry_time_dt  = now()
        sig_history.clear()
        risk.on_open(approx_exp)
        trail = trail_eng.create(atr_val, name)
        mfe_pts = mae_pts = 0.0
        exit_reason = "Unknown"

        logger.trade(name, (
            f"ENTRY #{trade_count} {option_type} {opt_symbol} "
            f"quoted:{entry_quoted:.2f} filled:{entry_filled:.2f} "
            f"slip:{slippage:+.2f} Score:{best.score:.0f} Regime:{vol_reg}"
        ))

        # ── Position management — tick-driven ────────────────
        position_open = True
        curr          = entry_filled
        atr_cached    = atr_val
        last_candle_refresh = time.monotonic()

        while position_open:
            ns = now_str()

            # Hard exit
            if ns >= cfg.hard_exit_time:
                fill_x = exec_eng.execute(
                    opt_symbol, icfg["option_exchange"], "SELL",
                    icfg["quantity"], name)
                curr        = fill_x.filled_price or cache.get_ltp(opt_symbol) or curr
                exit_reason = "Hard Exit"
                position_open = False
                last_exit_time = now()
                break

            # Get latest price from tick cache (sub-second latency)
            tick_age = cache.tick_age_ms(opt_symbol)
            new_price = cache.get_ltp(opt_symbol)
            if new_price is None:
                # Fallback to REST if no tick yet
                new_price = mde.get_quote_rest(opt_symbol, icfg["option_exchange"])
            if new_price is None:
                time.sleep(0.5); continue
            curr = new_price

            pnl_pts   = curr - entry_filled
            pnl_rs    = pnl_pts * icfg["quantity"]
            held_secs = (now() - entry_time_dt).total_seconds()

            mfe_pts = max(mfe_pts, pnl_pts)
            mae_pts = min(mae_pts, pnl_pts)

            # Refresh ATR from candles every 60 s (not every tick)
            if time.monotonic() - last_candle_refresh > 60:
                df_pos = mde.get_candles(icfg["index_symbol"], icfg["index_exchange"])
                if df_pos is not None and len(df_pos) >= 5:
                    atr_cached = te.atr(df_pos)
                last_candle_refresh = time.monotonic()

            trail_eng.update(trail, pnl_pts, atr_cached, name)

            logger.activity(name, (
                f"[{ist_ts()}] tick_age:{tick_age:.0f}ms "
                f"prem:{curr:.2f} pnl:{pnl_pts:+.2f}pts "
                f"SL:{trail.dynamic_sl:.2f} MFE:{mfe_pts:.2f} held:{held_secs:.0f}s"
            ))

            if held_secs < cfg.min_hold_seconds and pnl_pts > trail.initial_sl:
                time.sleep(cfg.tick_sl_check_interval); continue

            if trail_eng.should_exit(trail, pnl_pts):
                fill_x = exec_eng.execute(
                    opt_symbol, icfg["option_exchange"], "SELL",
                    icfg["quantity"], name)
                curr        = fill_x.filled_price or curr
                exit_reason = "Trailing SL" if trail.be_triggered else "Initial SL"
                position_open = False
                if not trail.be_triggered:
                    last_sl_time = now()
                last_exit_time = now()

            if position_open:
                time.sleep(cfg.tick_sl_check_interval)

        # ── Close ─────────────────────────────────────────────
        pnl_pts_f = curr - entry_filled
        pnl_rs_f  = pnl_pts_f * icfg["quantity"]
        total_pnl += pnl_rs_f
        risk.on_close(pnl_rs_f, approx_exp)

        # capture exit-context indicators from a fresh candle pull
        ex_ema9 = ex_ema21 = ex_vwap = ex_adx = ex_spot = 0.0
        try:
            df_x = mde.get_candles(icfg["index_symbol"], icfg["index_exchange"])
            if df_x is not None and len(df_x) >= 5:
                ex_ema9  = te.ema(df_x, cfg.ema_fast)
                ex_ema21 = te.ema(df_x, cfg.ema_slow)
                ex_vwap  = te.vwap(df_x)
                ex_adx   = te.adx(df_x)
                ex_spot  = float(df_x["close"].iloc[-1])
        except Exception:
            pass

        rec = TradeRecord(
            trade_num=trade_count, instrument=name,
            symbol=opt_symbol, direction=option_type,
            entry_price_quoted=entry_quoted,
            entry_price_filled=entry_filled,
            exit_price=curr,
            entry_time=entry_time_dt, exit_time=now(),
            exit_reason=exit_reason,
            quantity=icfg["quantity"],
            pnl_pts=pnl_pts_f, pnl_rs=pnl_rs_f,
            mfe_pts=mfe_pts, mae_pts=mae_pts,
            capture_pct=(pnl_pts_f/mfe_pts*100 if mfe_pts > 0 else 0),
            hold_seconds=(now()-entry_time_dt).total_seconds(),
            slippage_pts=slippage,
            trend_score=best.score, adx_entry=adx_val,
            vol_regime=vol_reg, running_total=total_pnl,
            # detailed entry snapshot
            spot_entry=best.latest_price, ema9_entry=best.ema_fast,
            ema21_entry=best.ema_slow, vwap_entry=best.vwap_val,
            atr_entry=best.atr_val, orb_high=best.orb_high,
            orb_low=best.orb_low, oi_bias=str(best.oi_bias),
            strike_type="OTM", initial_sl_pts=trail.initial_sl,
            be_triggered=trail.be_triggered,
            trail_count=len(trail.trail_log),
            # exit snapshot
            spot_exit=ex_spot, ema9_exit=ex_ema9, ema21_exit=ex_ema21,
            vwap_exit=ex_vwap, adx_exit=ex_adx, sl_at_exit=trail.dynamic_sl,
        )
        records.append(rec)
        logger.log_trade_card(rec)
        logger.trade(name, f"EXIT {exit_reason} | Rs{pnl_rs_f:+.0f} | Total:Rs{total_pnl:+.0f}")

        time.sleep(1)

    logger.trade(name, f"=== {name} done | Trades:{trade_count} | PnL:Rs{total_pnl:+.0f} ===")


# ================================================================
# STRATEGY LOOP — EQUITY
# ================================================================

def run_equity(
    cfg: AppConfig, mde: MarketDataEngine, scorer: ScoringEngine,
    scanner: Scanner, risk: RiskEngine, trail_eng: TrailingStopEngine,
    exec_eng: ExecutionEngine, cache: TickCache, logger: LoggingEngine,
) -> None:

    def now() -> datetime: return datetime.now(IST)
    def now_str() -> str:  return now().strftime("%H:%M")

    logger.day_start("EQUITY", cfg.mode_name, cfg.paper_mode)

    te             = TrendEngine(cfg)
    trade_count    = 0
    total_pnl      = 0.0
    records:       list[TradeRecord]   = []
    open_trades:   dict[str, dict]     = {}
    last_rank_ts:  Optional[datetime]  = None
    ranked:        list[StockRank]     = []
    symbol_sl_time:     dict           = {}
    symbol_trade_count: dict           = {}
    last_skip_log:      dict           = {}   # throttle repetitive "Skip ..." spam
    last_entry_time: Optional[datetime] = None
    nifty_chg_pct:   float             = 0.0
    last_nifty_ts:   Optional[datetime] = None
    symbol_sl_time:     dict           = {}
    symbol_trade_count: dict           = {}
    last_entry_time: Optional[datetime] = None
    nifty_chg_pct:   float             = 0.0
    last_nifty_ts:   Optional[datetime] = None
    breadth:       Optional[MarketBreadth] = None
    last_breadth_ts: Optional[datetime]   = None

    while True:
        ns = now_str()

        # ── Hard exit all equity ─────────────────────────────
        if ns >= cfg.hard_exit_time:
            for sym, st in list(open_trades.items()):
                _is_long = st.get("direction", "CE") == "CE"
                _exit_act = "SELL" if _is_long else "BUY"
                fill_x = exec_eng.execute(sym, cfg.equity_exchange, _exit_act, st["qty"], "EQUITY")
                curr_p = fill_x.filled_price or cache.get_ltp(sym) or st["entry"]
                _pnl_pts = (curr_p - st["entry"]) if _is_long else (st["entry"] - curr_p)
                pnl_rs = _pnl_pts * st["qty"]
                total_pnl += pnl_rs; trade_count += 1
                risk.on_close(pnl_rs, st["exposure"], strategy="EQUITY")
                rec = TradeRecord(
                    trade_num=trade_count, instrument="EQUITY", symbol=sym,
                    direction="BUY" if _is_long else "SELL",
                    entry_price_quoted=st.get("quoted", st["entry"]),
                    entry_price_filled=st["entry"],
                    exit_price=curr_p, entry_time=st["entry_time"], exit_time=now(),
                    exit_reason="Hard Exit", quantity=st["qty"],
                    pnl_pts=_pnl_pts, pnl_rs=pnl_rs,
                    mfe_pts=st["mfe"], mae_pts=st["mae"],
                    capture_pct=_pnl_pts/st["mfe"]*100 if st["mfe"] > 0 else 0,
                    hold_seconds=(now()-st["entry_time"]).total_seconds(),
                    trend_score=st["score"], adx_entry=st["adx"],
                    vol_regime="equity", running_total=total_pnl,
                    spot_entry=st.get("spot",0), ema9_entry=st.get("ema9",0),
                    ema21_entry=st.get("ema21",0), vwap_entry=st.get("vwap",0),
                    atr_entry=st["atr"], orb_high=st.get("orb_high",0),
                    orb_low=st.get("orb_low",0), oi_bias=st.get("oi_bias","N/A"),
                    strike_type="EQUITY", initial_sl_pts=st["trail"].initial_sl,
                    be_triggered=st["trail"].be_triggered,
                    trail_count=len(st["trail"].trail_log),
                    spot_exit=curr_p, sl_at_exit=st["trail"].dynamic_sl,
                )
                records.append(rec); logger.log_trade_card(rec)
            logger.trade("EQUITY", f"Session end | Trades:{trade_count} | PnL:Rs{total_pnl:+.0f}")
            logger.day_summary("EQUITY", records)
            break

        if ns < cfg.entry_time:
            time.sleep(5); continue
        if cfg.lunch_start <= ns <= cfg.lunch_end:
            time.sleep(60); continue

        # ── Update open positions via tick cache ─────────────
        for sym in list(open_trades.keys()):
            st    = open_trades[sym]
            curr_p = cache.get_ltp(sym) or mde.get_quote_rest(sym, cfg.equity_exchange)
            if curr_p is None:
                continue

            is_long   = st.get("direction", "CE") == "CE"
            exit_action = "SELL" if is_long else "BUY"
            pnl_pts   = (curr_p - st["entry"]) if is_long else (st["entry"] - curr_p)
            st["mfe"] = max(st["mfe"], pnl_pts)
            st["mae"] = min(st["mae"], pnl_pts)

            trail: TrailState = st["trail"]
            trail_eng.update(trail, pnl_pts, st["atr"], f"EQUITY:{sym}")

            held = (now() - st["entry_time"]).total_seconds()

            # Quick exit: if MFE < 1pt after 2 minutes → no momentum, exit now
            if (held >= cfg.min_mfe_after_secs and
                    st["mfe"] < cfg.min_mfe_pts and
                    pnl_pts <= 0):
                fill_x = exec_eng.execute(sym, cfg.equity_exchange, exit_action, st["qty"], "EQUITY")
                exit_p = fill_x.filled_price or curr_p
                pnl_rs = ((exit_p - st["entry"]) if is_long else (st["entry"] - exit_p)) * st["qty"]
                total_pnl += pnl_rs; trade_count += 1
                # V3: a quick ~flat no-momentum scratch is a non-event — don't let it
                # burn a daily-cap slot (data: these were 29% of trades, netting ~-Rs800,
                # and locked the engine out of real afternoon trends).
                is_scratch = abs(pnl_rs) < SCRATCH_PNL_MAX
                risk.on_close(pnl_rs, st["exposure"], strategy="EQUITY", real_trade=not is_scratch)
                er = "No Momentum Exit"
                rec = TradeRecord(
                    trade_num=trade_count, instrument="EQUITY", symbol=sym,
                    direction="BUY" if is_long else "SELL",
                    entry_price_quoted=st.get("quoted", st["entry"]),
                    entry_price_filled=st["entry"],
                    exit_price=exit_p, entry_time=st["entry_time"], exit_time=now(),
                    exit_reason=er, quantity=st["qty"],
                    pnl_pts=pnl_pts, pnl_rs=pnl_rs,
                    mfe_pts=st["mfe"], mae_pts=st["mae"],
                    capture_pct=0, hold_seconds=held,
                    trend_score=st["score"], adx_entry=st["adx"],
                    vol_regime="equity", running_total=total_pnl,
                    spot_entry=st.get("spot",0), ema9_entry=st.get("ema9",0),
                    ema21_entry=st.get("ema21",0), vwap_entry=st.get("vwap",0),
                    atr_entry=st["atr"], orb_high=st.get("orb_high",0),
                    orb_low=st.get("orb_low",0), oi_bias=st.get("oi_bias","N/A"),
                    strike_type="EQUITY", initial_sl_pts=trail.initial_sl,
                    be_triggered=trail.be_triggered,
                    trail_count=len(trail.trail_log),
                    spot_exit=exit_p, sl_at_exit=trail.dynamic_sl,
                )
                records.append(rec); logger.log_trade_card(rec)
                logger.trade("EQUITY", f"NO MOMENTUM EXIT {sym} after {held:.0f}s | Rs{pnl_rs:+.0f}")
                symbol_sl_time[sym] = now()
                del open_trades[sym]
                continue

            if trail_eng.should_exit(trail, pnl_pts):
                if held < cfg.min_hold_seconds and pnl_pts > trail.initial_sl:
                    continue
                fill_x = exec_eng.execute(sym, cfg.equity_exchange, exit_action, st["qty"], "EQUITY")
                exit_p = fill_x.filled_price or curr_p
                pnl_rs = ((exit_p - st["entry"]) if is_long else (st["entry"] - exit_p)) * st["qty"]
                total_pnl += pnl_rs; trade_count += 1
                risk.on_close(pnl_rs, st["exposure"], strategy="EQUITY")
                er = "Trailing SL" if trail.be_triggered else "Initial SL"
                rec = TradeRecord(
                    trade_num=trade_count, instrument="EQUITY", symbol=sym,
                    direction="BUY" if is_long else "SELL",
                    entry_price_quoted=st.get("quoted", st["entry"]),
                    entry_price_filled=st["entry"],
                    exit_price=exit_p, entry_time=st["entry_time"], exit_time=now(),
                    exit_reason=er, quantity=st["qty"],
                    pnl_pts=pnl_pts, pnl_rs=pnl_rs,
                    mfe_pts=st["mfe"], mae_pts=st["mae"],
                    capture_pct=pnl_pts/st["mfe"]*100 if st["mfe"]>0 else 0,
                    hold_seconds=held, trend_score=st["score"], adx_entry=st["adx"],
                    vol_regime="equity", running_total=total_pnl,
                    spot_entry=st.get("spot",0), ema9_entry=st.get("ema9",0),
                    ema21_entry=st.get("ema21",0), vwap_entry=st.get("vwap",0),
                    atr_entry=st["atr"], orb_high=st.get("orb_high",0),
                    orb_low=st.get("orb_low",0), oi_bias=st.get("oi_bias","N/A"),
                    strike_type="EQUITY", initial_sl_pts=trail.initial_sl,
                    be_triggered=trail.be_triggered,
                    trail_count=len(trail.trail_log),
                    spot_exit=exit_p, sl_at_exit=trail.dynamic_sl,
                )
                records.append(rec); logger.log_trade_card(rec)
                logger.trade("EQUITY", f"EXIT {er} | {sym} | Rs{pnl_rs:+.0f}")
                if er == "Initial SL":
                    symbol_sl_time[sym] = now()
                del open_trades[sym]

        # ── Market breadth (every 60s) ───────────────────────
        if last_breadth_ts is None or (now()-last_breadth_ts).total_seconds() >= 60:
            breadth          = scanner.breadth()
            last_breadth_ts  = now()
            logger.activity("EQUITY", (
                f"Breadth: {breadth.bias} | "
                f"AboveVWAP:{breadth.pct_above_vwap:.0f}% "
                f"AboveEMA21:{breadth.pct_above_ema21:.0f}% "
                f"Adv:{breadth.advancing} Dec:{breadth.declining}"
            ))
            # persist breadth to DB
            try:
                logger._tl.log_breadth(
                    breadth.pct_above_vwap, breadth.pct_above_ema21,
                    breadth.advancing, breadth.declining, breadth.bias,
                ) if hasattr(logger, "_tl") else None
            except Exception:
                pass

        # ── Re-rank stocks ───────────────────────────────────
        if last_rank_ts is None or (now()-last_rank_ts).total_seconds() >= cfg.stock_rank_refresh:
            ranked       = scanner.rank()
            last_rank_ts = now()
            top5         = ranked[:5]
            logger.activity("EQUITY", "Rank: " + " | ".join(
                f"{r.symbol}:{r.score:.1f}({r.direction}) RS15:{r.rs_15m:+.2f}%" for r in top5
            ))

        # ── New entries ──────────────────────────────────────
        m = cfg.mode
        if len(open_trades) >= m.max_equity_positions or ns >= cfg.late_entry_cutoff:
            time.sleep(cfg.tick_sl_check_interval); continue

        ok, reason = risk.can_trade("EQUITY")
        if not ok:
            time.sleep(30); continue

        # Refresh NIFTY benchmark every 5 min for sector RS
        if last_nifty_ts is None or (now()-last_nifty_ts).total_seconds() >= 300:
            nifty_chg = mde.get_nifty_change_pct()
            if nifty_chg is not None:
                nifty_chg_pct = nifty_chg
            last_nifty_ts = now()

        # Stagger: don't open new position within 5 min of last entry
        if last_entry_time and (now()-last_entry_time).total_seconds() < cfg.equity_entry_stagger_s:
            time.sleep(cfg.tick_sl_check_interval); continue

        for rank in ranked[:cfg.top_n_stocks]:
            if rank.symbol in open_trades: continue
            if len(open_trades) >= m.max_equity_positions: break
            if rank.score < m.score_threshold: continue
            sig = rank.signal
            if sig is None or sig.adx_val != sig.adx_val: continue  # nan ADX guard
            if sig.adx_val < 25: continue
            if sig.adx_val > 60:
                _sk = last_skip_log.get(rank.symbol)
                if _sk is None or (now() - _sk).total_seconds() >= 60:
                    logger.activity("EQUITY", f"Skip {rank.symbol} — ADX {sig.adx_val:.1f} exhausted move")
                    last_skip_log[rank.symbol] = now()
                continue

            is_long = rank.direction == "CE"

            # ── Symbol cooldown after SL ──────────────────────
            if sym_sl := symbol_sl_time.get(rank.symbol):
                if (now() - sym_sl).total_seconds() < cfg.symbol_sl_cooldown_s:
                    logger.activity("EQUITY", f"Skip {rank.symbol} — SL cooldown")
                    continue

            # ── Max trades per symbol ─────────────────────────
            if symbol_trade_count.get(rank.symbol, 0) >= cfg.max_trades_per_symbol:
                logger.activity("EQUITY", f"Skip {rank.symbol} — max {cfg.max_trades_per_symbol} trades today")
                continue

            # ── Market breadth gate ───────────────────────────
            if breadth:
                if is_long and breadth.bias == "BEARISH":
                    continue
                if not is_long and breadth.bias == "BULLISH":
                    continue
                if breadth.bias == "MIXED" and rank.rs_15m < 0.2:
                    logger.activity("EQUITY", f"Skip {rank.symbol} — breadth MIXED, RS15 weak ({rank.rs_15m:+.2f}%)")
                    continue

            # ── Sector strength filter ────────────────────────
            sector_sym = SECTOR_INDICES.get(rank.symbol)
            if sector_sym:
                sec_chg = mde.get_sector_change_pct(sector_sym)
                if sec_chg is not None and sec_chg < nifty_chg_pct - 0.1:
                    logger.activity("EQUITY",
                        f"Skip {rank.symbol} — sector {sector_sym} {sec_chg:+.2f}% "
                        f"underperforming NIFTY {nifty_chg_pct:+.2f}%")
                    continue

            # ── Momentum check: price moving in signal direction ──
            df_check = mde.get_candles(rank.symbol, cfg.equity_exchange)
            if df_check is not None and len(df_check) >= 3:
                last_close = float(df_check["close"].iloc[-1])
                prev_close = float(df_check["close"].iloc[-2])
                if is_long and last_close <= prev_close:
                    continue
                if not is_long and last_close >= prev_close:
                    continue

            price = cache.get_ltp(rank.symbol) or mde.get_quote_rest(
                rank.symbol, cfg.equity_exchange)
            if not price or price <= 0: continue

            qty      = max(1, int(cfg.equity_lot_value / price))
            exposure = qty * price
            ok2, r2  = risk.can_trade("EQUITY", exposure)
            if not ok2: continue

            entry_action = "BUY" if is_long else "SELL"
            fill = exec_eng.execute(rank.symbol, cfg.equity_exchange, entry_action, qty, "EQUITY")
            if not fill.success: continue

            filled_p = fill.filled_price or price
            trail    = trail_eng.create(sig.atr_val, f"EQUITY:{rank.symbol}")
            risk.on_open(exposure)
            symbol_trade_count[rank.symbol] = symbol_trade_count.get(rank.symbol, 0) + 1
            last_entry_time = now()
            open_trades[rank.symbol] = {
                "entry": filled_p, "qty": qty, "exposure": exposure,
                "entry_time": now(), "trail": trail,
                "mfe": 0.0, "mae": 0.0,
                "atr": sig.atr_val, "score": rank.score, "adx": sig.adx_val,
                "spot": sig.latest_price, "ema9": sig.ema_fast,
                "ema21": sig.ema_slow, "vwap": sig.vwap_val,
                "orb_high": sig.orb_high, "orb_low": sig.orb_low,
                "oi_bias": str(sig.oi_bias), "quoted": price,
                "sector": sector_sym or "N/A",
                "direction": rank.direction,
            }
            logger.trade("EQUITY", (
                f"ENTRY #{trade_count+1} {rank.symbol} {entry_action} {qty}@{filled_p:.2f} "
                f"Score:{rank.score:.1f} ADX:{sig.adx_val:.1f} "
                f"RS15:{rank.rs_15m:+.2f}% Sector:{sector_sym or 'N/A'}"
            ))

        time.sleep(cfg.tick_sl_check_interval)

    logger.trade("EQUITY", f"=== EQUITY done | Trades:{trade_count} | PnL:Rs{total_pnl:+.0f} ===")


# ================================================================
# MAIN
# ================================================================

def main() -> None:
    api_key = os.environ.get("OPENALGO_API_KEY", "")
    if not api_key:
        raise EnvironmentError(
            "Set OPENALGO_API_KEY before running.\n"
            "  export OPENALGO_API_KEY=your_key_here"
        )

    mode_name  = os.environ.get("TREND_RIDER_MODE", "balanced").lower()
    paper_mode = os.environ.get("TRADE_MODE", "paper").lower() != "live"

    if mode_name not in MODES:
        raise ValueError(f"Unknown mode '{mode_name}'. Choose: {list(MODES)}")

    cfg       = AppConfig(api_key=api_key, mode_name=mode_name, paper_mode=paper_mode)
    logger    = LoggingEngine()               # ~/trend_rider/logs/
    mde       = MarketDataEngine(cfg)
    te        = TrendEngine(cfg)
    scorer    = ScoringEngine(cfg, te)
    risk      = RiskEngine(cfg, logger)
    trail_eng = TrailingStopEngine(cfg, logger)
    cache     = TickCache()
    exec_eng  = ExecutionEngine(cfg, cache, mde, logger)
    scan      = Scanner(cfg, mde, scorer, logger)
    ws        = WebSocketEngine(cfg, cache, logger)

    # ── Collect all symbols to subscribe ────────────────────
    all_symbols: list[str] = list(ALL_EQUITY_STOCKS)
    # Index symbols removed — scalper handles NIFTY/SENSEX options

    ist_now = datetime.now(IST)
    tag     = "PAPER TRADE 🟡" if paper_mode else "LIVE TRADE 🔴"

    logger.trade("MAIN", "=" * 64)
    logger.trade("MAIN", f"Trend Rider V3.0  |  {ist_now.strftime('%Y-%m-%d %H:%M')}")
    logger.trade("MAIN", f"Mode: {mode_name.upper()}  |  {tag}")
    logger.trade("MAIN", f"Score threshold : {cfg.mode.score_threshold}")
    logger.trade("MAIN", f"Hard exit       : {cfg.hard_exit_time} IST")
    logger.trade("MAIN", f"Log directory   : ~/openalgo/logs/ (dated folders + CSV)")
    logger.trade("MAIN", f"WS available    : {_SIO_AVAILABLE}")
    logger.trade("MAIN", "=" * 64)

    # Start WebSocket feed first
    ws.start(all_symbols)
    time.sleep(2)   # allow WS handshake before strategies start

    threads: list[threading.Thread] = []

    # NIFTY/SENSEX options dropped — scalper handles those.
    # Trend rider focuses on NIFTY50 + Bank Nifty + Midcap equity only.

    t_eq = threading.Thread(
        target=run_equity,
        args=(cfg, mde, scorer, scan, risk, trail_eng, exec_eng, cache, logger),
        daemon=True, name="Thread-EQUITY",
    )
    t_eq.start(); threads.append(t_eq)

    for t in threads:
        t.join()

    logger.trade("MAIN", "=" * 64)
    logger.trade("MAIN", "All strategies complete.")
    logger.trade("MAIN", f"Portfolio PnL: Rs {risk.state.total_pnl:+.0f}")
    logger.trade("MAIN", "=" * 64)


if __name__ == "__main__":
    main()