"""
Production Momentum Scalper V4.2
==================================
All V4.1 features plus:

V4.2 FIXES:
- API key loaded from OPENALGO_API_KEY env var (never hardcoded)
- calc_adx: removed in-place Series mutation → uses .where() (no SettingWithCopyWarning)
- calc_vwap: proper volume-weighted VWAP (was simple cumulative avg of typical price)
- analyze_option_chain: correct max pain calculation (minimise total ITM value across
  all strikes); OI bias direction corrected (high PE OI = support = CE bias)
- get_expiry: advances to next week if today is expiry day but market has closed
- All datetime.now() calls replaced with get_ist_now() for consistent IST-aware datetimes
- Hard exit: pnl_pts computed before total_pnl update so MFE/MAE are accurate

V4.1 TRADE ANALYTICS:
- MFE (Maximum Favorable Excursion) — highest profit reached
- MAE (Maximum Adverse Excursion) — deepest drawdown reached
- Profit capture rate (exit profit / MFE)
- Break-even audit (when triggered, at what P&L, what time)
- Trailing SL audit (each trail update logged)
- High water mark audit (peak, giveback, exit)
- Exit context (spot, EMA9, EMA21, VWAP, ADX, momentum state)

NIFTY : 65 qty | Tuesday expiry  | NFO
SENSEX: 20 qty | Thursday expiry | BFO
"""

import os
import time
import threading
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytz
import requests

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

ENTRY_TIME            = "09:20"
EXIT_TIME             = "15:30"

MAX_DAILY_LOSS        = -3000
MAX_TRADES            = 4
SIGNAL_INTERVAL       = 10
COOLDOWN_AFTER_SL     = 300
COOLDOWN_AFTER_TARGET = 180
MIN_HOLD_SECONDS      = 60
CANDLE_CONFIRM_COUNT  = 2
HIGH_WATER_DROP       = 5

IST      = pytz.timezone("Asia/Kolkata")
SESSION  = requests.Session()
LOG_LOCK = threading.Lock()

# ============================================================
# INSTRUMENT CONFIG
# ============================================================

INSTRUMENTS = {
    "NIFTY": {
        "index_symbol":    "NIFTY",
        "index_exchange":  "NSE_INDEX",
        "option_exchange": "NFO",
        "quantity":        65,
        "target_pts":      8,
        "sl_pts":          5,
        "strike_interval": 50,
        "expiry_day":      1,
        "be_trigger":      5,
        "trail_trigger":   5,
        "trail_step":      2,
        "adx_threshold":   20,
        "orb_candles":     3,
        "orb_buffer":      20,
    },
    "SENSEX": {
        "index_symbol":    "SENSEX",
        "index_exchange":  "BSE_INDEX",
        "option_exchange": "BFO",
        "quantity":        20,
        "target_pts":      25,
        "sl_pts":          15,
        "strike_interval": 100,
        "expiry_day":      3,
        "be_trigger":      10,
        "trail_trigger":   10,
        "trail_step":      3,
        "adx_threshold":   20,
        "orb_candles":     3,
        "orb_buffer":      60,
    }
}

# ============================================================
# LOGGING SETUP
# ============================================================

LOG_DIR = Path(os.path.expanduser("~/openalgo/trade_logs"))
LOG_DIR.mkdir(exist_ok=True)

def get_trade_log_file():
    """Trade log — entries, exits, summaries only."""
    today = datetime.now(IST).strftime("%Y-%m-%d")
    return LOG_DIR / f"{today}_trades.log"

def get_activity_log_file():
    """Activity log — everything for debugging."""
    today = datetime.now(IST).strftime("%Y-%m-%d")
    return LOG_DIR / f"{today}_activity.log"

def tlog(text):
    """Write to trade log file + print to terminal."""
    print(text)
    try:
        with LOG_LOCK:
            with open(get_trade_log_file(), "a", encoding="utf-8") as fh:
                fh.write(text + "\n")
    except Exception:
        pass

def alog(text):
    """Write to activity log file + print to terminal."""
    print(text)
    try:
        with LOG_LOCK:
            with open(get_activity_log_file(), "a", encoding="utf-8") as fh:
                fh.write(text + "\n")
    except Exception:
        pass

def get_ist_now():
    return datetime.now(IST)

def get_time_str():
    return get_ist_now().strftime("%H:%M")

def log(name, msg):
    """Activity log only — routine signal checks go here."""
    line = f"[{get_ist_now().strftime('%H:%M:%S')}] [{name}] {msg}"
    alog(line)

def tradelog(name, msg):
    """Trade log — important events only."""
    line = f"[{get_ist_now().strftime('%H:%M:%S')}] [{name}] {msg}"
    tlog(line)

def get_momentum_state(ema9, ema21, adx):
    if ema9 > ema21 and adx > 20:
        return "Strong Bullish"
    elif ema9 > ema21 and adx <= 20:
        return "Weakening Bullish"
    elif ema9 < ema21 and adx > 20:
        return "Strong Bearish"
    elif ema9 < ema21 and adx <= 20:
        return "Weakening Bearish"
    else:
        return "Neutral"

def log_day_start(name):
    sep = "#" * 60
    tlog("")
    tlog(sep)
    tlog(f"# {name} Strategy Started — {get_ist_now().strftime('%Y-%m-%d')}")
    tlog(f"# Entry: {ENTRY_TIME} | Exit: {EXIT_TIME} | Max Trades: {MAX_TRADES}")
    tlog(sep)
    tlog("")
    alog(f"# {name} Activity Log — {get_ist_now().strftime('%Y-%m-%d')}")
    alog("")

def log_trade(
    name, trade_num, option_type, option_symbol,
    entry_premium, exit_premium, exit_reason,
    entry_time, exit_time,
    spot, orb_high, orb_low,
    ema9, ema21, vwap, adx, atr,
    max_pain_strike, max_pain_bias, oi_bias,
    # V4.1 analytics
    mfe_pts, mae_pts,
    be_triggered, be_trigger_time, be_trigger_pnl,
    trail_log,
    high_water_mark,
    exit_spot, exit_ema9, exit_ema21, exit_vwap, exit_adx,
    dynamic_sl_at_exit, running_total,
    cfg_target=8, cfg_sl=5
):
    qty     = INSTRUMENTS[name]["quantity"]
    pnl_pts = exit_premium - entry_premium
    pnl_rs  = pnl_pts * qty
    sep     = "=" * 60

    # Capture rate
    capture_rate = (pnl_pts / mfe_pts * 100) if mfe_pts > 0 else 0

    # Momentum state at exit
    exit_momentum = get_momentum_state(exit_ema9, exit_ema21, exit_adx)

    lines = [
        "",
        sep,
        f"TRADE #{trade_num} | {name} | {entry_time.strftime('%Y-%m-%d')} | {entry_time.strftime('%H:%M:%S')}",
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
        "── ENTRY ────────────────────────────────────────────",
        f"  Signal         : {option_type}",
        f"  Symbol         : {option_symbol}",
        f"  Strike Type    : ITM (1 strike in the money)",
        f"  Entry Premium  : Rs {entry_premium:.2f}",
        f"  Target Premium : Rs {entry_premium + cfg_target:.2f} (+{cfg_target}pts = Rs {cfg_target * qty:.0f})",
        f"  SL Premium     : Rs {entry_premium - cfg_sl:.2f} (-{cfg_sl}pts = Rs {cfg_sl * qty:.0f})",
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
            f"  Break-even     : YES — Triggered",
            f"  Triggered At   : {be_trigger_pnl:.1f}pts",
            f"  Time           : {be_trigger_time}",
            f"  SL moved       : -{cfg_sl}pts → 0",
        ]
    else:
        lines += [
            f"  Break-even     : NO — Never reached",
            f"  Highest Profit : {mfe_pts:.1f}pts (needed {cfg_target}pts to trigger BE)",
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
        "── HIGH WATER AUDIT ─────────────────────────────────",
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
        sep,
        "",
    ]
    tlog("\n".join(lines))

def log_day_summary(name, trade_count, total_pnl, trades_detail):
    """trades_detail is list of dicts with pnl, exit_reason, mfe, mae"""
    if not trades_detail:
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
        f"DAY SUMMARY — {name} — {get_ist_now().strftime('%Y-%m-%d')}",
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
        "── MFE / MAE SUMMARY ────────────────────────────────",
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
# MARKET DATA
# ============================================================

def api_post(endpoint, payload, timeout=15):
    payload = {"apikey": API_KEY, **payload}
    try:
        r = SESSION.post(
            f"{BASE_URL}/{endpoint}",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=timeout
        )
        try:
            data = r.json()
        except ValueError:
            return None
        if r.status_code >= 400:
            log("API", f"{endpoint} error {r.status_code}: {data}")
            return None
        return data
    except Exception as e:
        log("API", f"{endpoint} failed: {e}")
        return None

def get_quote(symbol, exchange):
    data = api_post("quotes", {"symbol": symbol, "exchange": exchange}, timeout=5)
    if data and data.get("status") == "success":
        return float(data["data"]["ltp"])
    return None

def get_index_price(cfg):
    return get_quote(cfg["index_symbol"], cfg["index_exchange"])

def fetch_candles(cfg, interval="1m"):
    today = get_ist_now().strftime("%Y-%m-%d")
    data  = api_post("history", {
        "symbol":     cfg["index_symbol"],
        "exchange":   cfg["index_exchange"],
        "interval":   interval,
        "start_date": today,
        "end_date":   today,
    })
    if not data or "data" not in data or not data["data"]:
        return None
    df = pd.DataFrame(data["data"])
    df = df.sort_values("timestamp").reset_index(drop=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s")
    return df

def fetch_option_chain(cfg, expiry_date):
    data = api_post("optionchain", {
        "underlying":  cfg["index_symbol"],
        "expiry_date": expiry_date,
        "exchange":    cfg["option_exchange"],
    })
    if data and data.get("status") == "success":
        return data
    return None

# ============================================================
# INDICATORS
# ============================================================

def calc_ema(df, period):
    return df["close"].ewm(span=period, adjust=False).mean().iloc[-1]

def calc_vwap(df):
    tp = (df["high"] + df["low"] + df["close"]) / 3
    if "volume" in df.columns and df["volume"].sum() > 0:
        return (tp * df["volume"]).cumsum().iloc[-1] / df["volume"].cumsum().iloc[-1]
    # Fallback to equal-weight average if volume data is absent
    return tp.expanding().mean().iloc[-1]

def calc_atr(df, period=14):
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"]  - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.rolling(period).mean().iloc[-1]

def calc_adx(df, period=14):
    plus_dm_raw  = df["high"].diff().clip(lower=0)
    minus_dm_raw = (-df["low"].diff()).clip(lower=0)
    # Zero out whichever directional move is smaller — no in-place mutation
    plus_dm  = plus_dm_raw.where(plus_dm_raw >= minus_dm_raw, 0.0)
    minus_dm = minus_dm_raw.where(minus_dm_raw > plus_dm_raw, 0.0)
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"]  - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    atr      = tr.rolling(period).mean()
    plus_di  = 100 * plus_dm.rolling(period).mean() / atr
    minus_di = 100 * minus_dm.rolling(period).mean() / atr
    dx  = ((plus_di - minus_di).abs() / (plus_di + minus_di)) * 100
    return dx.rolling(period).mean().iloc[-1]

def get_orb(df, orb_candles=3):
    orb_df = df.head(orb_candles)
    return orb_df["high"].max(), orb_df["low"].min()

# ============================================================
# OI + MAX PAIN
# ============================================================

def analyze_option_chain(chain_data, atm_strike, strike_interval, num_strikes=10):
    if not chain_data or "chain" not in chain_data:
        return None, None, None

    chain = chain_data["chain"]
    atm   = chain_data.get("atm_strike", atm_strike)
    spot  = chain_data.get("underlying_ltp", 0)

    # ── True Max Pain ────────────────────────────────────────
    # For each candidate expiry strike S, compute the total dollar
    # loss writers suffer: sum over all CE strikes < S of (S - strike)*CE_OI
    # plus sum over all PE strikes > S of (strike - S)*PE_OI.
    # The strike that minimises this total is max pain.
    strikes = sorted({row["strike"] for row in chain})
    ce_oi   = {row["strike"]: (row.get("ce") or {}).get("oi", 0) or 0 for row in chain}
    pe_oi   = {row["strike"]: (row.get("pe") or {}).get("oi", 0) or 0 for row in chain}

    min_pain  = float("inf")
    max_pain_strike = None
    for s in strikes:
        pain = (
            sum((s - k) * ce_oi[k] for k in strikes if k < s) +
            sum((k - s) * pe_oi[k] for k in strikes if k > s)
        )
        if pain < min_pain:
            min_pain        = pain
            max_pain_strike = s

    max_pain_bias = None
    if max_pain_strike and spot:
        # Price is expected to gravitate toward max pain at expiry.
        # If max pain is above spot, bulls benefit → CE bias for the trade.
        max_pain_bias = "CE" if max_pain_strike > spot else "PE"

    # ── OI Bias (near-ATM strikes only) ─────────────────────
    # High PE OI near ATM = strong support / put writers defended → bullish → CE bias
    # High CE OI near ATM = strong resistance / call writers defended → bearish → PE bias
    ce_oi_total = 0
    pe_oi_total = 0
    for row in chain:
        if abs(row["strike"] - atm) <= num_strikes * strike_interval:
            ce_oi_total += (row.get("ce") or {}).get("oi", 0) or 0
            pe_oi_total += (row.get("pe") or {}).get("oi", 0) or 0

    # More PE OI → put writing activity → market expects support → CE bias
    # More CE OI → call writing activity → market expects resistance → PE bias
    oi_bias = "CE" if pe_oi_total > ce_oi_total else "PE"
    return max_pain_strike, max_pain_bias, oi_bias

def get_itm_strike(option_type, atm_strike, strike_interval):
    return atm_strike - strike_interval if option_type == "CE" else atm_strike + strike_interval

# ============================================================
# OPTION HELPERS
# ============================================================

def get_atm_strike(price, interval):
    return round(price / interval) * interval

def get_expiry(cfg):
    today      = get_ist_now()
    today_date = today.date()
    days_ahead = (cfg["expiry_day"] - today_date.weekday()) % 7
    if days_ahead == 0:
        # Today is expiry day — if past exit time, use next week's expiry
        if today.strftime("%H:%M") >= EXIT_TIME:
            days_ahead = 7
    expiry = today_date if days_ahead == 0 else today_date + timedelta(days=days_ahead)
    months = ["JAN","FEB","MAR","APR","MAY","JUN",
              "JUL","AUG","SEP","OCT","NOV","DEC"]
    return f"{expiry.day:02d}{months[expiry.month-1]}{expiry.year % 100:02d}"

def build_option_symbol(name, cfg, strike, option_type):
    return f"{name}{get_expiry(cfg)}{strike}{option_type}"

def place_order(symbol, exchange, action, quantity):
    data = api_post("placeorder", {
        "symbol":    symbol,
        "exchange":  exchange,
        "action":    action,
        "quantity":  quantity,
        "price":     0,
        "pricetype": "MARKET",
        "product":   "MIS",
        "strategy":  "Momentum Scalper V4.2",
    })
    if data and data.get("status") == "success":
        return data.get("orderid")
    return None

# ============================================================
# STRATEGY
# ============================================================

def run_instrument(name, cfg):
    log(name, f"V4.2 Started | Qty:{cfg['quantity']} Target:{cfg['target_pts']}pts SL:{cfg['sl_pts']}pts")

    total_pnl        = 0
    trade_count      = 0
    last_sl_time     = None
    last_target_time = None
    last_direction   = None
    signal_history   = deque(maxlen=CANDLE_CONFIRM_COUNT)
    expiry_date      = get_expiry(cfg)
    chain_data       = None
    last_chain_fetch = None
    trades_detail    = []
    entry_snapshot   = {}

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

        if last_sl_time:
            elapsed = (get_ist_now() - last_sl_time).total_seconds()
            if elapsed < COOLDOWN_AFTER_SL:
                log(name, f"SL cooldown: {COOLDOWN_AFTER_SL - elapsed}s remaining.")
                time.sleep(20)
                continue

        if last_target_time:
            elapsed = (get_ist_now() - last_target_time).total_seconds()
            if elapsed < COOLDOWN_AFTER_TARGET:
                log(name, f"Target cooldown: {COOLDOWN_AFTER_TARGET - elapsed}s remaining.")
                time.sleep(20)
                continue

        if "11:45" <= now_str <= "13:00":
            log(name, "Lunch zone — skipping.")
            time.sleep(60)
            continue

        # Fetch option chain every 15 minutes
        if (last_chain_fetch is None or
                (now - last_chain_fetch).total_seconds() >= 900):
            log(name, "Fetching option chain...")
            chain_data       = fetch_option_chain(cfg, expiry_date)
            last_chain_fetch = now
            if chain_data:
                log(name, f"Option chain fetched. ATM: {chain_data.get('atm_strike')}")

        # Fetch candles
        df = fetch_candles(cfg, interval="1m")
        if df is None or len(df) < 20:
            log(name, "Not enough candles. Waiting...")
            time.sleep(15)
            continue

        # Indicators
        try:
            ema9     = calc_ema(df, 9)
            ema21    = calc_ema(df, 21)
            vwap     = calc_vwap(df)
            atr      = calc_atr(df)
            adx      = calc_adx(df)
            orb_high, orb_low = get_orb(df, cfg["orb_candles"])
            latest   = df.iloc[-1]["close"]
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

        if adx < cfg["adx_threshold"]:
            log(name, f"Low ADX ({adx:.1f}) — sideways. Skipping.")
            signal_history.clear()
            time.sleep(20)
            continue

        raw_signal = None
        orb_buffer = cfg["orb_buffer"]

        if (latest > orb_high + orb_buffer and
                ema9 > ema21 and latest > vwap):
            raw_signal = "CE"
        elif (latest < orb_low - orb_buffer and
                ema9 < ema21 and latest < vwap):
            raw_signal = "PE"

        if not raw_signal:
            log(name, f"No setup | ORB+buf H:{orb_high+orb_buffer:.0f} L:{orb_low-orb_buffer:.0f}")
            signal_history.clear()
            time.sleep(SIGNAL_INTERVAL)
            continue

        signal_history.append(raw_signal)
        if len(signal_history) < CANDLE_CONFIRM_COUNT:
            log(name, f"Candle confirm: {len(signal_history)}/{CANDLE_CONFIRM_COUNT}")
            time.sleep(SIGNAL_INTERVAL)
            continue

        if not all(s == raw_signal for s in signal_history):
            log(name, "Signal not consistent. Waiting...")
            time.sleep(SIGNAL_INTERVAL)
            continue

        option_type = raw_signal
        log(name, f"Signal confirmed: {option_type} ({CANDLE_CONFIRM_COUNT} candles)")

        if max_pain_bias and max_pain_bias != option_type:
            log(name, f"Max pain ({max_pain_bias}) conflicts signal ({option_type}). Note only — proceeding.")
            # Soft filter — log warning but don't block trade

        if oi_bias and oi_bias != option_type:
            log(name, f"OI bias ({oi_bias}) conflicts signal ({option_type}). Skipping.")
            signal_history.clear()
            time.sleep(SIGNAL_INTERVAL)
            continue

        if (last_direction == option_type and last_sl_time and
                (get_ist_now() - last_sl_time).total_seconds() < 600):
            log(name, f"Same direction ({option_type}) after recent SL. Skipping.")
            signal_history.clear()
            time.sleep(30)
            continue

        entry_snapshot = {
            "spot": latest, "orb_high": orb_high, "orb_low": orb_low,
            "ema9": ema9, "ema21": ema21, "vwap": vwap,
            "adx": adx, "atr": atr,
            "max_pain_strike": max_pain_strike,
            "max_pain_bias": max_pain_bias,
            "oi_bias": oi_bias,
        }

        itm_strike    = get_itm_strike(option_type, atm, cfg["strike_interval"])
        option_symbol = build_option_symbol(name, cfg, itm_strike, option_type)
        log(name, f"ITM Symbol: {option_symbol} (ATM:{atm} ITM:{itm_strike})")

        entry = get_quote(option_symbol, cfg["option_exchange"])
        if entry is None:
            log(name, "Could not fetch premium. Retrying.")
            time.sleep(10)
            continue

        order = place_order(option_symbol, cfg["option_exchange"], "BUY", cfg["quantity"])
        if not order:
            log(name, "Entry order failed.")
            time.sleep(10)
            continue

        trade_count    += 1
        last_direction  = option_type
        entry_time_dt   = get_ist_now()
        signal_history.clear()
        tradelog(name, f"ENTRY Trade #{trade_count} | {option_type} ITM | Rs {entry} | {option_symbol}")

        # ── V4.1 tracking variables ───────────────────────
        position_open   = True
        dynamic_sl      = -cfg["sl_pts"]
        sl_to_cost      = False
        locked_profit   = 0
        high_water_mark = 0
        mfe_pts         = 0        # max favorable excursion
        mae_pts         = 0        # max adverse excursion (most negative)
        be_triggered    = False
        be_trigger_time = None
        be_trigger_pnl  = 0
        trail_log       = []       # list of trail updates
        exit_reason_str = "Unknown"
        curr            = entry
        # Exit context indicators
        exit_ema9  = ema9
        exit_ema21 = ema21
        exit_vwap  = vwap
        exit_adx   = adx
        exit_spot  = latest

        while position_open:
            now_str = get_time_str()

            # Hard exit
            if now_str >= EXIT_TIME:
                log(name, "3:30 PM hard exit.")
                place_order(option_symbol, cfg["option_exchange"], "SELL", cfg["quantity"])
                curr            = get_quote(option_symbol, cfg["option_exchange"]) or curr
                pnl_pts         = curr - entry
                pnl_rs          = pnl_pts * cfg["quantity"]
                total_pnl      += pnl_rs
                exit_reason_str = "Hard Exit"
                position_open   = False

                # Capture exit context
                df_exit = fetch_candles(cfg, interval="1m")
                if df_exit is not None and len(df_exit) >= 5:
                    try:
                        exit_ema9  = calc_ema(df_exit, 9)
                        exit_ema21 = calc_ema(df_exit, 21)
                        exit_vwap  = calc_vwap(df_exit)
                        exit_adx   = calc_adx(df_exit)
                        exit_spot  = df_exit.iloc[-1]["close"]
                    except Exception as e:
                        log(name, f"Exit indicator error (Hard Exit): {e}")

                capture = (pnl_pts / mfe_pts * 100) if mfe_pts > 0 else 0
                trades_detail.append({
                    "pnl": pnl_rs, "exit_reason": exit_reason_str,
                    "mfe": mfe_pts, "mae": mae_pts, "capture": capture
                })
                log_trade(
                    name, trade_count, option_type, option_symbol,
                    entry, curr, exit_reason_str,
                    entry_time_dt, get_ist_now(),
                    entry_snapshot.get("spot", 0), entry_snapshot.get("orb_high", 0),
                    entry_snapshot.get("orb_low", 0), entry_snapshot.get("ema9", 0),
                    entry_snapshot.get("ema21", 0), entry_snapshot.get("vwap", 0),
                    entry_snapshot.get("adx", 0), entry_snapshot.get("atr", 0),
                    entry_snapshot.get("max_pain_strike", 0),
                    entry_snapshot.get("max_pain_bias", "N/A"),
                    entry_snapshot.get("oi_bias", "N/A"),
                    mfe_pts, mae_pts,
                    be_triggered, be_trigger_time, be_trigger_pnl,
                    trail_log, high_water_mark,
                    exit_spot, exit_ema9, exit_ema21, exit_vwap, exit_adx,
                    dynamic_sl, total_pnl,
                    cfg["target_pts"], cfg["sl_pts"]
                )
                break

            new_price = get_quote(option_symbol, cfg["option_exchange"])
            if new_price is None:
                time.sleep(3)
                continue
            curr = new_price

            pnl_pts   = curr - entry
            pnl_rs    = pnl_pts * cfg["quantity"]
            held_secs = int((get_ist_now() - entry_time_dt).total_seconds())

            # Track MFE and MAE
            if pnl_pts > mfe_pts:
                mfe_pts = pnl_pts
            if pnl_pts < mae_pts:
                mae_pts = pnl_pts

            log(name, (
                f"Premium:{curr} | P&L:{pnl_pts:.1f}pts = Rs {pnl_rs:.0f} | "
                f"SL:{dynamic_sl:.1f} | MFE:{mfe_pts:.1f} MAE:{mae_pts:.1f} | Held:{held_secs}s"
            ))

            if held_secs < MIN_HOLD_SECONDS and pnl_pts > -cfg["sl_pts"]:
                time.sleep(5)
                continue

            # High water mark
            if pnl_pts > high_water_mark:
                high_water_mark = pnl_pts

            # High water exit
            if (high_water_mark >= cfg["be_trigger"] and
                    pnl_pts <= high_water_mark - HIGH_WATER_DROP):
                place_order(option_symbol, cfg["option_exchange"], "SELL", cfg["quantity"])
                total_pnl        += pnl_rs
                last_target_time  = get_ist_now()
                exit_reason_str   = "High Water Exit"
                position_open     = False

                df_exit = fetch_candles(cfg, interval="1m")
                if df_exit is not None and len(df_exit) >= 5:
                    try:
                        exit_ema9  = calc_ema(df_exit, 9)
                        exit_ema21 = calc_ema(df_exit, 21)
                        exit_vwap  = calc_vwap(df_exit)
                        exit_adx   = calc_adx(df_exit)
                        exit_spot  = df_exit.iloc[-1]["close"]
                    except Exception as e:
                        log(name, f"Exit indicator error (High Water Exit): {e}")

                capture = (pnl_pts / mfe_pts * 100) if mfe_pts > 0 else 0
                trades_detail.append({
                    "pnl": pnl_rs, "exit_reason": exit_reason_str,
                    "mfe": mfe_pts, "mae": mae_pts, "capture": capture
                })
                tradelog(name, f"HIGH WATER EXIT! Peak:{high_water_mark:.1f} Now:{pnl_pts:.1f} | Rs {pnl_rs:.0f} | Total: Rs {total_pnl:.0f}")
                log_trade(
                    name, trade_count, option_type, option_symbol,
                    entry, curr, exit_reason_str,
                    entry_time_dt, get_ist_now(),
                    entry_snapshot.get("spot", 0), entry_snapshot.get("orb_high", 0),
                    entry_snapshot.get("orb_low", 0), entry_snapshot.get("ema9", 0),
                    entry_snapshot.get("ema21", 0), entry_snapshot.get("vwap", 0),
                    entry_snapshot.get("adx", 0), entry_snapshot.get("atr", 0),
                    entry_snapshot.get("max_pain_strike", 0),
                    entry_snapshot.get("max_pain_bias", "N/A"),
                    entry_snapshot.get("oi_bias", "N/A"),
                    mfe_pts, mae_pts,
                    be_triggered, be_trigger_time, be_trigger_pnl,
                    trail_log, high_water_mark,
                    exit_spot, exit_ema9, exit_ema21, exit_vwap, exit_adx,
                    dynamic_sl, total_pnl,
                    cfg["target_pts"], cfg["sl_pts"]
                )
                continue

            # Break-even
            if pnl_pts >= cfg["be_trigger"] and not sl_to_cost:
                dynamic_sl      = 0
                sl_to_cost      = True
                be_triggered    = True
                be_trigger_time = get_ist_now().strftime("%H:%M:%S")
                be_trigger_pnl  = pnl_pts
                tradelog(name, f"BREAK-EVEN triggered at +{pnl_pts:.1f}pts.")

            # Step trailing
            if pnl_pts >= cfg["trail_trigger"]:
                new_locked = ((pnl_pts - cfg["trail_trigger"]) // cfg["trail_step"]) * cfg["trail_step"]
                if new_locked > locked_profit:
                    trail_log.append({
                        "pnl":    pnl_pts,
                        "old_sl": dynamic_sl,
                        "new_sl": new_locked,
                        "time":   get_ist_now().strftime("%H:%M:%S")
                    })
                    locked_profit = new_locked
                    dynamic_sl    = locked_profit
                    tradelog(name, f"TRAIL #{len(trail_log)}: P&L {pnl_pts:.1f}pts | SL {trail_log[-1]['old_sl']:.1f} → {dynamic_sl:.1f}pts")

            # SL exit
            if pnl_pts <= dynamic_sl:
                place_order(option_symbol, cfg["option_exchange"], "SELL", cfg["quantity"])
                total_pnl      += pnl_rs
                last_sl_time    = get_ist_now()
                exit_reason_str = "SL Hit"
                position_open   = False

                df_exit = fetch_candles(cfg, interval="1m")
                if df_exit is not None and len(df_exit) >= 5:
                    try:
                        exit_ema9  = calc_ema(df_exit, 9)
                        exit_ema21 = calc_ema(df_exit, 21)
                        exit_vwap  = calc_vwap(df_exit)
                        exit_adx   = calc_adx(df_exit)
                        exit_spot  = df_exit.iloc[-1]["close"]
                    except Exception as e:
                        log(name, f"Exit indicator error (SL Hit): {e}")

                capture = (pnl_pts / mfe_pts * 100) if mfe_pts > 0 else 0
                trades_detail.append({
                    "pnl": pnl_rs, "exit_reason": exit_reason_str,
                    "mfe": mfe_pts, "mae": mae_pts, "capture": capture
                })
                log(name, f"SL exit | Rs {pnl_rs:.0f} | Total: Rs {total_pnl:.0f}")
                log_trade(
                    name, trade_count, option_type, option_symbol,
                    entry, curr, exit_reason_str,
                    entry_time_dt, get_ist_now(),
                    entry_snapshot.get("spot", 0), entry_snapshot.get("orb_high", 0),
                    entry_snapshot.get("orb_low", 0), entry_snapshot.get("ema9", 0),
                    entry_snapshot.get("ema21", 0), entry_snapshot.get("vwap", 0),
                    entry_snapshot.get("adx", 0), entry_snapshot.get("atr", 0),
                    entry_snapshot.get("max_pain_strike", 0),
                    entry_snapshot.get("max_pain_bias", "N/A"),
                    entry_snapshot.get("oi_bias", "N/A"),
                    mfe_pts, mae_pts,
                    be_triggered, be_trigger_time, be_trigger_pnl,
                    trail_log, high_water_mark,
                    exit_spot, exit_ema9, exit_ema21, exit_vwap, exit_adx,
                    dynamic_sl, total_pnl,
                    cfg["target_pts"], cfg["sl_pts"]
                )

            # Target exit
            elif pnl_pts >= cfg["target_pts"]:
                place_order(option_symbol, cfg["option_exchange"], "SELL", cfg["quantity"])
                total_pnl        += pnl_rs
                last_target_time  = get_ist_now()
                exit_reason_str   = "Target Hit"
                position_open     = False

                df_exit = fetch_candles(cfg, interval="1m")
                if df_exit is not None and len(df_exit) >= 5:
                    try:
                        exit_ema9  = calc_ema(df_exit, 9)
                        exit_ema21 = calc_ema(df_exit, 21)
                        exit_vwap  = calc_vwap(df_exit)
                        exit_adx   = calc_adx(df_exit)
                        exit_spot  = df_exit.iloc[-1]["close"]
                    except Exception as e:
                        log(name, f"Exit indicator error (Target Hit): {e}")

                capture = (pnl_pts / mfe_pts * 100) if mfe_pts > 0 else 0
                trades_detail.append({
                    "pnl": pnl_rs, "exit_reason": exit_reason_str,
                    "mfe": mfe_pts, "mae": mae_pts, "capture": capture
                })
                tradelog(name, f"TARGET HIT! Rs {pnl_rs:.0f} | Total: Rs {total_pnl:.0f}")
                log_trade(
                    name, trade_count, option_type, option_symbol,
                    entry, curr, exit_reason_str,
                    entry_time_dt, get_ist_now(),
                    entry_snapshot.get("spot", 0), entry_snapshot.get("orb_high", 0),
                    entry_snapshot.get("orb_low", 0), entry_snapshot.get("ema9", 0),
                    entry_snapshot.get("ema21", 0), entry_snapshot.get("vwap", 0),
                    entry_snapshot.get("adx", 0), entry_snapshot.get("atr", 0),
                    entry_snapshot.get("max_pain_strike", 0),
                    entry_snapshot.get("max_pain_bias", "N/A"),
                    entry_snapshot.get("oi_bias", "N/A"),
                    mfe_pts, mae_pts,
                    be_triggered, be_trigger_time, be_trigger_pnl,
                    trail_log, high_water_mark,
                    exit_spot, exit_ema9, exit_ema21, exit_vwap, exit_adx,
                    dynamic_sl, total_pnl,
                    cfg["target_pts"], cfg["sl_pts"]
                )

            if position_open:
                time.sleep(5)

        time.sleep(SIGNAL_INTERVAL)

    tradelog(name, f"=== Day done | Trades:{trade_count} | Final P&L: Rs {total_pnl:.0f} ===")

# ============================================================
# MAIN
# ============================================================

def main():
    tlog("=" * 60)
    tlog("Momentum Scalper V4.2 — NIFTY + SENSEX")
    tlog("OI + Max Pain + ORB + EMA + VWAP + ADX + MFE/MAE + Logging")
    tlog("=" * 60)
    tlog(f"Trade log  : {get_trade_log_file()}")
    tlog(f"Activity log: {get_activity_log_file()}")
    tlog("")
    alog("=" * 60)
    alog("Activity Log Started")
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
    tlog("All strategies complete for the day.")

if __name__ == "__main__":
    main()
