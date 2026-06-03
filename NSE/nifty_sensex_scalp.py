"""
NIFTY + SENSEX Morning Scalp Strategy — Rolling Reference Version
==================================================================
- Runs both NIFTY and SENSEX simultaneously
- Reference price updates every 15 minutes (rolling)
- If market moves direction_pts from rolling reference → enter
- Re-enters after SL or Target hit
- Hard exit at 3:30 PM
- NIFTY: 65 qty, 40pt direction, 8pt target, 5pt SL (NFO, Tuesday expiry)
- SENSEX: 20 qty, 150pt direction, 25pt target, 15pt SL (BFO, Thursday expiry)
"""

import time
import threading
from datetime import datetime, timedelta

import pytz
import requests

# ============================================================
# CONFIG
# ============================================================
OPENALGO_API_KEY     = "b2b89302e4f1ec3f478feb3d820fbd73106a2cca7991ee489e33966b86ae6cc5"
OPENALGO_HOST        = "http://127.0.0.1:5000"

ENTRY_TIME           = "09:20"   # earliest entry
EXIT_TIME            = "15:30"   # hard exit
SIGNAL_INTERVAL      = 10        # seconds between signal checks
REFERENCE_INTERVAL   = 15 * 60  # update rolling reference every 15 minutes

IST     = pytz.timezone("Asia/Kolkata")
SESSION = requests.Session()

# ── Per-instrument config ─────────────────────────────────
INSTRUMENTS = {
    "NIFTY": {
        "index_symbol":    "NIFTY",
        "index_exchange":  "NSE_INDEX",
        "option_exchange": "NFO",
        "quantity":        65,
        "direction_pts":   40,
        "target_pts":      8,
        "sl_pts":          5,
        "strike_interval": 50,
        "expiry_day":      1,       # Tuesday
    },
    "SENSEX": {
        "index_symbol":    "SENSEX",
        "index_exchange":  "BSE_INDEX",
        "option_exchange": "BFO",
        "quantity":        20,
        "direction_pts":   150,
        "target_pts":      25,
        "sl_pts":          15,
        "strike_interval": 100,
        "expiry_day":      2,       # Wednesday
    }
}

# ============================================================
# HELPERS
# ============================================================

def get_ist_now():
    return datetime.now(IST)

def get_time_str():
    return get_ist_now().strftime("%H:%M")

def log(instrument, msg):
    print(f"[{get_ist_now().strftime('%H:%M:%S')}] [{instrument}] {msg}")

def api_post(endpoint, payload, timeout=10):
    payload = {"apikey": OPENALGO_API_KEY, **payload}
    url = f"{OPENALGO_HOST}/api/v1/{endpoint}"
    try:
        r = SESSION.post(
            url,
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=timeout
        )
        try:
            data = r.json()
        except ValueError:
            return None
        if r.status_code >= 400:
            return None
        return data
    except requests.RequestException:
        return None

def get_quote(symbol, exchange):
    data = api_post("quotes", {"symbol": symbol, "exchange": exchange}, timeout=5)
    if data and data.get("status") == "success":
        return float(data["data"]["ltp"])
    return None

def get_index_price(cfg):
    return get_quote(cfg["index_symbol"], cfg["index_exchange"])

def get_atm_strike(price, interval):
    return round(price / interval) * interval

def get_expiry(cfg):
    today = get_ist_now().date()
    days_ahead = (cfg["expiry_day"] - today.weekday()) % 7
    expiry = today if days_ahead == 0 else today + timedelta(days=days_ahead)
    months = ["JAN","FEB","MAR","APR","MAY","JUN",
              "JUL","AUG","SEP","OCT","NOV","DEC"]
    return f"{expiry.day:02d}{months[expiry.month-1]}{expiry.year % 100:02d}"

def build_symbol(name, cfg, strike, option_type):
    expiry = get_expiry(cfg)
    return f"{name}{expiry}{strike}{option_type}"

def place_order(symbol, exchange, action, quantity):
    payload = {
        "symbol":    symbol,
        "exchange":  exchange,
        "action":    action,
        "quantity":  quantity,
        "price":     0,
        "pricetype": "MARKET",
        "product":   "MIS",
        "strategy":  "NIFTY SENSEX Scalp",
    }
    data = api_post("placeorder", payload, timeout=10)
    if data and data.get("status") == "success":
        return data.get("orderid")
    return None

# ============================================================
# STRATEGY RUNNER
# ============================================================

def run_instrument(name, cfg):
    log(name, f"Started | Direction: {cfg['direction_pts']}pts | "
              f"Target: {cfg['target_pts']}pts | SL: {cfg['sl_pts']}pts")

    total_pnl        = 0
    trade_count      = 0
    ref_price        = None
    last_ref_update  = None

    # ── Wait for market open and capture first reference ──
    while ref_price is None:
        if get_time_str() >= "09:15":
            ref_price = get_index_price(cfg)
            if ref_price:
                last_ref_update = get_ist_now()
                log(name, f"Initial reference price: {ref_price}")
        time.sleep(5)

    # ── Main loop ─────────────────────────────────────────
    while True:
        now_str = get_time_str()
        now     = get_ist_now()

        # Hard exit
        if now_str >= EXIT_TIME:
            log(name, f"3:30 PM — Done. Trades: {trade_count} | Total P&L: Rs {total_pnl:.0f}")
            break

        # Wait for entry time
        if now_str < ENTRY_TIME:
            time.sleep(10)
            continue

        # ── Update rolling reference every 15 minutes ─────
        if (now - last_ref_update).seconds >= REFERENCE_INTERVAL:
            new_ref = get_index_price(cfg)
            if new_ref:
                ref_price       = new_ref
                last_ref_update = now
                log(name, f"🔄 Rolling reference updated: {ref_price}")

        # ── Check for signal ──────────────────────────────
        index_now = get_index_price(cfg)
        if index_now is None:
            time.sleep(10)
            continue

        move = index_now - ref_price
        log(name, f"Ref: {ref_price} | Now: {index_now} | Move: {move:.0f}pts")

        if move >= cfg["direction_pts"]:
            option_type = "CE"
        elif move <= -cfg["direction_pts"]:
            option_type = "PE"
        else:
            log(name, f"No signal ({move:.0f}pts / {cfg['direction_pts']}pts needed). Waiting...")
            time.sleep(SIGNAL_INTERVAL)
            continue

        # ── Entry ─────────────────────────────────────────
        atm_strike    = get_atm_strike(index_now, cfg["strike_interval"])
        option_symbol = build_symbol(name, cfg, atm_strike, option_type)
        log(name, f"Signal: {option_type} | Symbol: {option_symbol}")

        entry_premium = get_quote(option_symbol, cfg["option_exchange"])
        if entry_premium is None:
            log(name, "Could not fetch premium. Retrying in 15s.")
            time.sleep(15)
            continue

        log(name, f"Entering trade #{trade_count + 1} | Premium: Rs {entry_premium}")
        order_id = place_order(option_symbol, cfg["option_exchange"], "BUY", cfg["quantity"])

        if not order_id:
            log(name, "Entry order failed. Retrying in 30s.")
            time.sleep(30)
            continue

        trade_count   += 1
        position_open  = True

        # Reset reference after entry so we don't re-enter same signal
        ref_price       = index_now
        last_ref_update = get_ist_now()
        log(name, f"Reference reset to {ref_price} after entry.")

        # ── Monitor trade ─────────────────────────────────
        while position_open:
            now_str = get_time_str()

            # Hard exit
            if now_str >= EXIT_TIME:
                log(name, "3:30 PM — Hard exiting open position.")
                place_order(option_symbol, cfg["option_exchange"], "SELL", cfg["quantity"])
                curr      = get_quote(option_symbol, cfg["option_exchange"]) or entry_premium
                trade_pnl = (curr - entry_premium) * cfg["quantity"]
                total_pnl += trade_pnl
                log(name, f"Hard exit P&L: Rs {trade_pnl:.0f}")
                position_open = False
                break

            curr = get_quote(option_symbol, cfg["option_exchange"])
            if curr is None:
                time.sleep(5)
                continue

            pnl_pts = curr - entry_premium
            pnl_rs  = pnl_pts * cfg["quantity"]
            log(name, f"Premium: {curr} | P&L: {pnl_pts:.1f}pts = Rs {pnl_rs:.0f}")

            # Target hit
            if pnl_pts >= cfg["target_pts"]:
                log(name, f"TARGET HIT! +{pnl_pts:.1f}pts = Rs {pnl_rs:.0f} profit 🎯")
                place_order(option_symbol, cfg["option_exchange"], "SELL", cfg["quantity"])
                total_pnl    += pnl_rs
                position_open = False
                log(name, f"Total P&L: Rs {total_pnl:.0f} | Looking for next signal...")

            # SL hit
            elif pnl_pts <= -cfg["sl_pts"]:
                log(name, f"SL HIT! -{abs(pnl_pts):.1f}pts = Rs {abs(pnl_rs):.0f} loss 🛑")
                place_order(option_symbol, cfg["option_exchange"], "SELL", cfg["quantity"])
                total_pnl    += pnl_rs
                position_open = False
                log(name, f"Total P&L: Rs {total_pnl:.0f} | Looking for next signal...")

            if position_open:
                time.sleep(10)

        # Wait before next signal check
        if get_time_str() < EXIT_TIME:
            log(name, f"Waiting {SIGNAL_INTERVAL}s before next signal...")
            time.sleep(SIGNAL_INTERVAL)

    log(name, f"=== Day Summary: {trade_count} trades | Final P&L: Rs {total_pnl:.0f} ===")

# ============================================================
# MAIN
# ============================================================

def main():
    print(f"[{get_ist_now().strftime('%H:%M:%S')}] Starting NIFTY + SENSEX Scalp Strategy")
    print("=" * 60)

    threads = []
    for name, cfg in INSTRUMENTS.items():
        t = threading.Thread(target=run_instrument, args=(name, cfg), daemon=True)
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

    print("=" * 60)
    print(f"[{get_ist_now().strftime('%H:%M:%S')}] All strategies complete for the day.")

if __name__ == "__main__":
    main()
