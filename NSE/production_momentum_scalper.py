"""
Production Momentum Scalper V3
================================
NIFTY + SENSEX with real indicators:
- EMA 9/21 trend filter
- VWAP filter
- ADX chop filter
- ATR volatility check
- ORB (Opening Range Breakout) signal
- Break-even SL
- Step trailing SL
- Cooldown after SL
- Lunch hour skip
- Daily loss lock
- Max trades limit
- Hard exit 3:30 PM

NIFTY : 65 qty | Tuesday expiry  | NFO
SENSEX: 20 qty | Wednesday expiry| BFO
"""

import time
import threading
from datetime import datetime, timedelta

import pandas as pd
import pytz
import requests

# ============================================================
# CONFIG — update API key and host if needed
# ============================================================

API_KEY  = "b2b89302e4f1ec3f478feb3d820fbd73106a2cca7991ee489e33966b86ae6cc5"
BASE_URL = "http://127.0.0.1:5000/api/v1"

ENTRY_TIME   = "09:20"
EXIT_TIME    = "15:30"

MAX_DAILY_LOSS    = -3000   # stop trading if loss hits Rs 3000
MAX_TRADES        = 4       # max trades per instrument per day
SIGNAL_INTERVAL   = 10      # seconds between signal checks
COOLDOWN_AFTER_SL = 300     # 5 min cooldown after each SL hit

IST     = pytz.timezone("Asia/Kolkata")
SESSION = requests.Session()

# ============================================================
# INSTRUMENT CONFIG — verified and corrected
# ============================================================

INSTRUMENTS = {
    "NIFTY": {
        "index_symbol":    "NIFTY",
        "index_exchange":  "NSE_INDEX",
        "option_exchange": "NFO",
        "quantity":        195,          # 1 lot NIFTY
        "target_pts":      8,
        "sl_pts":          5,
        "strike_interval": 50,
        "expiry_day":      1,           # Tuesday
        "be_trigger":      5,           # move SL to cost after 5pts gain
        "trail_trigger":   8,           # start trailing after 8pts gain
        "trail_step":      3,           # trail in 3pt steps
        "adx_threshold":   18,          # min ADX to enter
        "orb_candles":     3,           # first 3 candles = opening range (15 mins)
    },
    "SENSEX": {
        "index_symbol":    "SENSEX",
        "index_exchange":  "BSE_INDEX",
        "option_exchange": "BFO",
        "quantity":        60,          # 1 lot SENSEX
        "target_pts":      25,
        "sl_pts":          15,
        "strike_interval": 100,
        "expiry_day":      3,           # Thursday
        "be_trigger":      10,
        "trail_trigger":   15,
        "trail_step":      5,
        "adx_threshold":   18,
        "orb_candles":     3,
    }
}

# ============================================================
# HELPERS
# ============================================================

def get_ist_now():
    return datetime.now(IST)

def get_time_str():
    return get_ist_now().strftime("%H:%M")

def log(name, msg):
    print(f"[{get_ist_now().strftime('%H:%M:%S')}] [{name}] {msg}")

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

# ============================================================
# MARKET DATA
# ============================================================

def get_quote(symbol, exchange):
    data = api_post("quotes", {"symbol": symbol, "exchange": exchange}, timeout=5)
    if data and data.get("status") == "success":
        return float(data["data"]["ltp"])
    return None

def get_index_price(cfg):
    return get_quote(cfg["index_symbol"], cfg["index_exchange"])

def fetch_candles(cfg, interval="1m"):
    """Fetch today's candles from OpenAlgo history API."""
    today = get_ist_now().strftime("%Y-%m-%d")
    data = api_post("history", {
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

# ============================================================
# INDICATORS
# ============================================================

def calc_ema(df, period):
    """Exponential Moving Average on close prices."""
    return df["close"].ewm(span=period, adjust=False).mean().iloc[-1]

def calc_vwap(df):
    """
    VWAP = cumulative(typical_price) / candle count
    Volume is 0 from OpenAlgo so we use equal-weight typical price mean.
    """
    tp = (df["high"] + df["low"] + df["close"]) / 3
    return tp.expanding().mean().iloc[-1]

def calc_atr(df, period=14):
    """Average True Range."""
    hl  = df["high"] - df["low"]
    hc  = (df["high"] - df["close"].shift()).abs()
    lc  = (df["low"]  - df["close"].shift()).abs()
    tr  = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.rolling(period).mean().iloc[-1]

def calc_adx(df, period=14):
    """Average Directional Index — measures trend strength."""
    plus_dm  = df["high"].diff().clip(lower=0)
    minus_dm = (-df["low"].diff()).clip(lower=0)

    # zero out when the other direction is stronger
    mask = plus_dm < minus_dm
    plus_dm[mask] = 0
    mask2 = minus_dm <= plus_dm
    minus_dm[mask2] = 0

    hl  = df["high"] - df["low"]
    hc  = (df["high"] - df["close"].shift()).abs()
    lc  = (df["low"]  - df["close"].shift()).abs()
    tr  = pd.concat([hl, hc, lc], axis=1).max(axis=1)

    atr      = tr.rolling(period).mean()
    plus_di  = 100 * plus_dm.rolling(period).mean()  / atr
    minus_di = 100 * minus_dm.rolling(period).mean() / atr

    dx  = ((plus_di - minus_di).abs() / (plus_di + minus_di)) * 100
    adx = dx.rolling(period).mean()
    return adx.iloc[-1]

def get_orb(df, orb_candles=3):
    """
    Opening Range Breakout — high and low of first N candles.
    First 3 x 1min candles = 9:15, 9:16, 9:17 range.
    """
    orb_df   = df.head(orb_candles)
    orb_high = orb_df["high"].max()
    orb_low  = orb_df["low"].min()
    return orb_high, orb_low

# ============================================================
# OPTION HELPERS
# ============================================================

def get_atm_strike(price, interval):
    return round(price / interval) * interval

def get_expiry(cfg):
    today      = get_ist_now().date()
    days_ahead = (cfg["expiry_day"] - today.weekday()) % 7
    expiry     = today if days_ahead == 0 else today + timedelta(days=days_ahead)
    months     = ["JAN","FEB","MAR","APR","MAY","JUN",
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
        "strategy":  "Momentum Scalper V3",
    })
    if data and data.get("status") == "success":
        return data.get("orderid")
    return None

# ============================================================
# STRATEGY
# ============================================================

def run_instrument(name, cfg):
    log(name, f"Started | Qty:{cfg['quantity']} Target:{cfg['target_pts']}pts SL:{cfg['sl_pts']}pts")

    total_pnl    = 0
    trade_count  = 0
    last_sl_time = None

    while True:
        now_str = get_time_str()

        # Hard exit
        if now_str >= EXIT_TIME:
            log(name, f"Session ended | Trades:{trade_count} | P&L: Rs {total_pnl:.0f}")
            break

        # Wait for entry time
        if now_str < ENTRY_TIME:
            time.sleep(1)
            continue

        # Daily loss lock
        if total_pnl <= MAX_DAILY_LOSS:
            log(name, f"Daily loss limit hit (Rs {total_pnl:.0f}). Stopping.")
            break

        # Max trades
        if trade_count >= MAX_TRADES:
            log(name, f"Max {MAX_TRADES} trades reached. Stopping.")
            break

        # Cooldown after SL
        if last_sl_time:
            elapsed = (datetime.now() - last_sl_time).seconds
            if elapsed < COOLDOWN_AFTER_SL:
                remaining = COOLDOWN_AFTER_SL - elapsed
                log(name, f"Cooldown: {remaining}s remaining.")
                time.sleep(20)
                continue

        # Lunch hour skip
        if "11:45" <= now_str <= "13:15":
            log(name, "Lunch zone — skipping.")
            time.sleep(60)
            continue

        # ── Fetch candles ─────────────────────────────────
        df = fetch_candles(cfg, interval="1m")
        if df is None or len(df) < 20:
            log(name, f"Not enough candles ({0 if df is None else len(df)}). Waiting...")
            time.sleep(15)
            continue

        # ── Calculate indicators ──────────────────────────
        try:
            ema9      = calc_ema(df, 9)
            ema21     = calc_ema(df, 21)
            vwap      = calc_vwap(df)
            atr       = calc_atr(df)
            adx       = calc_adx(df)
            orb_high, orb_low = get_orb(df, cfg["orb_candles"])
            latest    = df.iloc[-1]["close"]
        except Exception as e:
            log(name, f"Indicator error: {e}")
            time.sleep(10)
            continue

        log(name, f"Price:{latest:.1f} EMA9:{ema9:.1f} EMA21:{ema21:.1f} VWAP:{vwap:.1f} ADX:{adx:.1f} ATR:{atr:.1f}")

        # ── ADX chop filter ───────────────────────────────
        if adx < cfg["adx_threshold"]:
            log(name, f"Low ADX ({adx:.1f} < {cfg['adx_threshold']}) — sideways market, skipping.")
            time.sleep(20)
            continue

        # ── Signal logic ──────────────────────────────────
        option_type = None

        # Bullish: price broke ORB high + EMA9 > EMA21 + price above VWAP
        if latest > orb_high and ema9 > ema21 and latest > vwap:
            option_type = "CE"
            log(name, f"CE signal | ORB:{orb_high:.1f} EMA:{ema9:.1f}>{ema21:.1f} VWAP:{vwap:.1f}")

        # Bearish: price broke ORB low + EMA9 < EMA21 + price below VWAP
        elif latest < orb_low and ema9 < ema21 and latest < vwap:
            option_type = "PE"
            log(name, f"PE signal | ORB:{orb_low:.1f} EMA:{ema9:.1f}<{ema21:.1f} VWAP:{vwap:.1f}")

        if not option_type:
            log(name, f"No setup | ORB H:{orb_high:.1f} L:{orb_low:.1f}")
            time.sleep(SIGNAL_INTERVAL)
            continue

        # ── Build option symbol and get premium ───────────
        atm           = get_atm_strike(latest, cfg["strike_interval"])
        option_symbol = build_option_symbol(name, cfg, atm, option_type)
        log(name, f"Symbol: {option_symbol}")

        entry = get_quote(option_symbol, cfg["option_exchange"])
        if entry is None:
            log(name, "Could not fetch option premium. Retrying.")
            time.sleep(10)
            continue

        # ── Place entry order ─────────────────────────────
        order = place_order(option_symbol, cfg["option_exchange"], "BUY", cfg["quantity"])
        if not order:
            log(name, "Entry order failed.")
            time.sleep(10)
            continue

        trade_count += 1
        log(name, f"Trade #{trade_count} entered @ Rs {entry} | {option_symbol}")

        # ── Position monitoring ───────────────────────────
        position_open  = True
        dynamic_sl     = -cfg["sl_pts"]
        sl_to_cost     = False
        locked_profit  = 0

        while position_open:
            now_str = get_time_str()

            # Hard exit during monitoring
            if now_str >= EXIT_TIME:
                log(name, "3:30 PM hard exit.")
                place_order(option_symbol, cfg["option_exchange"], "SELL", cfg["quantity"])
                curr      = get_quote(option_symbol, cfg["option_exchange"]) or entry
                trade_pnl = (curr - entry) * cfg["quantity"]
                total_pnl += trade_pnl
                log(name, f"Hard exit P&L: Rs {trade_pnl:.0f}")
                position_open = False
                break

            curr = get_quote(option_symbol, cfg["option_exchange"])
            if curr is None:
                time.sleep(3)
                continue

            pnl_pts = curr - entry
            pnl_rs  = pnl_pts * cfg["quantity"]
            log(name, f"Premium:{curr} | P&L:{pnl_pts:.1f}pts = Rs {pnl_rs:.0f} | SL at {dynamic_sl:.1f}pts")

            # Break-even
            if pnl_pts >= cfg["be_trigger"] and not sl_to_cost:
                dynamic_sl = 0
                sl_to_cost = True
                log(name, "SL moved to break-even.")

            # Step trailing
            if pnl_pts >= cfg["trail_trigger"]:
                new_locked = ((pnl_pts - cfg["trail_trigger"]) // cfg["trail_step"]) * cfg["trail_step"]
                if new_locked > locked_profit:
                    locked_profit = new_locked
                    dynamic_sl    = locked_profit
                    log(name, f"Trailing SL updated: lock {dynamic_sl:.1f}pts")

            # SL exit
            if pnl_pts <= dynamic_sl:
                place_order(option_symbol, cfg["option_exchange"], "SELL", cfg["quantity"])
                total_pnl    += pnl_rs
                last_sl_time  = datetime.now()
                position_open = False
                log(name, f"SL exit | Rs {pnl_rs:.0f} | Total: Rs {total_pnl:.0f}")

            # Target exit
            elif pnl_pts >= cfg["target_pts"]:
                place_order(option_symbol, cfg["option_exchange"], "SELL", cfg["quantity"])
                total_pnl    += pnl_rs
                position_open = False
                log(name, f"Target hit! Rs {pnl_rs:.0f} | Total: Rs {total_pnl:.0f}")

            if position_open:
                time.sleep(1)

        time.sleep(SIGNAL_INTERVAL)

    log(name, f"=== Day done | Trades:{trade_count} | Final P&L: Rs {total_pnl:.0f} ===")

# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 60)
    print("Production Momentum Scalper V3 — NIFTY + SENSEX")
    print("=" * 60)

    threads = []
    for name, cfg in INSTRUMENTS.items():
        t = threading.Thread(target=run_instrument, args=(name, cfg), daemon=True)
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

    print("=" * 60)
    print("All strategies complete for the day.")

if __name__ == "__main__":
    main()
