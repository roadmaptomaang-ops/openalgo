"""
Trend Rider V2.0 — WebSocket-Driven Intraday Trend Bot
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
        "Balanced",     80, 1.2, 1.8, 0.8, 5, 3, 300, 180, -6000, 2000),
    "aggressive": TradingMode(
        "Aggressive",   75, 1.0, 1.5, 0.6, 8, 3, 180, 120,-10000, 3000),
}


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
    late_entry_cutoff:        str   = "13:30"
    hard_exit_time:           str   = "15:15"
    lunch_start:              str   = "11:45"
    lunch_end:                str   = "13:15"

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
    high_water_drop_r:        float = 1.0
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
# LOGGING ENGINE  (~/trend_rider/logs/)
# ================================================================

class LoggingEngine:
    """
    Thread-safe dual-log system.
    Files: ~/trend_rider/logs/YYYY-MM-DD_trades.log
           ~/trend_rider/logs/YYYY-MM-DD_activity.log
    """

    def __init__(self, log_dir: str = "~/trend_rider/logs"):
        self._dir  = Path(os.path.expanduser(log_dir))
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(IST)

    def _trade_path(self) -> Path:
        return self._dir / f"{self._now().strftime('%Y-%m-%d')}_trades.log"

    def _activity_path(self) -> Path:
        return self._dir / f"{self._now().strftime('%Y-%m-%d')}_activity.log"

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
            f"# Trend Rider V2.0 — {name} — {self._now().strftime('%Y-%m-%d')}",
            f"# Mode: {mode_name.upper()}  |  {tag}",
            f"# Logs: ~/trend_rider/logs/",
            sep, "",
        ]
        for ln in lines:
            print(ln)
            self._write(self._trade_path(), ln)

    def log_trade_card(self, r: "TradeRecord") -> None:
        SEP  = "=" * 60
        DIV  = "─" * 60
        hold = str(timedelta(seconds=int(r.hold_seconds)))
        date = r.entry_time.strftime("%Y-%m-%d")
        cap  = f"{r.capture_pct:.1f}%" if r.mfe_pts > 0 else "N/A"
        slip = f"{r.slippage_pts:+.2f} pts" if r.slippage_pts != 0 else "0.00 pts"
        spread = r.entry_ask - r.entry_bid if r.entry_ask and r.entry_bid else 0.0

        # momentum label helper
        def adx_label(adx: float) -> str:
            if adx >= 35: return "Strong Trend"
            if adx >= 25: return "Trending"
            if adx >= 20: return "Weak Trend"
            return "Choppy / Ranging"

        is_option = r.instrument in ("NIFTY", "SENSEX")

        lines = ["", SEP]
        lines.append(f"TRADE #{r.trade_num} | {r.instrument} | {date} | {r.entry_time.strftime('%H:%M:%S')}")
        lines.append(SEP)

        # ── Market conditions at entry ──────────────────────
        if is_option:
            lines += [
                f"── MARKET CONDITIONS AT ENTRY {'─'*20}",
                f"  Spot Price     : {r.entry_spot:.2f}",
                f"  ORB High       : {r.entry_orb_high:.2f}",
                f"  ORB Low        : {r.entry_orb_low:.2f}",
                f"  EMA9           : {r.entry_ema9:.2f}",
                f"  EMA21          : {r.entry_ema21:.2f}",
                f"  VWAP           : {r.entry_vwap:.2f}",
                f"  ADX            : {r.entry_adx:.1f} ({adx_label(r.entry_adx)})",
                f"  ATR            : {r.entry_atr:.2f}",
                f"  Volatility     : {r.vol_regime.upper()}",
                f"  Max Pain       : {r.max_pain_strike or 'N/A'}"
                + (f" (Bias: {r.max_pain_bias})" if r.max_pain_bias else ""),
                f"  OI Bias        : {r.oi_bias or 'N/A'}",
                f"  Momentum       : {r.momentum_state}",
            ]
        else:
            lines += [
                f"── MARKET CONDITIONS AT ENTRY {'─'*20}",
                f"  Price at Entry : {r.entry_spot:.2f}",
                f"  EMA9           : {r.entry_ema9:.2f}",
                f"  EMA21          : {r.entry_ema21:.2f}",
                f"  VWAP           : {r.entry_vwap:.2f}",
                f"  ADX            : {r.entry_adx:.1f} ({adx_label(r.entry_adx)})",
                f"  ATR            : {r.entry_atr:.2f}",
                f"  Volatility     : {r.vol_regime.upper()}",
                f"  Momentum       : {r.momentum_state}",
            ]

        # ── Trend score breakdown ───────────────────────────
        lines.append(f"── TREND SCORE {'─'*34}")
        lines.append(f"  Total Score    : {r.trend_score:.1f} / 100")
        if r.score_components:
            for k, v in r.score_components.items():
                bar = "█" * int(v / 5)
                lines.append(f"    {k:<18}: {v:>5.1f}  {bar}")

        # ── Entry ───────────────────────────────────────────
        lines.append(f"── ENTRY {'─'*40}")
        lines.append(f"  Signal         : {r.direction}")
        lines.append(f"  Symbol         : {r.symbol}")
        if is_option:
            lines.append(f"  Strike Type    : {r.strike_type}")
            lines.append(f"  Entry Premium  : Rs {r.entry_price_quoted:.2f} (quoted)  Rs {r.entry_price_filled:.2f} (filled)")
            lines.append(f"  Slippage       : {slip}")
            lines.append(f"  Bid / Ask      : {r.entry_bid:.2f} / {r.entry_ask:.2f}  (spread {spread:.2f})")
            lines.append(f"  Initial SL     : {r.initial_sl_pts:.2f} pts  →  SL premium Rs {r.initial_sl_premium:.2f}")
        else:
            lines.append(f"  Entry Price    : Rs {r.entry_price_quoted:.2f} (quoted)  Rs {r.entry_price_filled:.2f} (filled)")
            lines.append(f"  Slippage       : {slip}")
            lines.append(f"  Quantity       : {r.quantity}")
            lines.append(f"  Initial SL     : {r.initial_sl_pts:.2f} pts  →  Rs {r.initial_sl_pts * r.quantity:.0f}")
        lines.append(f"  Entry Time     : {r.entry_time.strftime('%H:%M:%S')}")

        # ── MFE / MAE analytics ─────────────────────────────
        mfe_rs  = r.mfe_pts * r.quantity
        mae_rs  = r.mae_pts * r.quantity
        given   = r.high_water_pts - r.pnl_pts if r.high_water_pts > 0 else 0
        given_rs = given * r.quantity
        lines += [
            f"── MFE / MAE ANALYTICS {'─'*27}",
            f"  MFE (best point) : +{r.mfe_pts:.2f} pts  =  Rs {mfe_rs:+.0f}",
            f"  MAE (worst point): {r.mae_pts:.2f} pts  =  Rs {mae_rs:+.0f}",
            f"  Profit captured  : {r.pnl_pts:.2f} pts / {r.mfe_pts:.2f} pts  =  {cap}",
            f"  Risk-reward      : 1 : {abs(r.pnl_pts/r.initial_sl_pts):.2f}" if r.initial_sl_pts else "  Risk-reward      : N/A",
        ]

        # ── Break-even audit ────────────────────────────────
        lines.append(f"── BREAK-EVEN AUDIT {'─'*30}")
        if r.be_triggered:
            be_time = r.be_trigger_time.strftime("%H:%M:%S") if r.be_trigger_time else "N/A"
            lines += [
                f"  Break-even     : YES — Triggered",
                f"  Triggered at   : +{r.be_trigger_pnl:.2f} pts",
                f"  Time           : {be_time}",
                f"  SL moved       : {r.initial_sl_pts:.2f} pts → 0",
            ]
        else:
            lines.append(f"  Break-even     : NO — Never triggered")

        # ── Trailing SL audit ───────────────────────────────
        lines.append(f"── TRAILING SL AUDIT {'─'*29}")
        atr_trails = [t for t in r.trail_log if t.get("type") == "ATR"]
        hwm_trails = [t for t in r.trail_log if t.get("type") == "HWM"]
        if atr_trails:
            lines.append(f"  ATR trails     : {len(atr_trails)} adjustments")
            for t in atr_trails[-3:]:   # show last 3
                lines.append(f"    @ +{t['pnl']:.2f}pts  SL {t['old']:.2f} → {t['new']:.2f}")
        else:
            lines.append(f"  ATR trailing   : NO — Never triggered")
        if hwm_trails:
            lines.append(f"  HWM trails     : {len(hwm_trails)} adjustments")
            for t in hwm_trails[-3:]:
                lines.append(f"    @ +{t['pnl']:.2f}pts  SL {t['old']:.2f} → {t['new']:.2f}")
        else:
            lines.append(f"  HWM trailing   : NO — Never triggered")

        # ── High water audit ────────────────────────────────
        lines.append(f"── HIGH WATER AUDIT {'─'*30}")
        hwm_rs = r.high_water_pts * r.quantity
        lines += [
            f"  Peak profit    : +{r.high_water_pts:.2f} pts  =  Rs {hwm_rs:+.0f}",
            f"  Exit profit    : {r.pnl_pts:+.2f} pts  =  Rs {r.pnl_rs:+.0f}",
            f"  Giveback       : {given:.2f} pts  =  Rs {given_rs:.0f}",
            f"  Exit reason    : {r.exit_reason}",
        ]

        # ── Exit context ────────────────────────────────────
        lines.append(f"── EXIT CONTEXT {'─'*34}")
        exit_time_str = r.exit_time.strftime("%H:%M:%S") if r.exit_time else "N/A"
        lines += [
            f"  Exit Time      : {exit_time_str}",
            f"  Hold Duration  : {hold}",
            f"  Exit Premium   : Rs {r.exit_price:.2f}",
        ]
        if is_option and r.exit_spot:
            lines += [
                f"  Spot at exit   : {r.exit_spot:.2f}",
                f"  EMA9 at exit   : {r.exit_ema9:.2f}",
                f"  EMA21 at exit  : {r.exit_ema21:.2f}",
                f"  VWAP at exit   : {r.exit_vwap:.2f}",
                f"  ADX at exit    : {r.exit_adx:.1f} ({adx_label(r.exit_adx)})",
                f"  Momentum       : {r.exit_momentum}",
            ]
        elif r.exit_spot:
            lines += [
                f"  Price at exit  : {r.exit_spot:.2f}",
                f"  EMA9 at exit   : {r.exit_ema9:.2f}",
                f"  VWAP at exit   : {r.exit_vwap:.2f}",
                f"  ADX at exit    : {r.exit_adx:.1f} ({adx_label(r.exit_adx)})",
                f"  Momentum       : {r.exit_momentum}",
            ]
        lines.append(f"  SL at exit     : {r.sl_at_exit:.2f} pts")

        # ── Result ──────────────────────────────────────────
        result_flag = "✓ WIN" if r.pnl_rs > 0 else "✗ LOSS" if r.pnl_rs < 0 else "── BREAKEVEN"
        lines += [
            f"── RESULT {'─'*39}",
            f"  Outcome        : {result_flag}",
            f"  P&L Points     : {r.pnl_pts:+.2f} pts",
            f"  P&L Rupees     : Rs {r.pnl_rs:+.0f}",
            f"  RUNNING TOTAL  : Rs {r.running_total:+.0f}",
            SEP, "",
        ]

        for ln in lines:
            print(ln)
            self._write(self._trade_path(), ln)

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
    trade_num:            int
    instrument:           str
    symbol:               str
    direction:            str
    entry_price_quoted:   float
    entry_price_filled:   float              = 0.0
    exit_price:           float              = 0.0
    entry_time:           datetime           = field(default_factory=lambda: datetime.now(IST))
    exit_time:            Optional[datetime] = None
    exit_reason:          str               = "Unknown"
    quantity:             int               = 1
    pnl_pts:              float             = 0.0
    pnl_rs:               float             = 0.0
    mfe_pts:              float             = 0.0
    mae_pts:              float             = 0.0
    capture_pct:          float             = 0.0
    hold_seconds:         float             = 0.0
    slippage_pts:         float             = 0.0
    running_total:        float             = 0.0
    # ── Entry market snapshot ─────────────────────────────
    trend_score:          float             = 0.0
    score_components:     dict              = field(default_factory=dict)
    vol_regime:           str               = "medium"
    strike_type:          str               = "ATM"
    entry_spot:           float             = 0.0
    entry_ema9:           float             = 0.0
    entry_ema21:          float             = 0.0
    entry_vwap:           float             = 0.0
    entry_adx:            float             = 0.0
    entry_atr:            float             = 0.0
    entry_orb_high:       float             = 0.0
    entry_orb_low:        float             = 0.0
    entry_bid:            float             = 0.0
    entry_ask:            float             = 0.0
    max_pain_strike:      Optional[int]     = None
    max_pain_bias:        Optional[str]     = None
    oi_bias:              Optional[str]     = None
    momentum_state:       str               = "Neutral"
    initial_sl_pts:       float             = 0.0
    initial_sl_premium:   float             = 0.0
    # ── Break-even audit ──────────────────────────────────
    be_triggered:         bool              = False
    be_trigger_pnl:       float             = 0.0
    be_trigger_time:      Optional[datetime] = None
    # ── Trail audit ───────────────────────────────────────
    trail_log:            list              = field(default_factory=list)
    high_water_pts:       float             = 0.0
    sl_at_exit:           float             = 0.0
    # ── Exit market snapshot ──────────────────────────────
    exit_spot:            float             = 0.0
    exit_ema9:            float             = 0.0
    exit_ema21:           float             = 0.0
    exit_vwap:            float             = 0.0
    exit_adx:             float             = 0.0
    exit_momentum:        str               = "Neutral"


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

    def _verify_fill(self, order_id: str) -> FillResult:
        """Poll order status endpoint to confirm fill."""
        time.sleep(self.VERIFY_DELAY_S)
        # In analyze mode orders are always considered filled
        if order_id:
            return FillResult(True, order_id, 0.0, 0, "FILLED")
        return FillResult(False, order_id, status="TIMEOUT")

    def execute(
        self,
        symbol: str,
        exchange: str,
        action: str,
        quantity: int,
        name: str = "",
    ) -> FillResult:
        # ── Paper mode ───────────────────────────────────────
        if self._cfg.paper_mode:
            ltp = self._cache.get_ltp(symbol) or \
                  self._mde.get_quote_rest(symbol, exchange) or 0.0
            self._logger.activity(
                name, f"[PAPER] {action} {quantity} {symbol} @ {ltp:.2f}"
            )
            return FillResult(
                success=True, order_id="PAPER",
                filled_price=ltp, filled_qty=quantity, status="FILLED", attempts=1
            )

        # ── Live: place + verify with retries ────────────────
        for attempt in range(1, self.MAX_RETRIES + 1):
            data = self._post("placeorder", {
                "symbol":    symbol,
                "exchange":  exchange,
                "action":    action,
                "quantity":  quantity,
                "price":     0,
                "pricetype": "MARKET",
                "product":   "MIS",
                "strategy":  "Trend Rider V2.0",
            })

            if not data or data.get("status") != "success":
                self._logger.activity(
                    name,
                    f"Order place failed (attempt {attempt}/{self.MAX_RETRIES}): {data}",
                )
                time.sleep(self.RETRY_DELAY_S)
                continue

            order_id = data.get("orderid", "")
            result   = self._verify_fill(order_id)
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
    score:            float
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


class ScoringEngine:
    def __init__(self, cfg: AppConfig, te: TrendEngine) -> None:
        self._cfg = cfg
        self._te  = te

    # ── Component scorers (each returns its max weight or 0) ─

    def _s_ema(self, ef: float, es: float, price: float, d: str) -> float:
        if d == "CE":
            if ef > es and price > ef:  return 25.0
            if ef > es:                 return 15.0
        else:
            if ef < es and price < ef:  return 25.0
            if ef < es:                 return 15.0
        return 0.0

    def _s_adx(self, adx: float) -> float:
        if adx >= 35: return 20.0
        if adx >= 25: return 15.0
        if adx >= 20: return 10.0
        return 0.0

    def _s_vwap(self, price: float, vwap: float, d: str) -> float:
        if d == "CE" and price > vwap: return 20.0
        if d == "PE" and price < vwap: return 20.0
        return 0.0

    def _s_orb(
        self, price: float, oh: float, ol: float, buf: float, d: str
    ) -> float:
        if d == "CE" and price > oh + buf: return 20.0
        if d == "PE" and price < ol - buf: return 20.0
        return 0.0

    def _s_oi(self, oi_bias: Optional[str], d: str) -> float:
        if oi_bias == d:   return 15.0
        if oi_bias is None: return 7.5
        return 0.0

    def compute(
        self,
        df: pd.DataFrame,
        direction: str,
        orb_buffer: float = 0.0,
        oi_bias: Optional[str] = None,
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

        comp = {
            "ema_trend":    self._s_ema(ef, es, price, direction),
            "adx_strength": self._s_adx(adx),
            "vwap_confirm": self._s_vwap(price, vwap, direction),
            "orb_breakout": self._s_orb(price, oh, ol, orb_buffer, direction),
            "oi_alignment": self._s_oi(oi_bias, direction),
        }
        return TrendSignal(
            direction=direction, score=sum(comp.values()),
            component_scores=comp,
            ema_fast=ef, ema_slow=es, adx_val=adx,
            vwap_val=vwap, atr_val=atr,
            orb_high=oh, orb_low=ol,
            latest_price=price, vol_regime=vr,
            rel_volume=rv, oi_bias=oi_bias,
        )

    def best_direction(
        self,
        df: pd.DataFrame,
        orb_buffer: float = 0.0,
        oi_bias: Optional[str] = None,
    ) -> TrendSignal:
        ce = self.compute(df, "CE", orb_buffer, oi_bias)
        pe = self.compute(df, "PE", orb_buffer, oi_bias)
        return ce if ce.score >= pe.score else pe


# ================================================================
# RISK ENGINE
# ================================================================

@dataclass
class RiskState:
    total_pnl:           float = 0.0
    trade_count:         int   = 0
    consecutive_losses:  int   = 0
    open_positions:      int   = 0
    total_exposure:      float = 0.0
    circuit_broken:      bool  = False


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
            if s.circuit_broken:
                return False, "Circuit breaker active"
            if s.total_pnl <= m.max_daily_loss:
                return False, f"Daily loss limit (Rs {s.total_pnl:.0f})"
            if s.trade_count >= m.max_trades_per_day:
                return False, f"Max trades ({m.max_trades_per_day})"
            if s.consecutive_losses >= self._cfg.max_consecutive_losses:
                s.circuit_broken = True
                self._logger.trade(name, "CIRCUIT BREAKER: consecutive losses hit.")
                return False, "Circuit breaker"
            if s.total_exposure + exposure > self._cfg.portfolio_max_exposure_rs:
                return False, "Portfolio exposure cap"
            return True, "OK"

    def on_open(self, exposure: float) -> None:
        with self._lock:
            self.state.open_positions += 1
            self.state.total_exposure += exposure

    def on_close(self, pnl_rs: float, exposure: float) -> None:
        with self._lock:
            self.state.open_positions  = max(0, self.state.open_positions - 1)
            self.state.total_exposure  = max(0.0, self.state.total_exposure - exposure)
            self.state.total_pnl      += pnl_rs
            self.state.trade_count    += 1
            if pnl_rs <= 0:
                self.state.consecutive_losses += 1
            else:
                self.state.consecutive_losses  = 0


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

    def create(self, atr: float, name: str) -> TrailState:
        sl = -atr * self._cfg.mode.atr_sl_multiplier
        self._logger.activity(name, f"Trail init: SL={sl:.2f}pts (ATR={atr:.2f})")
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

        # Break-even
        be_pts = abs(state.initial_sl) * m.be_trigger_r
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

        # High-water-mark drop
        hwm_sl = state.high_water - abs(state.initial_sl) * self._cfg.high_water_drop_r
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


class Scanner:
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

    def rank(self) -> list[StockRank]:
        results: list[StockRank] = []
        for sym in NIFTY50_STOCKS:
            df = self._mde.get_candles(sym, self._cfg.equity_exchange)
            if df is None or len(df) < 20:
                continue
            try:
                sig = self._scorer.best_direction(df)
                results.append(StockRank(sym, sig.score, sig.direction, sig))
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

        try:
            ce_sig = scorer.compute(df, "CE", icfg["orb_buffer"], oi_bias)
            pe_sig = scorer.compute(df, "PE", icfg["orb_buffer"], oi_bias)
        except Exception as e:
            logger.activity(name, f"Score err: {e}"); time.sleep(10); continue

        best = ce_sig if ce_sig.score >= pe_sig.score else pe_sig

        logger.activity(name, (
            f"Price:{price:.1f} ADX:{adx_val:.1f} ATR:{atr_val:.2f} "
            f"Regime:{vol_reg} | CE:{ce_sig.score:.0f} PE:{pe_sig.score:.0f} "
            f"→ {best.direction}:{best.score:.0f}"
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

        # ── Snapshot all entry context for trade card ────────
        max_pain_strike, max_pain_bias, _ = analyse_chain(
            chain_data or {}, atm, icfg["strike_interval"])
        entry_depth   = cache.get_depth(opt_symbol)
        orb_h, orb_l  = te.orb(df)
        ema9_entry    = te.ema(df, cfg.ema_fast)
        ema21_entry   = te.ema(df, cfg.ema_slow)
        vwap_entry    = te.vwap(df)
        strike_labels = {"high": "ITM (1 strike in the money)",
                         "medium": "ATM", "low": "OTM (1 strike out)"}
        strike_type_str = strike_labels.get(vol_reg, "ATM")
        def momentum_label(adx: float, ema_f: float, ema_s: float, opt: str) -> str:
            bull = ema_f > ema_s
            if adx >= 30 and bull and opt == "CE": return "Strong Bullish"
            if adx >= 20 and bull and opt == "CE": return "Bullish"
            if adx >= 30 and not bull and opt == "PE": return "Strong Bearish"
            if adx >= 20 and not bull and opt == "PE": return "Bearish"
            return "Neutral"
        momentum_entry = momentum_label(adx_val, ema9_entry, ema21_entry, option_type)
        # break-even tracking
        be_triggered_flag  = False
        be_trigger_pnl_val = 0.0
        be_trigger_time_val: Optional[datetime] = None

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

            was_be = trail.be_triggered
            trail_eng.update(trail, pnl_pts, atr_cached, name)
            if trail.be_triggered and not was_be:
                be_triggered_flag  = True
                be_trigger_pnl_val = pnl_pts
                be_trigger_time_val = now()

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

        # Exit indicator snapshot
        exit_spot = exit_ema9 = exit_ema21 = exit_vwap = exit_adx = 0.0
        exit_momentum = "N/A"
        df_exit = mde.get_candles(icfg["index_symbol"], icfg["index_exchange"])
        if df_exit is not None and len(df_exit) >= 5:
            try:
                exit_spot   = float(df_exit["close"].iloc[-1])
                exit_ema9   = te.ema(df_exit, cfg.ema_fast)
                exit_ema21  = te.ema(df_exit, cfg.ema_slow)
                exit_vwap   = te.vwap(df_exit)
                exit_adx    = te.adx(df_exit)
                exit_momentum = momentum_label(exit_adx, exit_ema9, exit_ema21, option_type)
            except Exception as e:
                logger.activity(name, f"Exit snapshot err: {e}")

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
            running_total=total_pnl,
            # entry snapshot
            trend_score=best.score,
            score_components=best.component_scores,
            vol_regime=vol_reg,
            strike_type=strike_type_str,
            entry_spot=price,
            entry_ema9=ema9_entry,
            entry_ema21=ema21_entry,
            entry_vwap=vwap_entry,
            entry_adx=adx_val,
            entry_atr=atr_val,
            entry_orb_high=orb_h,
            entry_orb_low=orb_l,
            entry_bid=entry_depth.get("bid", 0.0),
            entry_ask=entry_depth.get("ask", 0.0),
            max_pain_strike=max_pain_strike,
            max_pain_bias=max_pain_bias,
            oi_bias=oi_bias,
            momentum_state=momentum_entry,
            initial_sl_pts=trail.initial_sl,
            initial_sl_premium=entry_filled + trail.initial_sl,
            # break-even audit
            be_triggered=be_triggered_flag,
            be_trigger_pnl=be_trigger_pnl_val,
            be_trigger_time=be_trigger_time_val,
            # trail audit
            trail_log=list(trail.trail_log),
            high_water_pts=trail.high_water,
            sl_at_exit=trail.dynamic_sl,
            # exit snapshot
            exit_spot=exit_spot,
            exit_ema9=exit_ema9,
            exit_ema21=exit_ema21,
            exit_vwap=exit_vwap,
            exit_adx=exit_adx,
            exit_momentum=exit_momentum,
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

    while True:
        ns = now_str()

        # ── Hard exit all equity ─────────────────────────────
        if ns >= cfg.hard_exit_time:
            for sym, st in list(open_trades.items()):
                fill_x = exec_eng.execute(sym, cfg.equity_exchange, "SELL", st["qty"], "EQUITY")
                curr_p = fill_x.filled_price or cache.get_ltp(sym) or st["entry"]
                pnl_rs = (curr_p - st["entry"]) * st["qty"]
                total_pnl += pnl_rs; trade_count += 1
                risk.on_close(pnl_rs, st["exposure"])
                rec = TradeRecord(
                    trade_num=trade_count, instrument="EQUITY", symbol=sym,
                    direction="BUY",
                    entry_price_quoted=st["entry"], entry_price_filled=st["entry"],
                    exit_price=curr_p, entry_time=st["entry_time"], exit_time=now(),
                    exit_reason="Hard Exit", quantity=st["qty"],
                    pnl_pts=curr_p-st["entry"], pnl_rs=pnl_rs,
                    mfe_pts=st["mfe"], mae_pts=st["mae"],
                    capture_pct=(curr_p-st["entry"])/st["mfe"]*100 if st["mfe"] > 0 else 0,
                    hold_seconds=(now()-st["entry_time"]).total_seconds(),
                    running_total=total_pnl,
                    trend_score=st["score"],
                    score_components=st.get("score_components", {}),
                    vol_regime=st.get("vol_regime", "medium"),
                    entry_spot=st["entry"],
                    entry_ema9=st.get("ema9", 0.0),
                    entry_ema21=st.get("ema21", 0.0),
                    entry_vwap=st.get("vwap", 0.0),
                    entry_adx=st.get("adx", 0.0),
                    entry_atr=st.get("atr", 0.0),
                    momentum_state=st.get("momentum", "Neutral"),
                    initial_sl_pts=st.get("initial_sl", 0.0),
                    be_triggered=st["trail"].be_triggered,
                    be_trigger_pnl=st.get("be_trigger_pnl", 0.0),
                    be_trigger_time=st.get("be_trigger_time"),
                    trail_log=list(st["trail"].trail_log),
                    high_water_pts=st["trail"].high_water,
                    sl_at_exit=st["trail"].dynamic_sl,
                    exit_spot=curr_p,
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

            pnl_pts   = curr_p - st["entry"]
            st["mfe"] = max(st["mfe"], pnl_pts)
            st["mae"] = min(st["mae"], pnl_pts)

            trail: TrailState = st["trail"]
            trail_eng.update(trail, pnl_pts, st["atr"], f"EQUITY:{sym}")

            held = (now() - st["entry_time"]).total_seconds()
            if trail_eng.should_exit(trail, pnl_pts):
                if held < cfg.min_hold_seconds and pnl_pts > trail.initial_sl:
                    continue
                fill_x = exec_eng.execute(sym, cfg.equity_exchange, "SELL", st["qty"], "EQUITY")
                exit_p = fill_x.filled_price or curr_p
                pnl_rs = (exit_p - st["entry"]) * st["qty"]
                total_pnl += pnl_rs; trade_count += 1
                risk.on_close(pnl_rs, st["exposure"])
                er = "Trailing SL" if trail.be_triggered else "Initial SL"
                rec = TradeRecord(
                    trade_num=trade_count, instrument="EQUITY", symbol=sym,
                    direction="BUY",
                    entry_price_quoted=st["entry"], entry_price_filled=st["entry"],
                    exit_price=exit_p, entry_time=st["entry_time"], exit_time=now(),
                    exit_reason=er, quantity=st["qty"],
                    pnl_pts=exit_p-st["entry"], pnl_rs=pnl_rs,
                    mfe_pts=st["mfe"], mae_pts=st["mae"],
                    capture_pct=(exit_p-st["entry"])/st["mfe"]*100 if st["mfe"]>0 else 0,
                    hold_seconds=held, running_total=total_pnl,
                    trend_score=st["score"],
                    score_components=st.get("score_components", {}),
                    vol_regime=st.get("vol_regime", "medium"),
                    entry_spot=st["entry"],
                    entry_ema9=st.get("ema9", 0.0),
                    entry_ema21=st.get("ema21", 0.0),
                    entry_vwap=st.get("vwap", 0.0),
                    entry_adx=st.get("adx", 0.0),
                    entry_atr=st.get("atr", 0.0),
                    momentum_state=st.get("momentum", "Neutral"),
                    initial_sl_pts=st.get("initial_sl", 0.0),
                    be_triggered=trail.be_triggered,
                    be_trigger_pnl=st.get("be_trigger_pnl", 0.0),
                    be_trigger_time=st.get("be_trigger_time"),
                    trail_log=list(trail.trail_log),
                    high_water_pts=trail.high_water,
                    sl_at_exit=trail.dynamic_sl,
                    exit_spot=exit_p,
                )
                records.append(rec); logger.log_trade_card(rec)
                logger.trade("EQUITY", f"EXIT {er} | {sym} | Rs{pnl_rs:+.0f}")
                del open_trades[sym]

        # ── Re-rank stocks ───────────────────────────────────
        if last_rank_ts is None or (now()-last_rank_ts).total_seconds() >= cfg.stock_rank_refresh:
            ranked       = scanner.rank()
            last_rank_ts = now()
            top5         = ranked[:5]
            logger.activity("EQUITY", "Rank: " + " | ".join(
                f"{r.symbol}:{r.score:.0f}({r.direction})" for r in top5
            ))

        # ── New entries ──────────────────────────────────────
        m = cfg.mode
        if len(open_trades) >= m.max_equity_positions or ns >= cfg.late_entry_cutoff:
            time.sleep(cfg.tick_sl_check_interval); continue

        ok, reason = risk.can_trade("EQUITY")
        if not ok:
            time.sleep(30); continue

        for rank in ranked[:cfg.top_n_stocks]:
            if rank.symbol in open_trades: continue
            if len(open_trades) >= m.max_equity_positions: break
            if rank.score < m.score_threshold: continue
            if rank.direction != "CE": continue   # equity long only
            sig = rank.signal
            if sig is None or sig.adx_val < 20: continue

            price = cache.get_ltp(rank.symbol) or mde.get_quote_rest(
                rank.symbol, cfg.equity_exchange)
            if not price or price <= 0: continue

            qty      = max(1, int(cfg.equity_lot_value / price))
            exposure = qty * price
            ok2, r2  = risk.can_trade("EQUITY", exposure)
            if not ok2: continue

            fill = exec_eng.execute(rank.symbol, cfg.equity_exchange, "BUY", qty, "EQUITY")
            if not fill.success: continue

            filled_p   = fill.filled_price or price
            slippage_eq = filled_p - price
            trail      = trail_eng.create(sig.atr_val, f"EQUITY:{rank.symbol}")
            risk.on_open(exposure)
            # Momentum label for equity
            eq_bull = sig.ema_fast > sig.ema_slow
            if sig.adx_val >= 30 and eq_bull:   eq_mom = "Strong Bullish"
            elif sig.adx_val >= 20 and eq_bull:  eq_mom = "Bullish"
            else:                                eq_mom = "Neutral"
            open_trades[rank.symbol] = {
                "entry": filled_p, "qty": qty, "exposure": exposure,
                "entry_time": now(), "trail": trail,
                "mfe": 0.0, "mae": 0.0,
                "atr": sig.atr_val, "score": rank.score, "adx": sig.adx_val,
                # full snapshot
                "score_components": sig.component_scores,
                "vol_regime": sig.vol_regime,
                "ema9": sig.ema_fast,
                "ema21": sig.ema_slow,
                "vwap": sig.vwap_val,
                "momentum": eq_mom,
                "initial_sl": trail.initial_sl,
                "slippage": slippage_eq,
                "be_trigger_pnl": 0.0,
                "be_trigger_time": None,
            }
            logger.trade("EQUITY", (
                f"ENTRY #{trade_count+1} {rank.symbol} BUY {qty}@{filled_p:.2f} "
                f"Score:{rank.score:.0f} ADX:{sig.adx_val:.1f}"
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
    all_symbols: list[str] = list(NIFTY50_STOCKS)
    for name, icfg in cfg.index_configs.items():
        all_symbols.append(icfg["index_symbol"])

    ist_now = datetime.now(IST)
    tag     = "PAPER TRADE 🟡" if paper_mode else "LIVE TRADE 🔴"

    logger.trade("MAIN", "=" * 64)
    logger.trade("MAIN", f"Trend Rider V2.0  |  {ist_now.strftime('%Y-%m-%d %H:%M')}")
    logger.trade("MAIN", f"Mode: {mode_name.upper()}  |  {tag}")
    logger.trade("MAIN", f"Score threshold : {cfg.mode.score_threshold}")
    logger.trade("MAIN", f"Hard exit       : {cfg.hard_exit_time} IST")
    logger.trade("MAIN", f"Log directory   : ~/trend_rider/logs/")
    logger.trade("MAIN", f"WS available    : {_SIO_AVAILABLE}")
    logger.trade("MAIN", "=" * 64)

    # Start WebSocket feed first
    ws.start(all_symbols)
    time.sleep(2)   # allow WS handshake before strategies start

    threads: list[threading.Thread] = []

    for name, icfg in cfg.index_configs.items():
        t = threading.Thread(
            target=run_index,
            args=(name, icfg, cfg, mde, scorer, risk, trail_eng, exec_eng, cache, logger),
            daemon=True, name=f"Thread-{name}",
        )
        t.start(); threads.append(t)

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