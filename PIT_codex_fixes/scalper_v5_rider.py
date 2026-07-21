"""
Scalper V5 — "Rider" Edition
============================================================
Same breakout ENTRY engine as the V4.3 scalper, but the EXIT is rebuilt
to RIDE the move like the trend rider instead of scalping a fixed target.

WHY V5 EXISTS
-------------
V4.3 kept sitting out clean, sustained breakouts (15+ min of one-way green)
because:
  1. ADX ceiling (35) rejected exactly the strong trends a momentum strategy
     is supposed to ride.
  2. A small fixed target (NIFTY +8 / SENSEX +25) capped winners.
  3. A tight high-water drop (3 / 7 pts) bailed on the first pullback.

V5 fixes the philosophy, not just the numbers:
  • ENTRY  : ORB breakout + EMA9>EMA21 + price vs VWAP + ADX floor.
             NO ADX ceiling — strong trends are taken.
  • RIDE   : no fixed target. Hold while the underlying trend is intact
             (spot EMA9 >= EMA21 for CE, <= for PE).
  • EXIT   : whichever fires first —
               - Initial hard SL (capital protection)
               - Trailing SL with a WIDE giveback from peak (ratchets up only)
               - "No Momentum Exit" when spot EMA9/EMA21 flips against us
               - Hard time exit
  • Logs as strategy "scalper_v5" → its own folder + master CSV + DB rows,
    so it runs ALONGSIDE the V4.3 scalper for a clean A/B comparison.

NIFTY : 65 qty | Tuesday expiry  | NFO
SENSEX: 20 qty | Thursday expiry | BFO

Run:
    export OPENALGO_API_KEY=your_key_here
    uv run python3 scalper_v5_rider.py
"""

import os
import sys
import time
import threading
from collections import deque
from datetime import datetime

import pytz

# Reuse the proven, side-effect-free helpers from the V4.3 module:
# market data, indicators, option-chain analysis, fill verification.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scalper_pro_v4_3 import (  # noqa: E402
    api_post,
    get_quote,
    fetch_candles,
    fetch_option_chain,
    calc_ema, calc_vwap, calc_atr, calc_adx,
    get_orb,
    analyze_option_chain, get_itm_strike,
    get_atm_strike, get_expiry, build_option_symbol,
    verify_fill,
    get_momentum_state,
)
from trade_logger import TradeLogger  # noqa: E402

# ============================================================
# CONFIG
# ============================================================

API_KEY  = os.environ.get("OPENALGO_API_KEY", "")
if not API_KEY:
    raise EnvironmentError(
        "OPENALGO_API_KEY environment variable is not set. "
        "Export it before running: export OPENALGO_API_KEY=your_key_here"
    )
BASE_URL = "http://127.0.0.1:5000/api/v1"

STRATEGY_TAG = "Scalper V5 Rider"   # tag sent to OpenAlgo on each order

ENTRY_TIME            = "09:20"
EXIT_TIME             = "15:25"   # square off the ride before MIS auto-exit
LATE_ENTRY_CUTOFF     = "15:00"   # no NEW entries after this; existing rides continue

MAX_DAILY_LOSS        = -5000
MAX_TRADES            = 5         # rides are longer; a few re-entries on continuation
SIGNAL_INTERVAL       = 10        # seconds between scans while flat
MONITOR_INTERVAL      = 5         # seconds between checks while in a position
MOM_CHECK_INTERVAL    = 20        # seconds between spot-trend (EMA) re-evaluations
STALE_QUOTE_EXIT_SECS = 25        # if no valid quote this long while in a position,
                                  # fire a protective market exit — never sit blind
                                  # through a data blackout (see trade #9, 2026-06-23)
COOLDOWN_AFTER_SL     = 180       # short — re-enter a continuing trend after a shakeout
COOLDOWN_AFTER_EXIT   = 120       # after a trailing / momentum exit
MIN_HOLD_SECONDS      = 60        # don't let one noisy EMA flip kick us out instantly
CANDLE_CONFIRM_COUNT  = 2         # 2 × 10s = 20s of consistent breakout before entry
LUNCH_SKIP            = False     # V5 does not sit out lunch — trends can run any time

# ── Circuit breaker (stops the afternoon-chop give-back) ──
MAX_CONSEC_LOSSES     = 3         # halt ALL new entries after this many losses in a row
PROFIT_LOCK_ARM       = 2000      # profit-lock only arms once the day's peak P&L >= this
PROFIT_LOCK_GIVEBACK  = 1500      # ...then halt if P&L retraces this much from that peak

IST     = pytz.timezone("Asia/Kolkata")


class DayRisk:
    """Shared across the NIFTY and SENSEX threads. Tracks COMBINED realised P&L,
    running peak, and consecutive losses, and latches a day-wide halt when the
    circuit breaker trips. A halt blocks NEW entries only — any open position
    still rides and exits on its own stop/trail/momentum logic."""

    def __init__(self):
        self._lock = threading.Lock()
        self.realized = 0.0
        self.peak = 0.0
        self.consec_losses = 0
        self.halted = False
        self.reason = ""

    def record(self, pnl_rs):
        """Call once per closed trade (from finalize)."""
        with self._lock:
            self.realized += pnl_rs
            self.peak = max(self.peak, self.realized)
            self.consec_losses = (self.consec_losses + 1) if pnl_rs <= 0 else 0
            if not self.halted:
                if self.consec_losses >= MAX_CONSEC_LOSSES:
                    self.halted = True
                    self.reason = f"{self.consec_losses} consecutive losses"
                elif self.peak >= PROFIT_LOCK_ARM and (self.peak - self.realized) >= PROFIT_LOCK_GIVEBACK:
                    self.halted = True
                    self.reason = (f"profit-lock — gave back Rs {self.peak - self.realized:.0f} "
                                   f"from peak Rs {self.peak:.0f}")

    def status(self):
        with self._lock:
            return self.halted, self.reason


day_risk = DayRisk()

# ============================================================
# INSTRUMENT CONFIG  (ride parameters, no fixed target, no ADX ceiling)
# ============================================================

INSTRUMENTS = {
    "NIFTY": {
        "index_symbol":      "NIFTY",
        "index_exchange":    "NSE_INDEX",
        "option_exchange":   "NFO",
        "quantity":          65,
        "strike_interval":   50,
        "expiry_day":        1,            # Tuesday
        "adx_threshold":     22,           # trend must exist; NO upper ceiling
        "orb_candles":       3,
        "orb_buffer":        20,
        # ── ride parameters (in option points) ──
        "initial_sl_pts":    7,            # hard stop from entry
        "be_trigger_pts":    6,            # move SL to breakeven once +6
        "trail_activate_pts": 8,           # start trailing once peak hits +8
        "trail_giveback_pts": 6,           # exit if premium falls 6 from peak (wide)
    },
    "SENSEX": {
        "index_symbol":      "SENSEX",
        "index_exchange":    "BSE_INDEX",
        "option_exchange":   "BFO",
        "quantity":          20,
        "strike_interval":   100,
        "expiry_day":        3,            # Thursday
        "adx_threshold":     22,
        "orb_candles":       3,
        "orb_buffer":        60,
        # ── ride parameters (in option points) ──
        "initial_sl_pts":    18,
        "be_trigger_pts":    14,
        "trail_activate_pts": 18,
        "trail_giveback_pts": 14,
    },
}

# ============================================================
# LOGGING  — same TradeLogger pipeline, distinct strategy name
#   logs/YYYY-MM-DD/scalper_v5/{trades,activity}.log + trades.csv
#   logs/scalper_v5_master.csv   + trading.db rows (strategy=scalper_v5)
# ============================================================

_TL = TradeLogger("scalper_v5")


def get_ist_now():
    return datetime.now(IST)


def get_time_str():
    return get_ist_now().strftime("%H:%M")


def tlog(text):
    _TL.raw_trade(text)


def alog(text):
    print(text)
    _TL._append(_TL._activity_log_path(), text)


def log(name, msg):
    _TL.activity(name, msg)


def tradelog(name, msg):
    _TL.trade(name, msg)


def log_day_start(name):
    sep = "#" * 60
    tlog("")
    tlog(sep)
    tlog(f"# {name} V5 RIDER Started — {get_ist_now().strftime('%Y-%m-%d')}")
    tlog(f"# Entry: {ENTRY_TIME} | Last entry: {LATE_ENTRY_CUTOFF} | "
         f"Exit: {EXIT_TIME} | Max Trades: {MAX_TRADES}")
    tlog(sep)
    tlog("")
    alog(f"# {name} V5 Activity Log — {get_ist_now().strftime('%Y-%m-%d')}")
    alog("")


# ============================================================
# ORDER HELPERS (own strategy tag so V5 is distinct in the analyzer)
# ============================================================

def place_order(symbol, exchange, action, quantity):
    data = api_post("placeorder", {
        "symbol":    symbol,
        "exchange":  exchange,
        "action":    action,
        "quantity":  quantity,
        "price":     0,
        "pricetype": "MARKET",
        "product":   "MIS",
        "strategy":  STRATEGY_TAG,
    })
    if data and data.get("status") == "success":
        return data.get("orderid")
    return None


def execute_market_order(symbol, exchange, action, quantity, fallback_price=None):
    order_id = place_order(symbol, exchange, action, quantity)
    if not order_id:
        return None
    return verify_fill(order_id, fallback_price=fallback_price, fallback_qty=quantity)


# ============================================================
# TRADE LOG CARD  (rider-flavoured)
# ============================================================

def log_trade(
    name, trade_num, option_type, option_symbol,
    entry_premium, exit_premium, exit_reason,
    entry_time, exit_time,
    spot, orb_high, orb_low,
    ema9, ema21, vwap, adx, atr,
    max_pain_strike, max_pain_bias, oi_bias,
    mfe_pts, mae_pts,
    be_triggered, be_trigger_time, be_trigger_pnl,
    trail_log,
    high_water_mark,
    exit_spot, exit_ema9, exit_ema21, exit_vwap, exit_adx,
    dynamic_sl_at_exit, running_total,
):
    cfg     = INSTRUMENTS[name]
    qty     = cfg["quantity"]
    init_sl = cfg["initial_sl_pts"]
    pnl_pts = exit_premium - entry_premium
    pnl_rs  = pnl_pts * qty
    sep     = "=" * 60

    capture_rate  = max(0.0, pnl_pts / mfe_pts * 100) if mfe_pts > 0 else 0
    exit_momentum = get_momentum_state(exit_ema9, exit_ema21, exit_adx)

    lines = [
        "",
        sep,
        f"TRADE #{trade_num} | {name} V5 | {entry_time.strftime('%Y-%m-%d')} | {entry_time.strftime('%H:%M:%S')}",
        sep,
        "",
        "── MARKET CONDITIONS AT ENTRY ──────────────────────",
        f"  Spot Price     : {spot:.2f}",
        f"  ORB High       : {orb_high:.2f}",
        f"  ORB Low        : {orb_low:.2f}",
        f"  EMA9           : {ema9:.2f}",
        f"  EMA21          : {ema21:.2f}",
        f"  VWAP           : {vwap:.2f}",
        f"  ADX            : {adx:.1f} ({'Trending' if adx >= 20 else 'Sideways'})",
        f"  ATR            : {atr:.2f}",
        f"  Max Pain       : {max_pain_strike} (Bias: {max_pain_bias})",
        f"  OI Bias        : {oi_bias}",
        f"  Momentum       : {get_momentum_state(ema9, ema21, adx)}",
        "",
        "── ENTRY (RIDER MODE) ───────────────────────────────",
        f"  Signal         : {option_type}",
        f"  Symbol         : {option_symbol}",
        "  Strike Type    : ITM (1 strike in the money)",
        f"  Entry Premium  : Rs {entry_premium:.2f}",
        f"  Initial SL     : Rs {entry_premium - init_sl:.2f} (-{init_sl}pts = Rs {init_sl * qty:.0f})",
        "  Target         : NONE — rides until trend flips or trailing SL",
        f"  Trail Giveback : {cfg['trail_giveback_pts']}pts from peak (after +{cfg['trail_activate_pts']}pts)",
        f"  Entry Time     : {entry_time.strftime('%H:%M:%S')}",
        "",
        "── MFE / MAE ANALYTICS ─────────────────────────────",
        f"  MFE (Best point): +{mfe_pts:.1f}pts = Rs {mfe_pts * qty:.0f}",
        f"  MAE (Worst point): {mae_pts:.1f}pts = Rs {mae_pts * qty:.0f}",
        f"  Profit Captured : {pnl_pts:.1f}pts / {mfe_pts:.1f}pts = {capture_rate:.0f}%",
        "",
        "── BREAK-EVEN AUDIT ─────────────────────────────────",
    ]

    if be_triggered:
        lines += [
            "  Break-even     : YES — Triggered",
            f"  Triggered At   : {be_trigger_pnl:.1f}pts",
            f"  Time           : {be_trigger_time}",
            f"  SL moved       : -{init_sl}pts → 0",
        ]
    else:
        lines += [
            "  Break-even     : NO — Never reached",
            f"  Highest Profit : {mfe_pts:.1f}pts (needed {cfg['be_trigger_pts']}pts to trigger BE)",
        ]

    lines += ["", "── TRAILING SL AUDIT ────────────────────────────────"]
    if trail_log:
        lines.append(f"  Trailing       : YES — {len(trail_log)} update(s)")
        for i, t in enumerate(trail_log, 1):
            lines.append(f"  Trail #{i}       : P&L {t['pnl']:.1f}pts | SL {t['old_sl']:.1f} → {t['new_sl']:.1f}pts | {t['time']}")
        lines.append(f"  Final Locked   : +{trail_log[-1]['new_sl']:.1f}pts = Rs {trail_log[-1]['new_sl'] * qty:.0f}")
    else:
        lines.append("  Trailing       : NO — Never triggered")

    lines += [
        "",
        "── RIDE / HIGH WATER AUDIT ──────────────────────────",
        f"  Peak Profit    : +{high_water_mark:.1f}pts = Rs {high_water_mark * qty:.0f}",
        f"  Exit Profit    : {pnl_pts:+.1f}pts = Rs {pnl_rs:.0f}",
        f"  Giveback       : {high_water_mark - pnl_pts:.1f}pts = Rs {(high_water_mark - pnl_pts) * qty:.0f}",
        f"  Exit Reason    : {exit_reason}",
        "",
        "── EXIT CONTEXT ─────────────────────────────────────",
        f"  Exit Time      : {exit_time.strftime('%H:%M:%S')}",
        f"  Exit Premium   : Rs {exit_premium:.2f}",
        f"  Spot at Exit   : {exit_spot:.2f}",
        f"  EMA9 at Exit   : {exit_ema9:.2f}",
        f"  EMA21 at Exit  : {exit_ema21:.2f}",
        f"  VWAP at Exit   : {exit_vwap:.2f}",
        f"  ADX at Exit    : {exit_adx:.1f}",
        f"  Momentum State : {exit_momentum}",
        f"  SL at Exit     : {dynamic_sl_at_exit:.1f}pts",
        "",
        "── RESULT ───────────────────────────────────────────",
        f"  P&L Points     : {pnl_pts:+.1f}pts",
        f"  P&L Rupees     : {'+ ' if pnl_rs >= 0 else '- '}Rs {abs(pnl_rs):.0f}",
        f"  RUNNING TOTAL  : Rs {running_total:.0f}",
        "",
    ]

    diag_tag, diag_msg = _TL.diagnose(
        exit_reason=exit_reason,
        pnl_pts=pnl_pts, mfe_pts=mfe_pts, mae_pts=mae_pts,
        be_triggered=be_triggered, trail_count=len(trail_log) if trail_log else 0,
        adx_entry=adx, adx_exit=exit_adx,
        high_water=high_water_mark,
    )
    lines += [
        "── TRADE DIAGNOSIS ──────────────────────────────────",
        f"  Tag            : {diag_tag}",
        f"  Analysis       : {diag_msg}",
        sep,
        "",
    ]
    tlog("\n".join(lines))

    # ── Structured CSV + DB row ──────────────────────────────
    try:
        trail_count = len(trail_log) if trail_log else 0
        _TL.csv_row({
            "date":          entry_time.strftime("%Y-%m-%d"),
            "entry_time":    entry_time.strftime("%H:%M:%S"),
            "exit_time":     exit_time.strftime("%H:%M:%S"),
            "strategy":      "scalper_v5",
            "instrument":    name,
            "direction":     option_type,
            "symbol":        option_symbol,
            "strike_type":   "ITM",
            "entry_price":   round(entry_premium, 2),
            "exit_price":    round(exit_premium, 2),
            "quantity":      qty,
            "pnl_pts":       round(pnl_pts, 2),
            "pnl_rs":        round(pnl_rs, 0),
            "mfe_pts":       round(mfe_pts, 2),
            "mae_pts":       round(mae_pts, 2),
            "capture_pct":   round(max(0.0, capture_rate), 1),
            "exit_reason":   exit_reason,
            "hold_seconds":  int((exit_time - entry_time).total_seconds()),
            "adx_entry":     round(adx, 1),
            "atr_entry":     round(atr, 2),
            "ema9_entry":    round(ema9, 2),
            "ema21_entry":   round(ema21, 2),
            "vwap_entry":    round(vwap, 2),
            "orb_high":      round(orb_high, 2),
            "orb_low":       round(orb_low, 2),
            "max_pain":      max_pain_strike,
            "max_pain_bias": max_pain_bias,
            "oi_bias":       oi_bias,
            "momentum":      get_momentum_state(ema9, ema21, adx),
            "be_triggered":  "YES" if be_triggered else "NO",
            "trail_count":   trail_count,
            "high_water_pts": round(high_water_mark, 2),
            "trend_score":   "",
            "running_total": round(running_total, 0),
        })
    except Exception as e:
        print(f"[CSV ERROR scalper_v5] {e}")


def log_day_summary(name, trade_count, total_pnl, trades_detail):
    if not trades_detail:
        tlog(f"\n[{name}] No trades taken today.\n")
        return

    wins   = [t for t in trades_detail if t["pnl"] > 0]
    losses = [t for t in trades_detail if t["pnl"] <= 0]
    win_rate = (len(wins) / trade_count * 100) if trade_count > 0 else 0

    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss   = abs(sum(t["pnl"] for t in losses))
    pf = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

    avg_winner = (gross_profit / len(wins)) if wins else 0
    avg_loser  = (gross_loss / len(losses)) if losses else 0
    best  = max(t["pnl"] for t in trades_detail)
    worst = min(t["pnl"] for t in trades_detail)

    avg_mfe = sum(t["mfe"] for t in trades_detail) / len(trades_detail)
    avg_mae = sum(t["mae"] for t in trades_detail) / len(trades_detail)
    avg_capture = sum(t["capture"] for t in trades_detail) / len(trades_detail)

    exit_counts = {}
    for t in trades_detail:
        exit_counts[t["exit_reason"]] = exit_counts.get(t["exit_reason"], 0) + 1

    sep = "=" * 60
    lines = [
        "",
        sep,
        f"DAY SUMMARY — {name} V5 — {get_ist_now().strftime('%Y-%m-%d')}",
        sep,
        "",
        "── PERFORMANCE ──────────────────────────────────────",
        f"  Total Trades   : {trade_count}",
        f"  Wins           : {len(wins)}",
        f"  Losses         : {len(losses)}",
        f"  Win Rate       : {win_rate:.1f}%",
        f"  Gross Profit   : Rs {gross_profit:.0f}",
        f"  Gross Loss     : Rs {gross_loss:.0f}",
        f"  Profit Factor  : {pf:.2f}",
        f"  Average Winner : Rs {avg_winner:.0f}",
        f"  Average Loser  : Rs {avg_loser:.0f}",
        f"  Best Trade     : Rs {best:.0f}",
        f"  Worst Trade    : Rs {worst:.0f}",
        f"  Final P&L      : Rs {total_pnl:.0f}",
        "",
        "── RIDE QUALITY (MFE / MAE) ─────────────────────────",
        f"  Average MFE    : {avg_mfe:.1f}pts",
        f"  Average MAE    : {avg_mae:.1f}pts",
        f"  Avg Capture    : {avg_capture:.0f}% of MFE",
        "",
        "── EXIT BREAKDOWN ───────────────────────────────────",
    ]
    for reason, count in exit_counts.items():
        lines.append(f"  {reason:<20}: {count} trade(s)")
    lines += [sep, ""]
    tlog("\n".join(lines))


# ============================================================
# SPOT TREND SNAPSHOT (for the ride / momentum-death check)
# ============================================================

def spot_trend(cfg):
    """Return (ema9, ema21, vwap, adx, close) for the index, or None."""
    df = fetch_candles(cfg, interval="1m")
    if df is None or len(df) < 20:
        return None
    try:
        return (
            calc_ema(df, 9),
            calc_ema(df, 21),
            calc_vwap(df),
            calc_adx(df),
            float(df.iloc[-1]["close"]),
        )
    except Exception:
        return None


def momentum_alive(option_type, ema9, ema21):
    """Trend still supports the position?  CE rides while EMA9>=EMA21; PE while EMA9<=EMA21."""
    if option_type == "CE":
        return ema9 >= ema21
    return ema9 <= ema21


# ============================================================
# STRATEGY
# ============================================================

def run_instrument(name, cfg):
    log(name, f"V5 RIDER Started | Qty:{cfg['quantity']} "
              f"InitSL:{cfg['initial_sl_pts']}pts Trail:{cfg['trail_giveback_pts']}pts | NO target, NO ADX ceiling")

    total_pnl        = 0
    trade_count      = 0
    last_sl_time     = None
    last_exit_time   = None
    signal_history   = deque(maxlen=CANDLE_CONFIRM_COUNT)
    expiry_date      = get_expiry(cfg)
    chain_data       = None
    last_chain_fetch = None
    trades_detail    = []

    log(name, f"Expiry: {expiry_date}")
    log_day_start(name)

    while True:
        now_str = get_time_str()
        now     = get_ist_now()

        if now_str >= EXIT_TIME:
            tradelog(name, f"Session ended | Trades:{trade_count} | P&L: Rs {total_pnl:.0f}")
            log_day_summary(name, trade_count, total_pnl, trades_detail)
            break

        if now_str < ENTRY_TIME:
            time.sleep(5)
            continue

        if total_pnl <= MAX_DAILY_LOSS:
            tradelog(name, f"Daily loss limit hit (Rs {total_pnl:.0f}). Stopping.")
            log_day_summary(name, trade_count, total_pnl, trades_detail)
            break

        if trade_count >= MAX_TRADES:
            tradelog(name, f"Max {MAX_TRADES} trades done. Stopping.")
            log_day_summary(name, trade_count, total_pnl, trades_detail)
            break

        # Circuit breaker — shared across both instrument threads. Once the
        # combined book trips (consecutive losses or profit give-back), no new
        # entries for the rest of the day. This is the afternoon-chop guard.
        halted, halt_reason = day_risk.status()
        if halted:
            tradelog(name, f"CIRCUIT BREAKER — {halt_reason}. Standing down for the day.")
            log_day_summary(name, trade_count, total_pnl, trades_detail)
            break

        if now_str >= LATE_ENTRY_CUTOFF:
            log(name, f"Past {LATE_ENTRY_CUTOFF} — no new entries.")
            time.sleep(30)
            continue

        if last_sl_time:
            elapsed = (get_ist_now() - last_sl_time).seconds
            if elapsed < COOLDOWN_AFTER_SL:
                log(name, f"SL cooldown: {COOLDOWN_AFTER_SL - elapsed}s remaining.")
                time.sleep(20)
                continue

        if last_exit_time:
            elapsed = (get_ist_now() - last_exit_time).seconds
            if elapsed < COOLDOWN_AFTER_EXIT:
                log(name, f"Exit cooldown: {COOLDOWN_AFTER_EXIT - elapsed}s remaining.")
                time.sleep(20)
                continue

        if LUNCH_SKIP and "11:45" <= now_str <= "13:15":
            log(name, "Lunch zone — skipping.")
            time.sleep(60)
            continue

        # Option chain (soft context only) every 15 minutes
        if last_chain_fetch is None or (now - last_chain_fetch).seconds >= 900:
            log(name, "Fetching option chain...")
            chain_data       = fetch_option_chain(cfg, expiry_date)
            last_chain_fetch = now
            if chain_data:
                log(name, f"Option chain fetched. ATM: {chain_data.get('atm_strike')}")

        df = fetch_candles(cfg, interval="1m")
        if df is None or len(df) < 20:
            log(name, "Not enough candles. Waiting...")
            time.sleep(15)
            continue

        try:
            ema9     = calc_ema(df, 9)
            ema21    = calc_ema(df, 21)
            vwap     = calc_vwap(df)
            atr      = calc_atr(df)
            adx      = calc_adx(df)
            orb_high, orb_low = get_orb(df, cfg["orb_candles"])
            latest   = float(df.iloc[-1]["close"])
            atm      = get_atm_strike(latest, cfg["strike_interval"])
        except Exception as e:
            log(name, f"Indicator error: {e}")
            time.sleep(10)
            continue

        max_pain_strike, max_pain_bias, oi_bias = analyze_option_chain(
            chain_data, atm, cfg["strike_interval"]
        )

        log(name, (
            f"Price:{latest:.1f} EMA9:{ema9:.1f} EMA21:{ema21:.1f} "
            f"VWAP:{vwap:.1f} ADX:{adx:.1f} | "
            f"MaxPain:{max_pain_strike} Bias:{max_pain_bias} OI:{oi_bias}"
        ))

        # ── ENTRY GATE (no ADX ceiling) ──────────────────────
        # Guard against nan ADX: nan < threshold is False in Python, which would
        # let the strategy trade blind before enough candles exist for a valid ADX.
        if adx != adx:  # nan check (nan is the only value not equal to itself)
            log(name, "ADX unavailable (nan) — not enough data yet. Skipping.")
            signal_history.clear()
            time.sleep(20)
            continue
        if adx < cfg["adx_threshold"]:
            log(name, f"Low ADX ({adx:.1f}) — no trend yet. Skipping.")
            signal_history.clear()
            time.sleep(20)
            continue

        raw_signal = None
        buf = cfg["orb_buffer"]
        if latest > orb_high + buf and ema9 > ema21 and latest > vwap:
            raw_signal = "CE"
        elif latest < orb_low - buf and ema9 < ema21 and latest < vwap:
            raw_signal = "PE"

        if not raw_signal:
            log(name, f"No setup | ORB+buf H:{orb_high+buf:.0f} L:{orb_low-buf:.0f}")
            signal_history.clear()
            time.sleep(SIGNAL_INTERVAL)
            continue

        signal_history.append(raw_signal)
        if len(signal_history) < CANDLE_CONFIRM_COUNT:
            log(name, f"Breakout confirm: {len(signal_history)}/{CANDLE_CONFIRM_COUNT}")
            time.sleep(SIGNAL_INTERVAL)
            continue

        if not all(s == raw_signal for s in signal_history):
            log(name, "Breakout not consistent. Waiting...")
            time.sleep(SIGNAL_INTERVAL)
            continue

        option_type = raw_signal
        log(name, f"BREAKOUT confirmed: {option_type} ({CANDLE_CONFIRM_COUNT} polls) — riding.")

        # OI / max pain are soft context only — never block (logged for review)
        if max_pain_bias and max_pain_bias != option_type:
            log(name, f"(soft) Max pain {max_pain_bias} vs {option_type} — proceeding.")
        if oi_bias and oi_bias != option_type:
            log(name, f"(soft) OI bias {oi_bias} vs {option_type} — proceeding.")

        entry_snapshot = {
            "spot": latest, "orb_high": orb_high, "orb_low": orb_low,
            "ema9": ema9, "ema21": ema21, "vwap": vwap,
            "adx": adx, "atr": atr,
            "max_pain_strike": max_pain_strike,
            "max_pain_bias": max_pain_bias, "oi_bias": oi_bias,
        }

        itm_strike    = get_itm_strike(option_type, atm, cfg["strike_interval"])
        option_symbol = build_option_symbol(name, cfg, itm_strike, option_type)
        log(name, f"ITM Symbol: {option_symbol} (ATM:{atm} ITM:{itm_strike})")

        entry_quote = get_quote(option_symbol, cfg["option_exchange"])
        if entry_quote is None:
            log(name, "Could not fetch premium. Retrying.")
            time.sleep(10)
            continue

        fill = execute_market_order(
            option_symbol, cfg["option_exchange"], "BUY",
            cfg["quantity"], fallback_price=entry_quote
        )
        if not fill:
            log(name, "Entry order failed/rejected.")
            time.sleep(10)
            continue
        entry = fill

        trade_count  += 1
        entry_time_dt = get_ist_now()
        signal_history.clear()
        tradelog(name, f"ENTRY #{trade_count} | {option_type} ITM | Rs {entry} | {option_symbol} | RIDING")

        # ── ride state ───────────────────────────────────────
        position_open   = True
        dynamic_sl      = -cfg["initial_sl_pts"]    # in pts relative to entry
        be_done         = False
        trail_active    = False
        high_water_mark = 0.0
        mfe_pts         = 0.0
        mae_pts         = 0.0
        be_trigger_time = None
        be_trigger_pnl  = 0.0
        trail_log       = []
        curr            = entry
        last_mom_check  = get_ist_now()
        last_quote_time = get_ist_now()   # for stale-quote protection
        mom_dead        = False
        # exit context (refreshed by the momentum check)
        ex_ema9, ex_ema21, ex_vwap, ex_adx, ex_spot = ema9, ema21, vwap, adx, latest

        def finalize(exit_reason, pnl_pts, pnl_rs, exit_premium):
            day_risk.record(pnl_rs)   # feed the shared circuit breaker
            capture = (pnl_pts / mfe_pts * 100) if mfe_pts > 0 else 0
            trades_detail.append({
                "pnl": pnl_rs, "exit_reason": exit_reason,
                "mfe": mfe_pts, "mae": mae_pts, "capture": capture,
            })
            log_trade(
                name, trade_count, option_type, option_symbol,
                entry, exit_premium, exit_reason,
                entry_time_dt, get_ist_now(),
                entry_snapshot["spot"], entry_snapshot["orb_high"], entry_snapshot["orb_low"],
                entry_snapshot["ema9"], entry_snapshot["ema21"], entry_snapshot["vwap"],
                entry_snapshot["adx"], entry_snapshot["atr"],
                entry_snapshot["max_pain_strike"], entry_snapshot["max_pain_bias"],
                entry_snapshot["oi_bias"],
                mfe_pts, mae_pts,
                be_done, be_trigger_time, be_trigger_pnl,
                trail_log, high_water_mark,
                ex_spot, ex_ema9, ex_ema21, ex_vwap, ex_adx,
                dynamic_sl, total_pnl,
            )

        while position_open:
            now_str = get_time_str()

            # Hard time exit
            if now_str >= EXIT_TIME:
                place_order(option_symbol, cfg["option_exchange"], "SELL", cfg["quantity"])
                curr       = get_quote(option_symbol, cfg["option_exchange"]) or curr
                pnl_pts    = curr - entry
                pnl_rs     = pnl_pts * cfg["quantity"]
                total_pnl += pnl_rs
                last_exit_time = get_ist_now()
                position_open  = False
                tradelog(name, f"HARD EXIT | Rs {pnl_rs:.0f} | Total: Rs {total_pnl:.0f}")
                finalize("Hard Exit", pnl_pts, pnl_rs, curr)
                break

            new_price = get_quote(option_symbol, cfg["option_exchange"])
            if new_price is None:
                # Quote feed is down — the SL is BLIND right now. Log it (so the
                # gap is visible in the activity log, never silent) and if it has
                # been dark too long, exit the position protectively rather than
                # ride an unmonitored move (trade #9 lost 2.4x its stop this way).
                stale = (get_ist_now() - last_quote_time).total_seconds()
                log(name, f"Quote unavailable — {stale:.0f}s since last good price (SL blind).")
                if stale >= STALE_QUOTE_EXIT_SECS:
                    tradelog(name, f"STALE QUOTE {stale:.0f}s — protective market exit.")
                    # verify_fill polls orderstatus (a different endpoint than the
                    # dead quotes feed), so we still capture the true exit price.
                    fill = execute_market_order(
                        option_symbol, cfg["option_exchange"], "SELL",
                        cfg["quantity"], fallback_price=curr
                    )
                    if not fill:
                        tradelog(name, f"WARNING: protective SELL for {option_symbol} "
                                       f"unconfirmed — POSITION MAY STILL BE OPEN. Check broker.")
                    exit_px    = fill or curr
                    pnl_pts    = exit_px - entry
                    pnl_rs     = pnl_pts * cfg["quantity"]
                    total_pnl += pnl_rs
                    last_exit_time = get_ist_now()
                    position_open  = False
                    tradelog(name, f"STALE QUOTE EXIT | Rs {pnl_rs:.0f} | Total: Rs {total_pnl:.0f}")
                    finalize("Stale Quote Exit", pnl_pts, pnl_rs, exit_px)
                    break
                time.sleep(3)
                continue
            curr = new_price
            last_quote_time = get_ist_now()

            pnl_pts   = curr - entry
            pnl_rs    = pnl_pts * cfg["quantity"]
            held_secs = (get_ist_now() - entry_time_dt).seconds

            if pnl_pts > mfe_pts:
                mfe_pts = pnl_pts
            if pnl_pts < mae_pts:
                mae_pts = pnl_pts
            if pnl_pts > high_water_mark:
                high_water_mark = pnl_pts

            # Re-evaluate the underlying trend on its own cadence
            if (get_ist_now() - last_mom_check).seconds >= MOM_CHECK_INTERVAL:
                snap = spot_trend(cfg)
                last_mom_check = get_ist_now()
                if snap:
                    ex_ema9, ex_ema21, ex_vwap, ex_adx, ex_spot = snap
                    mom_dead = not momentum_alive(option_type, ex_ema9, ex_ema21)

            log(name, (
                f"Premium:{curr} | P&L:{pnl_pts:.1f}pts = Rs {pnl_rs:.0f} | "
                f"SL:{dynamic_sl:.1f} | Peak:{high_water_mark:.1f} | "
                f"Trend:{'DEAD' if mom_dead else 'alive'} | Held:{held_secs}s"
            ))

            # Breakeven
            if pnl_pts >= cfg["be_trigger_pts"] and not be_done:
                dynamic_sl      = 0.0
                be_done         = True
                be_trigger_time = get_ist_now().strftime("%H:%M:%S")
                be_trigger_pnl  = pnl_pts
                tradelog(name, f"BREAK-EVEN at +{pnl_pts:.1f}pts — risk-free ride.")

            # Trailing SL (ratchets up only, wide giveback)
            if high_water_mark >= cfg["trail_activate_pts"]:
                trail_active = True
                new_sl = high_water_mark - cfg["trail_giveback_pts"]
                if new_sl > dynamic_sl:
                    trail_log.append({
                        "pnl": pnl_pts, "old_sl": dynamic_sl,
                        "new_sl": new_sl, "time": get_ist_now().strftime("%H:%M:%S"),
                    })
                    dynamic_sl = new_sl
                    tradelog(name, f"TRAIL #{len(trail_log)}: peak {high_water_mark:.1f} | SL → {dynamic_sl:.1f}pts")

            # ── EXIT PRIORITY ────────────────────────────────
            # 1. Stop loss (initial / breakeven / trailing)
            if pnl_pts <= dynamic_sl:
                place_order(option_symbol, cfg["option_exchange"], "SELL", cfg["quantity"])
                total_pnl += pnl_rs
                position_open = False
                if trail_active or be_done:
                    last_exit_time = get_ist_now()
                    reason = "Trailing SL"
                else:
                    last_sl_time = get_ist_now()
                    reason = "SL Hit"
                tradelog(name, f"{reason} | Rs {pnl_rs:.0f} | Total: Rs {total_pnl:.0f}")
                finalize(reason, pnl_pts, pnl_rs, curr)
                break

            # 2. Momentum death — the trend that we were riding has flipped
            if mom_dead and held_secs >= MIN_HOLD_SECONDS:
                place_order(option_symbol, cfg["option_exchange"], "SELL", cfg["quantity"])
                total_pnl += pnl_rs
                last_exit_time = get_ist_now()
                position_open  = False
                tradelog(name, f"NO MOMENTUM EXIT (trend flipped) | Rs {pnl_rs:.0f} | Total: Rs {total_pnl:.0f}")
                finalize("No Momentum Exit", pnl_pts, pnl_rs, curr)
                break

            time.sleep(MONITOR_INTERVAL)

        time.sleep(SIGNAL_INTERVAL)

    tradelog(name, f"=== Day done | Trades:{trade_count} | Final P&L: Rs {total_pnl:.0f} ===")


# ============================================================
# MAIN
# ============================================================

def main():
    tlog("=" * 60)
    tlog("Scalper V5 — RIDER Edition — NIFTY + SENSEX")
    tlog("Breakout entry + ride-the-trend exit (no fixed target, no ADX ceiling)")
    tlog("=" * 60)
    tlog(f"Log folder : {_TL._date_folder()}")
    tlog(f"Master CSV : {_TL._master_csv_path()}")
    tlog("")
    alog("=" * 60)
    alog("V5 Activity Log Started")
    alog("=" * 60)
    alog("")

    threads = []
    for name, cfg in INSTRUMENTS.items():
        t = threading.Thread(target=run_instrument, args=(name, cfg), daemon=True)
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

    tlog("=" * 60)
    tlog("V5 Rider complete for the day.")


if __name__ == "__main__":
    main()
