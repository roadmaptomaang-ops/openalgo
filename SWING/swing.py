"""
SWING — multi-day equity swing book (one decision per morning)
==============================================================
Built 2026-07-11. The slow book: 2-10 day holds on the backtest-proven
NIFTY-50 list, entered on daily-timeframe breakouts, exited on a wide
ATR trailing stop. A handful of trades per month — fixed per-trade
costs become a rounding error, and winners get days to compound
instead of being force-liquidated at 15:25.

Run ONCE each morning after 09:20 (`./run_swing.sh`). It:
  1. pulls ~90 days of daily candles per symbol,
  2. EXITS any open position whose trailing stop was hit
     (yesterday's close < highest-close-since-entry - 2.5xATR20,
      or close < SMA21 = trend over),
  3. ENTERS (max 4 concurrent, Rs50k each) where yesterday closed at
     a fresh 20-day closing high with SMA9 > SMA21,
  4. logs and exits the process. No daemon, no monitoring loop —
     the stop is evaluated on daily closes, by design.

Product is CNC (delivery) so positions survive overnight — the whole
point. Isolation: strategy label "swing" -> logs/YYYY-MM-DD/swing/,
logs/swing_master.csv, trading.db (SAGE ingests automatically). Open
positions persist in SWING/positions.json between runs.
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytz

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "PIT_codex_fixes"))
from scalper_pro_v4_3 import api_post, get_quote, verify_fill  # noqa: E402
from trade_logger import TradeLogger  # noqa: E402

API_KEY = os.environ.get("OPENALGO_API_KEY", "")
if not API_KEY:
    raise EnvironmentError("Set OPENALGO_API_KEY before running.")

STRATEGY_TAG = "SWING"
IST = pytz.timezone("Asia/Kolkata")
STATE_FILE = Path(__file__).resolve().parent / "positions.json"

EXCHANGE        = "NSE"
PRODUCT         = "CNC"     # delivery — positions live for days
POSITION_RS     = 50_000.0
MAX_POSITIONS   = 4
BREAKOUT_DAYS   = 20        # fresh N-day closing high
BREAKOUT_MIN_ATR = 0.25     # must clear the prior high by this xATR — a close
                            # 0.03% above the high is noise, not a breakout
ATR_PERIOD      = 20
ATR_STOP_MULT   = 2.5       # wide by design — daily noise must not shake us out
SMA_FAST, SMA_SLOW = 9, 21  # the 5yr-backtested regime filter on this universe
HISTORY_DAYS    = 90
MAX_HOLD_DAYS   = 15        # sanity ceiling; normally the stop exits first

# Same 5yr-backtest-proven universe Trend Rider trades (SMA 9/21 positive
# return + PF > 1). Copied, not imported — SWING must not depend on the
# intraday engine's module.
UNIVERSE = [
    "BRITANNIA", "BAJFINANCE", "WIPRO", "IOC", "HINDALCO",
    "ASIANPAINT", "INDUSINDBK", "BPCL", "TCS", "SUNPHARMA",
    "HEROMOTOCO", "HCLTECH", "GRASIM", "RELIANCE", "BHARTIARTL",
    "SBIN", "TATASTEEL", "POWERGRID", "NTPC", "COALINDIA",
]

_TL = TradeLogger("swing")


def now_ist():
    return datetime.now(IST)


def log(msg):
    _TL.activity("SWING", msg)


def tlog(msg):
    _TL.trade("SWING", msg)


# ============================================================
# STATE
# ============================================================
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"positions": {}}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ============================================================
# DATA
# ============================================================
def daily_candles(symbol):
    end = now_ist().strftime("%Y-%m-%d")
    start = (now_ist() - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
    data = api_post("history", {
        "symbol": symbol, "exchange": EXCHANGE, "interval": "D",
        "start_date": start, "end_date": end,
    })
    if not data or "data" not in data or not data["data"]:
        return None
    df = pd.DataFrame(data["data"]).sort_values("timestamp").reset_index(drop=True)
    # drop today's forming candle if present — decisions use CLOSED days only
    df["ts"] = pd.to_datetime(df["timestamp"], unit="s")
    today = now_ist().strftime("%Y-%m-%d")
    df = df[df["ts"].dt.strftime("%Y-%m-%d") < today].reset_index(drop=True)
    return df if len(df) >= SMA_SLOW + 2 else None


def atr(df, period=ATR_PERIOD):
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])


# ============================================================
# ORDERS
# ============================================================
def market_order(symbol, action, qty):
    data = api_post("placeorder", {
        "symbol": symbol, "exchange": EXCHANGE, "action": action,
        "quantity": qty, "price": 0, "pricetype": "MARKET",
        "product": PRODUCT, "strategy": STRATEGY_TAG,
    })
    oid = data.get("orderid") if data and data.get("status") == "success" else None
    if not oid:
        return None
    fallback = get_quote(symbol, EXCHANGE)
    return verify_fill(oid, fallback_price=fallback, fallback_qty=qty,
                       strategy=STRATEGY_TAG)


# ============================================================
# EXITS  (evaluated on yesterday's close — daily-timeframe stops)
# ============================================================
def check_exits(state):
    closed_rs = 0.0
    for sym in list(state["positions"].keys()):
        pos = state["positions"][sym]
        df = daily_candles(sym)
        if df is None:
            log(f"{sym}: no daily data — holding, will retry tomorrow.")
            continue

        last_close = float(df["close"].iloc[-1])
        sma_slow = float(df["close"].rolling(SMA_SLOW).mean().iloc[-1])
        pos["high_close"] = max(pos["high_close"], last_close)
        trail_stop = pos["high_close"] - ATR_STOP_MULT * pos["atr"]
        held_days = (now_ist().date()
                     - datetime.strptime(pos["entry_date"], "%Y-%m-%d").date()).days

        reason = None
        if last_close < trail_stop:
            reason = "ATR Trail Stop"
        elif last_close < sma_slow:
            reason = "Trend Over (SMA21)"
        elif held_days >= MAX_HOLD_DAYS:
            reason = "Max Hold"

        if not reason:
            save_state(state)   # persist the updated peak — tomorrow's trail depends on it
            log(f"HOLD {sym}: close {last_close:.2f} | trail {trail_stop:.2f} | "
                f"SMA21 {sma_slow:.2f} | peak {pos['high_close']:.2f} | day {held_days}")
            continue

        exit_p = market_order(sym, "SELL", pos["qty"])
        if exit_p is None:
            log(f"EXIT FAILED for {sym} — will retry tomorrow.")
            continue

        pnl_pts = exit_p - pos["entry_price"]
        pnl_rs = pnl_pts * pos["qty"]
        mfe_pts = pos["high_close"] - pos["entry_price"]
        closed_rs += pnl_rs
        tlog(f"EXIT {sym} {reason} | {pos['entry_price']:.2f} -> {exit_p:.2f} "
             f"x{pos['qty']} | {held_days}d | Rs {pnl_rs:+.0f}")
        _TL.csv_row({
            "date": pos["entry_date"],
            "entry_time": pos.get("entry_clock", "09:25:00"),
            "exit_time": now_ist().strftime("%H:%M:%S"),
            "strategy": "swing",
            "instrument": "EQUITY",
            "direction": "BUY",
            "symbol": sym,
            "strike_type": "SWING",
            "entry_price": round(pos["entry_price"], 2),
            "exit_price": round(exit_p, 2),
            "quantity": pos["qty"],
            "pnl_pts": round(pnl_pts, 2),
            "pnl_rs": round(pnl_rs, 0),
            "mfe_pts": round(max(0.0, mfe_pts), 2),
            "mae_pts": "",
            "capture_pct": round(max(0.0, pnl_pts / mfe_pts * 100), 1) if mfe_pts > 0 else 0,
            "exit_reason": reason,
            "hold_seconds": held_days * 86400,
            "atr_entry": round(pos["atr"], 2),
            "momentum": "Swing breakout",
            "high_water_pts": round(max(0.0, mfe_pts), 2),
            "running_total": round(pnl_rs, 0),
        })
        del state["positions"][sym]
        save_state(state)
    return closed_rs


# ============================================================
# ENTRIES  (yesterday closed at a fresh 20-day high, SMA9 > SMA21)
# ============================================================
def scan_entries(state):
    slots = MAX_POSITIONS - len(state["positions"])
    if slots <= 0:
        log(f"Book full ({MAX_POSITIONS} positions) — no new entries.")
        return

    candidates = []
    for sym in UNIVERSE:
        if sym in state["positions"]:
            continue
        df = daily_candles(sym)
        if df is None:
            continue
        closes = df["close"]
        last = float(closes.iloc[-1])
        prior_high = float(closes.iloc[-(BREAKOUT_DAYS + 1):-1].max())
        sma_f = float(closes.rolling(SMA_FAST).mean().iloc[-1])
        sma_s = float(closes.rolling(SMA_SLOW).mean().iloc[-1])
        a = atr(df)
        if last >= prior_high + BREAKOUT_MIN_ATR * a and sma_f > sma_s:
            strength = (last - prior_high) / prior_high * 100
            candidates.append((strength, sym, last, a))
            log(f"SIGNAL {sym}: fresh {BREAKOUT_DAYS}d high {last:.2f} "
                f"(+{strength:.2f}% above prior) SMA{SMA_FAST}>{SMA_SLOW}")
        time.sleep(0.3)   # be gentle on the history endpoint

    if not candidates:
        log("No breakout signals today.")
        return

    for strength, sym, last_close, a in sorted(candidates, reverse=True)[:slots]:
        qty = max(1, int(POSITION_RS / last_close))
        fill = market_order(sym, "BUY", qty)
        if fill is None:
            log(f"ENTRY FAILED for {sym} — skipping.")
            continue
        state["positions"][sym] = {
            "entry_date": now_ist().strftime("%Y-%m-%d"),
            "entry_clock": now_ist().strftime("%H:%M:%S"),
            "entry_price": fill,
            "qty": qty,
            "atr": a,
            "high_close": fill,
        }
        save_state(state)
        tlog(f"ENTRY {sym} BUY {qty} @ {fill:.2f} | breakout +{strength:.2f}% | "
             f"initial stop {fill - ATR_STOP_MULT * a:.2f} ({ATR_STOP_MULT}xATR {a:.2f})")


# ============================================================
# MAIN — run once, decide, exit
# ============================================================
def main():
    state = load_state()
    tlog("=" * 60)
    tlog(f"SWING morning run | {now_ist().strftime('%Y-%m-%d %H:%M')} | "
         f"open positions: {len(state['positions'])}/{MAX_POSITIONS}")
    tlog("=" * 60)

    if now_ist().weekday() >= 5:
        tlog("Weekend — nothing to do.")
        return

    closed = check_exits(state)
    scan_entries(state)

    state = load_state()
    if state["positions"]:
        tlog("BOOK: " + " | ".join(
            f"{s} {p['qty']}@{p['entry_price']:.2f} (peak {p['high_close']:.2f})"
            for s, p in state["positions"].items()))
    else:
        tlog("BOOK: flat.")
    if closed:
        tlog(f"Closed today: Rs {closed:+.0f}")
    tlog("SWING run complete — see you tomorrow morning.")


if __name__ == "__main__":
    main()
