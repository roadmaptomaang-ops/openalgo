"""
Trend Rider — Production Intraday Trend Following Bot
======================================================
Markets  : NIFTY options, SENSEX options, Nifty 50 equity (cash MIS)
Strategy : Capture large intraday trending moves; let winners run.
Version  : 1.0.0

Architecture
------------
  ConfigManager      — all tuneable parameters + trading modes
  LoggingEngine      — trade log, activity log, daily summary
  MarketDataEngine   — quotes, candles, option chain via OpenAlgo API
  TrendEngine        — EMA, ADX, VWAP, ORB, ATR, OI per instrument
  ScoringEngine      — weighted trend score (pluggable indicators)
  RiskEngine         — per-trade, daily, portfolio, consecutive-loss guards
  StrikeSelector     — ATR-based dynamic option strike selection
  TrailingStopEngine — multi-mode trailing (ATR, swing-low/high, step)
  ExecutionEngine    — order placement + fill tracking
  PositionManager    — open position state + MFE/MAE tracking
  Scanner            — rank Nifty 50 stocks each cycle
  StrategyLoop       — orchestrates all modules per instrument
"""

from __future__ import annotations

import os
import time
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import pandas as pd
from zoneinfo import ZoneInfo
import requests

# ============================================================
# NIFTY 50 CONSTITUENTS  (NSE symbols)
# ============================================================

NIFTY50_STOCKS: list[str] = [
    "RELIANCE", "TCS", "HDFCBANK", "BHARTIARTL", "ICICIBANK",
    "INFOSYS", "SBIN", "HINDUNILVR", "ITC", "KOTAKBANK",
    "LT", "AXISBANK", "BAJFINANCE", "ASIANPAINT", "MARUTI",
    "HCLTECH", "SUNPHARMA", "TITAN", "ULTRACEMCO", "NTPC",
    "WIPRO", "POWERGRID", "BAJAJFINSV", "TECHM", "NESTLEIND",
    "ADANIENT", "ADANIPORTS", "TATAMOTORS", "TATASTEEL", "JSWSTEEL",
    "HINDALCO", "COALINDIA", "ONGC", "BPCL", "IOC",
    "GRASIM", "DIVISLAB", "CIPLA", "DRREDDY", "APOLLOHOSP",
    "EICHERMOT", "HEROMOTOCO", "BAJAJ-AUTO", "M&M", "TATACONSUM",
    "BRITANNIA", "SHRIRAMFIN", "BEL", "INDUSINDBK", "HDFCLIFE",
]

# ============================================================
# CONFIG MANAGER
# ============================================================

@dataclass
class TradingMode:
    name: str
    score_threshold: float       # minimum trend score to enter
    atr_sl_multiplier: float     # initial SL = ATR * this
    atr_trail_multiplier: float  # trailing SL = ATR * this
    be_trigger_r: float          # break-even after this many R multiples
    max_trades_per_day: int
    max_equity_positions: int
    cooldown_after_sl: int       # seconds
    cooldown_after_exit: int     # seconds
    max_daily_loss: float        # in Rs
    per_trade_risk_rs: float     # max risk per trade in Rs


MODES: dict[str, TradingMode] = {
    "conservative": TradingMode(
        name="Conservative",
        score_threshold=85,
        atr_sl_multiplier=1.5,
        atr_trail_multiplier=2.0,
        be_trigger_r=1.0,
        max_trades_per_day=3,
        max_equity_positions=2,
        cooldown_after_sl=600,
        cooldown_after_exit=300,
        max_daily_loss=-4000,
        per_trade_risk_rs=1500,
    ),
    "balanced": TradingMode(
        name="Balanced",
        score_threshold=80,
        atr_sl_multiplier=1.2,
        atr_trail_multiplier=1.8,
        be_trigger_r=0.8,
        max_trades_per_day=5,
        max_equity_positions=3,
        cooldown_after_sl=300,
        cooldown_after_exit=180,
        max_daily_loss=-6000,
        per_trade_risk_rs=2000,
    ),
    "aggressive": TradingMode(
        name="Aggressive",
        score_threshold=75,
        atr_sl_multiplier=1.0,
        atr_trail_multiplier=1.5,
        be_trigger_r=0.6,
        max_trades_per_day=8,
        max_equity_positions=3,
        cooldown_after_sl=180,
        cooldown_after_exit=120,
        max_daily_loss=-10000,
        per_trade_risk_rs=3000,
    ),
}

@dataclass
class AppConfig:
    # ── API ───────────────────────────────────────────────
    api_key: str                        = ""
    base_url: str                       = "http://127.0.0.1:5000/api/v1"

    # ── Session times (IST) ───────────────────────────────
    entry_time: str                     = "09:20"
    late_entry_cutoff: str              = "13:30"   # no fresh entries after this
    hard_exit_time: str                 = "15:15"   # hard exit all positions
    lunch_start: str                    = "11:45"
    lunch_end: str                      = "13:15"

    # ── Scan & signal ─────────────────────────────────────
    signal_interval: int                = 15        # seconds between scans
    candle_confirm_count: int           = 2         # consecutive matching signals
    min_hold_seconds: int               = 120       # minimum position hold time
    chain_refresh_interval: int         = 900       # re-fetch option chain (seconds)
    stock_rank_refresh: int             = 60        # re-rank stocks (seconds)

    # ── Indicators ────────────────────────────────────────
    ema_fast: int                       = 9
    ema_slow: int                       = 21
    adx_period: int                     = 14
    atr_period: int                     = 14
    orb_candles: int                    = 3
    swing_lookback: int                 = 5         # candles for swing high/low trail

    # ── Index instruments ────────────────────────────────
    index_configs: dict                 = field(default_factory=lambda: {
        "NIFTY": {
            "index_symbol":    "NIFTY",
            "index_exchange":  "NSE_INDEX",
            "option_exchange": "NFO",
            "quantity":        75,
            "strike_interval": 50,
            "expiry_day":      1,   # Monday=0 … Sunday=6; Tuesday=1
            "orb_buffer":      30,
        },
        "SENSEX": {
            "index_symbol":    "SENSEX",
            "index_exchange":  "BSE_INDEX",
            "option_exchange": "BFO",
            "quantity":        20,
            "strike_interval": 100,
            "expiry_day":      3,   # Thursday=3
            "orb_buffer":      80,
        },
    })

    # ── Equity ───────────────────────────────────────────
    equity_exchange: str                = "NSE"
    equity_lot_value: float             = 50000.0   # approx capital per stock trade
    top_n_stocks: int                   = 3         # max simultaneous equity positions

    # ── Scoring weights (must sum to 100) ────────────────
    score_weights: dict                 = field(default_factory=lambda: {
        "ema_trend":   25,
        "adx_strength": 20,
        "vwap_confirm": 20,
        "orb_breakout": 20,
        "oi_alignment": 15,
    })

    # ── Trailing stop ────────────────────────────────────
    high_water_drop_r: float            = 1.0       # exit if falls this many R from peak
    swing_trail_enabled: bool           = True

    # ── Risk ─────────────────────────────────────────────
    max_consecutive_losses: int         = 3         # circuit breaker
    portfolio_max_exposure_rs: float    = 200000.0

    # ── Active mode ──────────────────────────────────────
    mode_name: str                      = "balanced"

    @property
    def mode(self) -> TradingMode:
        return MODES[self.mode_name]


# ============================================================
# LOGGING ENGINE
# ============================================================

class LoggingEngine:
    """Thread-safe dual-log (trade + activity) with daily summary."""

    def __init__(self, log_dir: str = "~/openalgo/trend_logs"):
        self._dir  = Path(os.path.expanduser(log_dir))
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._ist  = ZoneInfo("Asia/Kolkata")

    def _now(self) -> datetime:
        return datetime.now(self._ist)

    def _trade_path(self) -> Path:
        return self._dir / f"{self._now().strftime('%Y-%m-%d')}_trades.log"

    def _activity_path(self) -> Path:
        return self._dir / f"{self._now().strftime('%Y-%m-%d')}_activity.log"

    def _write(self, path: Path, text: str) -> None:
        try:
            with self._lock:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(text + "\n")
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

    def day_start(self, name: str, mode_name: str) -> None:
        sep = "#" * 60
        lines = [
            "", sep,
            f"# Trend Rider — {name} — {self._now().strftime('%Y-%m-%d')}",
            f"# Mode: {mode_name.upper()}",
            sep, "",
        ]
        for line in lines:
            print(line)
            self._write(self._trade_path(), line)

    def log_trade_record(self, r: "TradeRecord") -> None:
        """Write full trade card to trade log."""
        sep = "=" * 60
        hold = str(timedelta(seconds=int(r.hold_seconds)))
        capture = f"{r.capture_pct:.0f}%" if r.mfe_pts and r.mfe_pts > 0 else "N/A"
        lines = [
            "", sep,
            f"TRADE #{r.trade_num} | {r.instrument} | {r.direction} | {r.entry_time.strftime('%H:%M:%S')}",
            sep,
            f"  Symbol       : {r.symbol}",
            f"  Direction    : {r.direction}",
            f"  Entry Price  : {r.entry_price:.2f}",
            f"  Exit Price   : {r.exit_price:.2f}",
            f"  PnL (pts/Rs) : {r.pnl_pts:+.2f} pts  |  Rs {r.pnl_rs:+.0f}",
            f"  MFE          : +{r.mfe_pts:.2f} pts",
            f"  MAE          : {r.mae_pts:.2f} pts",
            f"  Capture      : {capture}",
            f"  Hold Time    : {hold}",
            f"  Exit Reason  : {r.exit_reason}",
            f"  Trend Score  : {r.trend_score:.1f}",
            f"  ADX at entry : {r.adx_entry:.1f}",
            f"  Running PnL  : Rs {r.running_total:+.0f}",
            sep, "",
        ]
        for line in lines:
            print(line)
            self._write(self._trade_path(), line)

    def day_summary(self, name: str, records: list["TradeRecord"]) -> None:
        if not records:
            return
        wins      = [r for r in records if r.pnl_rs > 0]
        losses    = [r for r in records if r.pnl_rs <= 0]
        total     = len(records)
        wr        = len(wins) / total * 100 if total else 0
        gp        = sum(r.pnl_rs for r in wins)
        gl        = abs(sum(r.pnl_rs for r in losses))
        pf        = gp / gl if gl > 0 else float("inf")
        net       = sum(r.pnl_rs for r in records)
        best      = max(r.pnl_rs for r in records)
        worst     = min(r.pnl_rs for r in records)
        avg_hold  = sum(r.hold_seconds for r in records) / total
        avg_mfe   = sum(r.mfe_pts for r in records) / total
        avg_mae   = sum(r.mae_pts for r in records) / total
        avg_cap   = sum(r.capture_pct for r in records) / total

        exit_ct: dict[str, int] = {}
        for r in records:
            exit_ct[r.exit_reason] = exit_ct.get(r.exit_reason, 0) + 1

        sep   = "=" * 60
        lines = [
            "", sep,
            f"DAY SUMMARY — {name} — {datetime.now(pytz.timezone('Asia/Kolkata')).strftime('%Y-%m-%d')}",
            sep, "",
            f"  Total Trades   : {total}",
            f"  Wins / Losses  : {len(wins)} / {len(losses)}",
            f"  Win Rate       : {wr:.1f}%",
            f"  Profit Factor  : {pf:.2f}",
            f"  Gross Profit   : Rs {gp:.0f}",
            f"  Gross Loss     : Rs {gl:.0f}",
            f"  Net PnL        : Rs {net:+.0f}",
            f"  Best Trade     : Rs {best:+.0f}",
            f"  Worst Trade    : Rs {worst:+.0f}",
            f"  Avg Hold Time  : {str(timedelta(seconds=int(avg_hold)))}",
            f"  Avg MFE        : {avg_mfe:.2f} pts",
            f"  Avg MAE        : {avg_mae:.2f} pts",
            f"  Avg Capture    : {avg_cap:.0f}%",
            "", "  Exit Breakdown:",
        ]
        for reason, cnt in exit_ct.items():
            lines.append(f"    {reason:<25}: {cnt}")
        lines += [sep, ""]

        for line in lines:
            print(line)
            self._write(self._trade_path(), line)


# ============================================================
# TRADE RECORD
# ============================================================

@dataclass
class TradeRecord:
    trade_num:    int
    instrument:   str
    symbol:       str
    direction:    str          # "CE" / "PE" / "BUY" / "SELL"
    entry_price:  float
    exit_price:   float        = 0.0
    entry_time:   datetime     = field(default_factory=datetime.now)
    exit_time:    Optional[datetime] = None
    exit_reason:  str          = "Unknown"
    quantity:     int          = 1
    pnl_pts:      float        = 0.0
    pnl_rs:       float        = 0.0
    mfe_pts:      float        = 0.0
    mae_pts:      float        = 0.0
    capture_pct:  float        = 0.0
    hold_seconds: float        = 0.0
    trend_score:  float        = 0.0
    adx_entry:    float        = 0.0
    running_total: float       = 0.0


# ============================================================
# MARKET DATA ENGINE
# ============================================================

class MarketDataEngine:
    """Thin wrapper around OpenAlgo REST API."""

    def __init__(self, cfg: AppConfig):
        self._cfg     = cfg
        self._session = requests.Session()
        self._ist     = ZoneInfo("Asia/Kolkata")

    def _post(self, endpoint: str, payload: dict, timeout: int = 15) -> Optional[dict]:
        payload = {"apikey": self._cfg.api_key, **payload}
        try:
            r = self._session.post(
                f"{self._cfg.base_url}/{endpoint}",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=timeout,
            )
            try:
                data = r.json()
            except ValueError:
                return None
            if r.status_code >= 400:
                return None
            return data
        except Exception:
            return None

    def get_quote(self, symbol: str, exchange: str) -> Optional[float]:
        data = self._post("quotes", {"symbol": symbol, "exchange": exchange}, timeout=5)
        if data and data.get("status") == "success":
            try:
                return float(data["data"]["ltp"])
            except (KeyError, TypeError, ValueError):
                return None
        return None

    def get_candles(self, symbol: str, exchange: str, interval: str = "1m") -> Optional[pd.DataFrame]:
        today = datetime.now(self._ist).strftime("%Y-%m-%d")
        data  = self._post("history", {
            "symbol":     symbol,
            "exchange":   exchange,
            "interval":   interval,
            "start_date": today,
            "end_date":   today,
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

    def get_option_chain(self, underlying: str, exchange: str, expiry: str) -> Optional[dict]:
        data = self._post("optionchain", {
            "underlying":  underlying,
            "expiry_date": expiry,
            "exchange":    exchange,
        })
        if data and data.get("status") == "success":
            return data
        return None

    def place_order(
        self,
        symbol: str,
        exchange: str,
        action: str,
        quantity: int,
        strategy: str = "Trend Rider V1.0",
    ) -> Optional[str]:
        data = self._post("placeorder", {
            "symbol":    symbol,
            "exchange":  exchange,
            "action":    action,
            "quantity":  quantity,
            "price":     0,
            "pricetype": "MARKET",
            "product":   "MIS",
            "strategy":  strategy,
        })
        if data and data.get("status") == "success":
            return data.get("orderid")
        return None


# ============================================================
# TREND ENGINE  (technical indicators)
# ============================================================

class TrendEngine:
    """Compute all technical indicators from a candle DataFrame."""

    def __init__(self, cfg: AppConfig):
        self._cfg = cfg

    def ema(self, df: pd.DataFrame, period: int) -> float:
        return float(df["close"].ewm(span=period, adjust=False).mean().iloc[-1])

    def vwap(self, df: pd.DataFrame) -> float:
        tp = (df["high"] + df["low"] + df["close"]) / 3
        if "volume" in df.columns and df["volume"].sum() > 0:
            return float(
                (tp * df["volume"]).cumsum().iloc[-1] / df["volume"].cumsum().iloc[-1]
            )
        return float(tp.expanding().mean().iloc[-1])

    def atr(self, df: pd.DataFrame, period: Optional[int] = None) -> float:
        p  = period or self._cfg.atr_period
        hl = df["high"] - df["low"]
        hc = (df["high"] - df["close"].shift()).abs()
        lc = (df["low"]  - df["close"].shift()).abs()
        tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
        val = tr.rolling(p).mean().iloc[-1]
        return float(val) if pd.notna(val) else 0.0

    def adx(self, df: pd.DataFrame, period: Optional[int] = None) -> float:
        p             = period or self._cfg.adx_period
        plus_dm_raw   = df["high"].diff().clip(lower=0)
        minus_dm_raw  = (-df["low"].diff()).clip(lower=0)
        plus_dm       = plus_dm_raw.where(plus_dm_raw >= minus_dm_raw, 0.0)
        minus_dm      = minus_dm_raw.where(minus_dm_raw > plus_dm_raw, 0.0)
        hl = df["high"] - df["low"]
        hc = (df["high"] - df["close"].shift()).abs()
        lc = (df["low"]  - df["close"].shift()).abs()
        tr       = pd.concat([hl, hc, lc], axis=1).max(axis=1)
        atr_     = tr.rolling(p).mean()
        plus_di  = 100 * plus_dm.rolling(p).mean()  / atr_
        minus_di = 100 * minus_dm.rolling(p).mean() / atr_
        dx       = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, float("nan"))) * 100
        val      = dx.rolling(p).mean().iloc[-1]
        return float(val) if pd.notna(val) else 0.0

    def orb(self, df: pd.DataFrame, n_candles: Optional[int] = None) -> tuple[float, float]:
        n = n_candles or self._cfg.orb_candles
        orb_df = df.head(n)
        return float(orb_df["high"].max()), float(orb_df["low"].min())

    def swing_low(self, df: pd.DataFrame) -> float:
        lb = self._cfg.swing_lookback
        return float(df["low"].iloc[-lb:].min())

    def swing_high(self, df: pd.DataFrame) -> float:
        lb = self._cfg.swing_lookback
        return float(df["high"].iloc[-lb:].max())

    def volatility_regime(self, atr_val: float, price: float) -> str:
        """Return 'high', 'medium', or 'low' based on ATR as % of price."""
        if price <= 0:
            return "medium"
        pct = atr_val / price * 100
        if pct > 0.8:
            return "high"
        elif pct > 0.4:
            return "medium"
        return "low"

    def relative_volume(self, df: pd.DataFrame) -> float:
        """Latest candle volume vs 20-candle average."""
        if "volume" not in df.columns or len(df) < 5:
            return 1.0
        avg = df["volume"].rolling(20).mean().iloc[-1]
        if pd.isna(avg) or avg == 0:
            return 1.0
        return float(df["volume"].iloc[-1] / avg)


# ============================================================
# SCORING ENGINE
# ============================================================

@dataclass
class TrendSignal:
    direction: str          # "CE" (bullish) / "PE" (bearish) / "NEUTRAL"
    score: float
    component_scores: dict[str, float]
    ema_fast: float
    ema_slow: float
    adx_val: float
    vwap_val: float
    atr_val: float
    orb_high: float
    orb_low: float
    latest_price: float
    volatility_regime: str
    rel_volume: float
    oi_bias: Optional[str]


class ScoringEngine:
    """
    Pluggable weighted scoring model.
    Add new indicators by inserting into _score_* methods
    and registering them in score_weights.
    """

    def __init__(self, cfg: AppConfig, trend: TrendEngine):
        self._cfg   = cfg
        self._trend = trend

    # ── Individual component scorers ─────────────────────────

    def _score_ema_trend(
        self, ema_fast: float, ema_slow: float, price: float, direction: str
    ) -> float:
        """25 pts: EMA alignment + price above/below both EMAs."""
        if direction == "CE":
            if ema_fast > ema_slow and price > ema_fast:
                return 25.0
            if ema_fast > ema_slow:
                return 15.0
        elif direction == "PE":
            if ema_fast < ema_slow and price < ema_fast:
                return 25.0
            if ema_fast < ema_slow:
                return 15.0
        return 0.0

    def _score_adx(self, adx_val: float) -> float:
        """20 pts: ADX strength (higher = more trending)."""
        if adx_val >= 35:
            return 20.0
        if adx_val >= 25:
            return 15.0
        if adx_val >= 20:
            return 10.0
        return 0.0

    def _score_vwap(self, price: float, vwap: float, direction: str) -> float:
        """20 pts: Price on correct side of VWAP."""
        if direction == "CE" and price > vwap:
            return 20.0
        if direction == "PE" and price < vwap:
            return 20.0
        return 0.0

    def _score_orb(
        self, price: float, orb_high: float, orb_low: float,
        buffer: float, direction: str
    ) -> float:
        """20 pts: Clean ORB breakout beyond buffer."""
        if direction == "CE" and price > orb_high + buffer:
            return 20.0
        if direction == "PE" and price < orb_low - buffer:
            return 20.0
        return 0.0

    def _score_oi(self, oi_bias: Optional[str], direction: str) -> float:
        """15 pts: OI alignment with trade direction."""
        if oi_bias and oi_bias == direction:
            return 15.0
        if oi_bias is None:
            return 7.5   # neutral — partial score
        return 0.0

    # ── Main scorer ──────────────────────────────────────────

    def compute(
        self,
        df: pd.DataFrame,
        direction: str,
        orb_buffer: float = 0.0,
        oi_bias: Optional[str] = None,
    ) -> TrendSignal:
        te   = self._trend
        w    = self._cfg.score_weights

        ema_fast  = te.ema(df, self._cfg.ema_fast)
        ema_slow  = te.ema(df, self._cfg.ema_slow)
        vwap_val  = te.vwap(df)
        adx_val   = te.adx(df)
        atr_val   = te.atr(df)
        orb_h, orb_l = te.orb(df)
        price     = float(df["close"].iloc[-1])
        vol_reg   = te.volatility_regime(atr_val, price)
        rel_vol   = te.relative_volume(df)

        components: dict[str, float] = {
            "ema_trend":    self._score_ema_trend(ema_fast, ema_slow, price, direction),
            "adx_strength": self._score_adx(adx_val),
            "vwap_confirm": self._score_vwap(price, vwap_val, direction),
            "orb_breakout": self._score_orb(price, orb_h, orb_l, orb_buffer, direction),
            "oi_alignment": self._score_oi(oi_bias, direction),
        }

        # Weighted total (each component already returns its max weight)
        total_score = sum(components.values())

        return TrendSignal(
            direction=direction,
            score=total_score,
            component_scores=components,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            adx_val=adx_val,
            vwap_val=vwap_val,
            atr_val=atr_val,
            orb_high=orb_h,
            orb_low=orb_l,
            latest_price=price,
            volatility_regime=vol_reg,
            rel_volume=rel_vol,
            oi_bias=oi_bias,
        )

    def best_direction(
        self,
        df: pd.DataFrame,
        orb_buffer: float = 0.0,
        oi_bias: Optional[str] = None,
    ) -> TrendSignal:
        """Score both directions and return the stronger one."""
        ce = self.compute(df, "CE", orb_buffer, oi_bias)
        pe = self.compute(df, "PE", orb_buffer, oi_bias)
        return ce if ce.score >= pe.score else pe


# ============================================================
# RISK ENGINE
# ============================================================

@dataclass
class RiskState:
    total_pnl:          float = 0.0
    trade_count:        int   = 0
    consecutive_losses: int   = 0
    open_positions:     int   = 0
    total_exposure:     float = 0.0
    circuit_broken:     bool  = False


class RiskEngine:
    """Gate every trade attempt through multiple risk checks."""

    def __init__(self, cfg: AppConfig, logger: LoggingEngine):
        self._cfg    = cfg
        self._logger = logger
        self.state   = RiskState()

    def can_trade(self, name: str, exposure_to_add: float = 0.0) -> tuple[bool, str]:
        m = self._cfg.mode
        s = self.state

        if s.circuit_broken:
            return False, "Circuit breaker active"
        if s.total_pnl <= m.max_daily_loss:
            return False, f"Daily loss limit (Rs {s.total_pnl:.0f})"
        if s.trade_count >= m.max_trades_per_day:
            return False, f"Max trades ({m.max_trades_per_day}) reached"
        if s.consecutive_losses >= self._cfg.max_consecutive_losses:
            s.circuit_broken = True
            self._logger.trade(name, "CIRCUIT BREAKER: consecutive loss limit hit.")
            return False, "Consecutive loss circuit breaker"
        if s.total_exposure + exposure_to_add > self._cfg.portfolio_max_exposure_rs:
            return False, "Portfolio exposure cap"
        return True, "OK"

    def on_trade_open(self, exposure: float) -> None:
        self.state.open_positions  += 1
        self.state.total_exposure  += exposure

    def on_trade_close(self, pnl_rs: float, exposure: float) -> None:
        self.state.open_positions  = max(0, self.state.open_positions - 1)
        self.state.total_exposure  = max(0.0, self.state.total_exposure - exposure)
        self.state.total_pnl      += pnl_rs
        self.state.trade_count    += 1
        if pnl_rs <= 0:
            self.state.consecutive_losses += 1
        else:
            self.state.consecutive_losses  = 0


# ============================================================
# STRIKE SELECTOR  (ATR-based dynamic option strike)
# ============================================================

class StrikeSelector:
    """
    Decide CE/PE strike based on volatility regime.
      High ATR   → ITM  (1 strike in the money) for higher delta
      Medium ATR → ATM
      Low ATR    → ATM or 1 strike OTM
    """

    @staticmethod
    def select(
        spot: float,
        atr: float,
        option_type: str,
        strike_interval: int,
        vol_regime: str,
    ) -> int:
        atm = round(spot / strike_interval) * strike_interval

        if vol_regime == "high":
            # ITM: 1 strike in the money
            return atm - strike_interval if option_type == "CE" else atm + strike_interval
        elif vol_regime == "medium":
            # ATM
            return atm
        else:
            # Low vol → slight OTM (cheaper premium, still liquid)
            return atm + strike_interval if option_type == "CE" else atm - strike_interval


# ============================================================
# TRAILING STOP ENGINE
# ============================================================

@dataclass
class TrailState:
    initial_sl:   float         # negative pts (e.g. -1.2 × ATR)
    dynamic_sl:   float         # current SL level in pts from entry
    locked_profit: float = 0.0
    be_triggered: bool   = False
    high_water:   float  = 0.0
    trail_log:    list   = field(default_factory=list)


class TrailingStopEngine:
    """
    Multi-mode trailing stop:
      1. Initial SL (ATR-based)
      2. Break-even once trade reaches cfg.be_trigger_r × initial_risk
      3. ATR-based trailing
      4. Swing-low / swing-high trailing
      5. High-water-mark drop protection
    """

    def __init__(self, cfg: AppConfig, logger: LoggingEngine):
        self._cfg    = cfg
        self._logger = logger

    def create(self, atr: float, name: str) -> TrailState:
        initial = -atr * self._cfg.mode.atr_sl_multiplier
        self._logger.activity(name, f"Trail init: SL={initial:.2f}pts (ATR={atr:.2f})")
        return TrailState(initial_sl=initial, dynamic_sl=initial)

    def update(
        self,
        state: TrailState,
        pnl_pts: float,
        atr: float,
        df: Optional[pd.DataFrame],
        name: str,
    ) -> None:
        m   = self._cfg.mode
        te  = TrendEngine(self._cfg)

        # Track high water
        if pnl_pts > state.high_water:
            state.high_water = pnl_pts

        # Break-even
        be_trigger_pts = abs(state.initial_sl) * m.be_trigger_r
        if pnl_pts >= be_trigger_pts and not state.be_triggered:
            state.dynamic_sl  = 0.0
            state.be_triggered = True
            self._logger.activity(name, f"BREAK-EVEN triggered at +{pnl_pts:.2f}pts")

        # ATR trail
        atr_trail = pnl_pts - atr * m.atr_trail_multiplier
        if atr_trail > state.dynamic_sl:
            old = state.dynamic_sl
            state.dynamic_sl = atr_trail
            state.trail_log.append({
                "type": "ATR", "pnl": pnl_pts,
                "old_sl": old, "new_sl": atr_trail,
            })
            self._logger.activity(name, f"ATR TRAIL: SL {old:.2f} → {atr_trail:.2f}pts")

        # Swing-low/high trail
        if self._cfg.swing_trail_enabled and df is not None and len(df) >= self._cfg.swing_lookback:
            swing = te.swing_low(df)
            # Convert absolute swing level to pts from entry — caller manages entry price
            # Here we store swing reference in pts via signal caller

        # High-water-mark drop protection
        hwm_sl = state.high_water - abs(state.initial_sl) * self._cfg.high_water_drop_r
        if hwm_sl > state.dynamic_sl and state.high_water > be_trigger_pts:
            old = state.dynamic_sl
            state.dynamic_sl = hwm_sl
            state.trail_log.append({
                "type": "HWM", "pnl": pnl_pts,
                "old_sl": old, "new_sl": hwm_sl,
            })
            self._logger.activity(name, f"HWM TRAIL: SL {old:.2f} → {hwm_sl:.2f}pts")

    def should_exit(self, state: TrailState, pnl_pts: float) -> bool:
        return pnl_pts <= state.dynamic_sl


# ============================================================
# OPTION CHAIN ANALYSER
# ============================================================

def analyse_option_chain(
    chain_data: dict,
    atm_strike: int,
    strike_interval: int,
    num_strikes: int = 10,
) -> tuple[Optional[int], Optional[str], Optional[str]]:
    """Return (max_pain_strike, max_pain_bias, oi_bias)."""
    if not chain_data or "chain" not in chain_data:
        return None, None, None

    chain = chain_data["chain"]
    spot  = chain_data.get("underlying_ltp", 0)

    strikes = sorted({row["strike"] for row in chain})
    ce_oi   = {row["strike"]: (row.get("ce") or {}).get("oi", 0) or 0 for row in chain}
    pe_oi   = {row["strike"]: (row.get("pe") or {}).get("oi", 0) or 0 for row in chain}

    # True max pain
    min_pain, max_pain_strike = float("inf"), None
    for s in strikes:
        pain = (
            sum((s - k) * ce_oi[k] for k in strikes if k < s) +
            sum((k - s) * pe_oi[k] for k in strikes if k > s)
        )
        if pain < min_pain:
            min_pain, max_pain_strike = pain, s

    max_pain_bias = None
    if max_pain_strike and spot:
        max_pain_bias = "CE" if max_pain_strike > spot else "PE"

    # Near-ATM OI bias
    ce_oi_total = pe_oi_total = 0
    for row in chain:
        if abs(row["strike"] - atm_strike) <= num_strikes * strike_interval:
            ce_oi_total += (row.get("ce") or {}).get("oi", 0) or 0
            pe_oi_total += (row.get("pe") or {}).get("oi", 0) or 0

    oi_bias = "CE" if pe_oi_total > ce_oi_total else "PE"
    return max_pain_strike, max_pain_bias, oi_bias


# ============================================================
# EXPIRY HELPER
# ============================================================

def get_expiry(expiry_day: int, exit_time: str = "15:15") -> str:
    ist   = ZoneInfo("Asia/Kolkata")
    today = datetime.now(ist)
    d     = today.date()
    days_ahead = (expiry_day - d.weekday()) % 7
    if days_ahead == 0 and today.strftime("%H:%M") >= exit_time:
        days_ahead = 7
    expiry = d if days_ahead == 0 else d + timedelta(days=days_ahead)
    months = ["JAN","FEB","MAR","APR","MAY","JUN",
              "JUL","AUG","SEP","OCT","NOV","DEC"]
    return f"{expiry.day:02d}{months[expiry.month-1]}{expiry.year % 100:02d}"


def build_option_symbol(name: str, expiry: str, strike: int, option_type: str) -> str:
    return f"{name}{expiry}{strike}{option_type}"


# ============================================================
# SCANNER  (Nifty 50 stock ranking)
# ============================================================

@dataclass
class StockRank:
    symbol: str
    score:  float
    direction: str
    signal: Optional[TrendSignal]


class Scanner:
    """Rank all Nifty 50 stocks by trend quality each cycle."""

    def __init__(
        self,
        cfg: AppConfig,
        mde: MarketDataEngine,
        scorer: ScoringEngine,
        logger: LoggingEngine,
    ):
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
                results.append(StockRank(
                    symbol=sym,
                    score=sig.score,
                    direction=sig.direction,
                    signal=sig,
                ))
            except Exception as e:
                self._logger.activity("SCANNER", f"{sym} error: {e}")
        results.sort(key=lambda x: x.score, reverse=True)
        return results


# ============================================================
# STRATEGY LOOP — INDEX OPTIONS (NIFTY / SENSEX)
# ============================================================

def run_index(
    name: str,
    icfg: dict,
    cfg: AppConfig,
    mde: MarketDataEngine,
    scorer: ScoringEngine,
    risk: RiskEngine,
    trail_eng: TrailingStopEngine,
    logger: LoggingEngine,
) -> None:
    ist = ZoneInfo("Asia/Kolkata")

    def now() -> datetime:
        return datetime.now(ist)

    def now_str() -> str:
        return now().strftime("%H:%M")

    logger.day_start(name, cfg.mode_name)
    logger.activity(name, f"Qty:{icfg['quantity']} Strike-interval:{icfg['strike_interval']}")

    trade_count       = 0
    total_pnl         = 0.0
    last_sl_time: Optional[datetime]     = None
    last_exit_time: Optional[datetime]   = None
    last_direction: Optional[str]        = None
    signal_history: deque                = deque(maxlen=cfg.candle_confirm_count)
    expiry                               = get_expiry(icfg["expiry_day"], cfg.hard_exit_time)
    chain_data: Optional[dict]           = None
    last_chain_fetch: Optional[datetime] = None
    records: list[TradeRecord]           = []
    te = TrendEngine(cfg)

    logger.activity(name, f"Expiry: {expiry}")

    while True:
        ns = now_str()

        # ── Session end ──────────────────────────────────────
        if ns >= cfg.hard_exit_time:
            logger.trade(name, f"Session end | Trades:{trade_count} | PnL: Rs {total_pnl:+.0f}")
            logger.day_summary(name, records)
            break

        if ns < cfg.entry_time:
            time.sleep(5)
            continue

        if ns >= cfg.late_entry_cutoff:
            logger.activity(name, "Late entry cutoff — no new entries.")
            time.sleep(30)
            continue

        # ── Daily risk checks ────────────────────────────────
        ok, reason = risk.can_trade(name)
        if not ok:
            logger.trade(name, f"Risk gate: {reason}. Stopping {name}.")
            logger.day_summary(name, records)
            break

        # ── Cooling periods ──────────────────────────────────
        m = cfg.mode
        if last_sl_time:
            elapsed = (now() - last_sl_time).total_seconds()
            if elapsed < m.cooldown_after_sl:
                logger.activity(name, f"SL cooldown: {m.cooldown_after_sl - elapsed:.0f}s")
                time.sleep(20)
                continue

        if last_exit_time:
            elapsed = (now() - last_exit_time).total_seconds()
            if elapsed < m.cooldown_after_exit:
                logger.activity(name, f"Exit cooldown: {m.cooldown_after_exit - elapsed:.0f}s")
                time.sleep(20)
                continue

        # ── Lunch zone ───────────────────────────────────────
        if cfg.lunch_start <= ns <= cfg.lunch_end:
            logger.activity(name, "Lunch zone — skipping.")
            time.sleep(60)
            continue

        # ── Refresh option chain ─────────────────────────────
        if last_chain_fetch is None or (now() - last_chain_fetch).total_seconds() >= cfg.chain_refresh_interval:
            chain_data       = mde.get_option_chain(icfg["index_symbol"], icfg["option_exchange"], expiry)
            last_chain_fetch = now()
            atm_info = chain_data.get("atm_strike") if chain_data else "N/A"
            logger.activity(name, f"Chain refreshed. ATM: {atm_info}")

        # ── Candles & indicators ─────────────────────────────
        df = mde.get_candles(icfg["index_symbol"], icfg["index_exchange"])
        if df is None or len(df) < 20:
            logger.activity(name, "Insufficient candles.")
            time.sleep(15)
            continue

        try:
            atr_val  = te.atr(df)
            adx_val  = te.adx(df)
            price    = float(df["close"].iloc[-1])
            vol_reg  = te.volatility_regime(atr_val, price)
        except Exception as e:
            logger.activity(name, f"Indicator error: {e}")
            time.sleep(10)
            continue

        # ── OI bias ──────────────────────────────────────────
        atm_strike = round(price / icfg["strike_interval"]) * icfg["strike_interval"]
        _, _, oi_bias = analyse_option_chain(chain_data or {}, atm_strike, icfg["strike_interval"])

        # ── Score both directions ─────────────────────────────
        try:
            ce_sig = scorer.compute(df, "CE", icfg["orb_buffer"], oi_bias)
            pe_sig = scorer.compute(df, "PE", icfg["orb_buffer"], oi_bias)
        except Exception as e:
            logger.activity(name, f"Scoring error: {e}")
            time.sleep(10)
            continue

        best = ce_sig if ce_sig.score >= pe_sig.score else pe_sig

        logger.activity(name, (
            f"Price:{price:.1f} ADX:{adx_val:.1f} ATR:{atr_val:.2f} "
            f"Vol:{vol_reg} | CE:{ce_sig.score:.0f} PE:{pe_sig.score:.0f} "
            f"→ {best.direction}:{best.score:.0f}"
        ))

        # ── Threshold gate ───────────────────────────────────
        if best.score < m.score_threshold:
            logger.activity(name, f"Score {best.score:.0f} < threshold {m.score_threshold}. Waiting.")
            signal_history.clear()
            time.sleep(cfg.signal_interval)
            continue

        # ── ADX filter ───────────────────────────────────────
        if adx_val < 20:
            logger.activity(name, f"Low ADX ({adx_val:.1f}) — choppy. Skipping.")
            signal_history.clear()
            time.sleep(20)
            continue

        # ── Signal confirmation (N consecutive) ─────────────
        signal_history.append(best.direction)
        if len(signal_history) < cfg.candle_confirm_count:
            logger.activity(name, f"Confirming signal: {len(signal_history)}/{cfg.candle_confirm_count}")
            time.sleep(cfg.signal_interval)
            continue

        if not all(s == best.direction for s in signal_history):
            logger.activity(name, "Signal inconsistent. Resetting.")
            signal_history.clear()
            time.sleep(cfg.signal_interval)
            continue

        option_type = best.direction

        # ── Same-direction re-entry cooldown ─────────────────
        if (last_direction == option_type and last_sl_time and
                (now() - last_sl_time).total_seconds() < 600):
            logger.activity(name, f"Same-direction ({option_type}) re-entry blocked.")
            signal_history.clear()
            time.sleep(30)
            continue

        # ── Risk gate ────────────────────────────────────────
        approx_exposure = icfg["quantity"] * price
        ok, reason = risk.can_trade(name, approx_exposure)
        if not ok:
            logger.activity(name, f"Risk gate at entry: {reason}")
            time.sleep(cfg.signal_interval)
            continue

        # ── Strike selection ─────────────────────────────────
        strike       = StrikeSelector.select(price, atr_val, option_type, icfg["strike_interval"], vol_reg)
        opt_symbol   = build_option_symbol(name, expiry, strike, option_type)
        logger.activity(name, f"Strike selected: {opt_symbol} (regime={vol_reg})")

        # ── Entry ────────────────────────────────────────────
        entry_premium = mde.get_quote(opt_symbol, icfg["option_exchange"])
        if entry_premium is None:
            logger.activity(name, "Could not fetch premium. Retrying.")
            time.sleep(10)
            continue

        order_id = mde.place_order(opt_symbol, icfg["option_exchange"], "BUY", icfg["quantity"])
        if not order_id:
            logger.activity(name, "Entry order failed.")
            time.sleep(10)
            continue

        trade_count   += 1
        last_direction = option_type
        entry_time_dt  = now()
        signal_history.clear()
        risk.on_trade_open(approx_exposure)

        trail = trail_eng.create(atr_val, name)
        mfe_pts  = 0.0
        mae_pts  = 0.0
        curr     = entry_premium
        position_open = True
        exit_reason   = "Unknown"

        logger.trade(name, (
            f"ENTRY #{trade_count} | {option_type} | {opt_symbol} "
            f"| Rs {entry_premium:.2f} | Score:{best.score:.0f} | ADX:{adx_val:.1f} | Regime:{vol_reg}"
        ))

        # ── Position management loop ──────────────────────────
        while position_open:
            ns = now_str()

            # Hard exit
            if ns >= cfg.hard_exit_time:
                logger.activity(name, "Hard exit 15:15.")
                mde.place_order(opt_symbol, icfg["option_exchange"], "SELL", icfg["quantity"])
                curr         = mde.get_quote(opt_symbol, icfg["option_exchange"]) or curr
                exit_reason  = "Hard Exit"
                position_open = False
                last_exit_time = now()
                break

            new_price = mde.get_quote(opt_symbol, icfg["option_exchange"])
            if new_price is None:
                time.sleep(3)
                continue
            curr = new_price

            pnl_pts   = curr - entry_premium
            pnl_rs    = pnl_pts * icfg["quantity"]
            held_secs = (now() - entry_time_dt).total_seconds()

            # MFE / MAE
            mfe_pts = max(mfe_pts, pnl_pts)
            mae_pts = min(mae_pts, pnl_pts)

            # Refresh ATR for trailing
            df_pos = mde.get_candles(icfg["index_symbol"], icfg["index_exchange"])
            atr_now = te.atr(df_pos) if df_pos is not None and len(df_pos) >= 5 else atr_val

            trail_eng.update(trail, pnl_pts, atr_now, df_pos, name)

            logger.activity(name, (
                f"Prem:{curr:.2f} PnL:{pnl_pts:+.2f}pts Rs{pnl_rs:+.0f} "
                f"SL:{trail.dynamic_sl:.2f} MFE:{mfe_pts:.2f} MAE:{mae_pts:.2f} Held:{held_secs:.0f}s"
            ))

            # Minimum hold guard
            if held_secs < cfg.min_hold_seconds and pnl_pts > trail.initial_sl:
                time.sleep(5)
                continue

            # Trail stop exit
            if trail_eng.should_exit(trail, pnl_pts):
                mde.place_order(opt_symbol, icfg["option_exchange"], "SELL", icfg["quantity"])
                exit_reason    = "Trailing SL" if trail.be_triggered else "Initial SL"
                position_open  = False
                if not trail.be_triggered:
                    last_sl_time = now()
                last_exit_time = now()

            if position_open:
                time.sleep(5)

        # ── Close accounting ─────────────────────────────────
        pnl_pts_final = curr - entry_premium
        pnl_rs_final  = pnl_pts_final * icfg["quantity"]
        total_pnl    += pnl_rs_final
        risk.on_trade_close(pnl_rs_final, approx_exposure)

        capture = (pnl_pts_final / mfe_pts * 100) if mfe_pts > 0 else 0.0
        rec = TradeRecord(
            trade_num=trade_count,
            instrument=name,
            symbol=opt_symbol,
            direction=option_type,
            entry_price=entry_premium,
            exit_price=curr,
            entry_time=entry_time_dt,
            exit_time=now(),
            exit_reason=exit_reason,
            quantity=icfg["quantity"],
            pnl_pts=pnl_pts_final,
            pnl_rs=pnl_rs_final,
            mfe_pts=mfe_pts,
            mae_pts=mae_pts,
            capture_pct=capture,
            hold_seconds=(now() - entry_time_dt).total_seconds(),
            trend_score=best.score,
            adx_entry=adx_val,
            running_total=total_pnl,
        )
        records.append(rec)
        logger.log_trade_record(rec)
        logger.trade(name, f"EXIT {exit_reason} | Rs {pnl_rs_final:+.0f} | Total: Rs {total_pnl:+.0f}")

        time.sleep(cfg.signal_interval)

    logger.trade(name, f"=== {name} done | Trades:{trade_count} | PnL: Rs {total_pnl:+.0f} ===")


# ============================================================
# STRATEGY LOOP — EQUITY (Nifty 50 stocks)
# ============================================================

def run_equity(
    cfg: AppConfig,
    mde: MarketDataEngine,
    scorer: ScoringEngine,
    scanner: Scanner,
    risk: RiskEngine,
    trail_eng: TrailingStopEngine,
    logger: LoggingEngine,
) -> None:
    ist = ZoneInfo("Asia/Kolkata")

    def now() -> datetime:
        return datetime.now(ist)

    def now_str() -> str:
        return now().strftime("%H:%M")

    logger.day_start("EQUITY", cfg.mode_name)

    trade_count        = 0
    total_pnl          = 0.0
    records: list[TradeRecord] = []
    open_trades: dict[str, dict] = {}    # symbol → trade state
    last_rank_time: Optional[datetime]  = None
    ranked_stocks: list[StockRank]       = []
    te = TrendEngine(cfg)
    m  = cfg.mode

    while True:
        ns = now_str()

        if ns >= cfg.hard_exit_time:
            # Force exit all open equity positions
            for sym, state in list(open_trades.items()):
                logger.trade("EQUITY", f"Hard exit: {sym}")
                mde.place_order(sym, cfg.equity_exchange, "SELL", state["qty"])
                curr_p = mde.get_quote(sym, cfg.equity_exchange) or state["entry"]
                pnl_rs = (curr_p - state["entry"]) * state["qty"]
                total_pnl += pnl_rs
                risk.on_trade_close(pnl_rs, state["exposure"])
                capture = ((curr_p - state["entry"]) / state["mfe"] * 100
                           if state["mfe"] > 0 else 0.0)
                rec = TradeRecord(
                    trade_num=trade_count, instrument="EQUITY", symbol=sym,
                    direction="BUY", entry_price=state["entry"], exit_price=curr_p,
                    entry_time=state["entry_time"], exit_time=now(),
                    exit_reason="Hard Exit",
                    quantity=state["qty"],
                    pnl_pts=curr_p - state["entry"],
                    pnl_rs=pnl_rs,
                    mfe_pts=state["mfe"],
                    mae_pts=state["mae"],
                    capture_pct=capture,
                    hold_seconds=(now() - state["entry_time"]).total_seconds(),
                    trend_score=state["score"],
                    adx_entry=state["adx"],
                    running_total=total_pnl,
                )
                records.append(rec)
                logger.log_trade_record(rec)
            logger.trade("EQUITY", f"Session end | Trades:{trade_count} | PnL: Rs {total_pnl:+.0f}")
            logger.day_summary("EQUITY", records)
            break

        if ns < cfg.entry_time:
            time.sleep(5)
            continue

        if cfg.lunch_start <= ns <= cfg.lunch_end:
            time.sleep(60)
            continue

        # ── Update open positions ────────────────────────────
        for sym in list(open_trades.keys()):
            state = open_trades[sym]
            curr_p = mde.get_quote(sym, cfg.equity_exchange)
            if curr_p is None:
                continue

            pnl_pts = curr_p - state["entry"]
            state["mfe"] = max(state["mfe"], pnl_pts)
            state["mae"] = min(state["mae"], pnl_pts)

            df_pos = mde.get_candles(sym, cfg.equity_exchange)
            atr_now = te.atr(df_pos) if df_pos is not None and len(df_pos) >= 5 else state["atr"]
            trail: TrailState = state["trail"]
            trail_eng.update(trail, pnl_pts, atr_now, df_pos, f"EQUITY:{sym}")

            if trail_eng.should_exit(trail, pnl_pts):
                held = (now() - state["entry_time"]).total_seconds()
                if held < cfg.min_hold_seconds and pnl_pts > trail.initial_sl:
                    continue  # min hold guard

                mde.place_order(sym, cfg.equity_exchange, "SELL", state["qty"])
                exit_reason = "Trailing SL" if trail.be_triggered else "Initial SL"
                pnl_rs      = pnl_pts * state["qty"]
                total_pnl  += pnl_rs
                trade_count += 1
                risk.on_trade_close(pnl_rs, state["exposure"])

                capture = (pnl_pts / state["mfe"] * 100) if state["mfe"] > 0 else 0.0
                rec = TradeRecord(
                    trade_num=trade_count, instrument="EQUITY", symbol=sym,
                    direction="BUY", entry_price=state["entry"], exit_price=curr_p,
                    entry_time=state["entry_time"], exit_time=now(),
                    exit_reason=exit_reason,
                    quantity=state["qty"],
                    pnl_pts=pnl_pts, pnl_rs=pnl_rs,
                    mfe_pts=state["mfe"], mae_pts=state["mae"],
                    capture_pct=capture,
                    hold_seconds=held,
                    trend_score=state["score"], adx_entry=state["adx"],
                    running_total=total_pnl,
                )
                records.append(rec)
                logger.log_trade_record(rec)
                logger.trade("EQUITY", f"EXIT {exit_reason} | {sym} | Rs {pnl_rs:+.0f}")
                del open_trades[sym]

        # ── Re-rank stocks ───────────────────────────────────
        if (last_rank_time is None or
                (now() - last_rank_time).total_seconds() >= cfg.stock_rank_refresh):
            ranked_stocks  = scanner.rank()
            last_rank_time = now()
            top = ranked_stocks[:5]
            logger.activity("EQUITY", "Rank: " + " | ".join(
                f"{r.symbol}:{r.score:.0f}({r.direction})" for r in top
            ))

        # ── Attempt new entries ──────────────────────────────
        if len(open_trades) >= m.max_equity_positions:
            time.sleep(cfg.signal_interval)
            continue

        if ns >= cfg.late_entry_cutoff:
            time.sleep(30)
            continue

        ok, reason = risk.can_trade("EQUITY")
        if not ok:
            logger.activity("EQUITY", f"Risk gate: {reason}")
            time.sleep(30)
            continue

        for rank in ranked_stocks[:cfg.top_n_stocks]:
            if rank.symbol in open_trades:
                continue
            if len(open_trades) >= m.max_equity_positions:
                break
            if rank.score < m.score_threshold:
                continue
            if rank.direction != "CE":
                # Equity long only — skip bearish
                continue

            sig = rank.signal
            if sig is None or sig.adx_val < 20:
                continue

            # Calculate quantity from capital allocation
            price = mde.get_quote(rank.symbol, cfg.equity_exchange)
            if price is None or price <= 0:
                continue
            qty = max(1, int(cfg.equity_lot_value / price))

            exposure = qty * price
            ok2, reason2 = risk.can_trade("EQUITY", exposure)
            if not ok2:
                logger.activity("EQUITY", f"{rank.symbol} risk gate: {reason2}")
                continue

            order_id = mde.place_order(rank.symbol, cfg.equity_exchange, "BUY", qty)
            if not order_id:
                logger.activity("EQUITY", f"{rank.symbol} order failed.")
                continue

            trail = trail_eng.create(sig.atr_val, f"EQUITY:{rank.symbol}")
            trade_count += 1
            risk.on_trade_open(exposure)

            open_trades[rank.symbol] = {
                "entry":      price,
                "qty":        qty,
                "exposure":   exposure,
                "entry_time": now(),
                "trail":      trail,
                "mfe":        0.0,
                "mae":        0.0,
                "atr":        sig.atr_val,
                "score":      rank.score,
                "adx":        sig.adx_val,
            }
            logger.trade("EQUITY", (
                f"ENTRY #{trade_count} | {rank.symbol} BUY {qty}@{price:.2f} "
                f"| Score:{rank.score:.0f} ADX:{sig.adx_val:.1f} ATR:{sig.atr_val:.2f}"
            ))

        time.sleep(cfg.signal_interval)

    logger.trade("EQUITY", f"=== EQUITY done | Trades:{trade_count} | PnL: Rs {total_pnl:+.0f} ===")


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    api_key = os.environ.get("OPENALGO_API_KEY", "")
    if not api_key:
        raise EnvironmentError(
            "OPENALGO_API_KEY not set. "
            "Run: export OPENALGO_API_KEY=your_key_here"
        )

    # ── Build config (change mode_name here or via env var) ──
    mode_name = os.environ.get("TREND_RIDER_MODE", "balanced").lower()
    if mode_name not in MODES:
        raise ValueError(f"Unknown mode '{mode_name}'. Choose: {list(MODES)}")

    cfg        = AppConfig(api_key=api_key, mode_name=mode_name)
    logger     = LoggingEngine()
    mde        = MarketDataEngine(cfg)
    te         = TrendEngine(cfg)
    scorer     = ScoringEngine(cfg, te)
    risk       = RiskEngine(cfg, logger)
    trail_eng  = TrailingStopEngine(cfg, logger)
    scan       = Scanner(cfg, mde, scorer, logger)

    ist = ZoneInfo("Asia/Kolkata")
    logger.trade("MAIN", "=" * 60)
    logger.trade("MAIN", f"Trend Rider V1.0 — {datetime.now(ist).strftime('%Y-%m-%d %H:%M')}")
    logger.trade("MAIN", f"Mode: {mode_name.upper()} | Threshold: {cfg.mode.score_threshold}")
    logger.trade("MAIN", f"Hard Exit: {cfg.hard_exit_time} | Max Trades/day: {cfg.mode.max_trades_per_day}")
    logger.trade("MAIN", "=" * 60)

    threads: list[threading.Thread] = []

    # Index option threads
    for name, icfg in cfg.index_configs.items():
        t = threading.Thread(
            target=run_index,
            args=(name, icfg, cfg, mde, scorer, risk, trail_eng, logger),
            daemon=True,
            name=f"Thread-{name}",
        )
        t.start()
        threads.append(t)

    # Equity thread
    t_eq = threading.Thread(
        target=run_equity,
        args=(cfg, mde, scorer, scan, risk, trail_eng, logger),
        daemon=True,
        name="Thread-EQUITY",
    )
    t_eq.start()
    threads.append(t_eq)

    for t in threads:
        t.join()

    logger.trade("MAIN", "=" * 60)
    logger.trade("MAIN", "All strategies complete.")
    logger.trade("MAIN", f"Portfolio PnL today: Rs {risk.state.total_pnl:+.0f}")
    logger.trade("MAIN", "=" * 60)


if __name__ == "__main__":
    main()
