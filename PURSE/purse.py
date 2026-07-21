"""
PURSE — capital-aware, single-position options trader (small account)
=====================================================================
Built 2026-07-11. Unlike every other engine in this repo (which all
assume the ~Rs1 Crore sandbox balance), PURSE models a REAL, FINITE
wallet — the way you'd actually trade Rs 15-20k. Its distinctive organ
is the LEDGER, not the signal:

  * Starts with a fixed balance (PURSE_CAPITAL, default Rs 20,000).
  * Holds AT MOST ONE position at a time — when money is in a trade it
    is LOCKED; nothing else can enter until that trade exits.
  * Before every entry it PRICES the trade (premium x lot + round-trip
    cost) and refuses anything it cannot afford from the CURRENT
    balance. As the balance grows or shrinks, what it can afford moves
    with it.
  * Auto-picks the instrument + strike that fits the wallet: tries
    NIFTY / SENSEX (BANKNIFTY optional), steps a few strikes OTM only
    as far as needed to fit the budget, and skips anything too far OTM
    to be a real trade (no lottery tickets).
  * Hard per-trade risk cap: MAX_LOSS_RS (default Rs 2,000, ~10% of a
    20k account). Because position size is forced (one lot is most of
    the wallet), the STOP is the real risk control, not sizing.

The SIGNAL is deliberately a thin, swappable placeholder (ORB + EMA
confirmation) behind a clean `Signal` interface — the point of this
build is the money-management layer. Replace `Signal.evaluate` with a
real edge once the ledger discipline is proven.

Isolation: label "purse" -> logs/YYYY-MM-DD/purse/, logs/purse_master.csv,
trading.db (SAGE ingests automatically). Wallet state persists in
PURSE/ledger.json; an open position survives restarts via
PURSE/position.json (crash recovery resumes monitoring it).
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pytz

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "PIT_codex_fixes"))
from scalper_pro_v4_3 import (  # noqa: E402
    api_post, get_quote, fetch_candles,
    calc_ema, calc_adx, get_orb, get_atm_strike,
    build_option_symbol, verify_fill,
)
from trade_logger import TradeLogger  # noqa: E402
from squareoff import squareoff_requested  # noqa: E402  shared square-off flag

API_KEY = os.environ.get("OPENALGO_API_KEY", "")
if not API_KEY:
    raise EnvironmentError("Set OPENALGO_API_KEY before running.")

STRATEGY_TAG = "PURSE"
IST = pytz.timezone("Asia/Kolkata")
HERE = Path(__file__).resolve().parent
LEDGER_FILE   = HERE / "ledger.json"
POSITION_FILE = HERE / "position.json"

# ── wallet / risk ──
START_CAPITAL = float(os.environ.get("PURSE_CAPITAL", "20000"))
MAX_LOSS_RS   = 1200.0     # hard per-trade stop (~6% of a 20k account)
                            # tightened from Rs2000 on 2026-07-17: PURSE has no
                            # pattern-trap/regime history filter like MINT, so its
                            # single-trade variance (-908, -2120, +2958 in one week)
                            # is the biggest lever on whether a day nets red or green.
                            # NOTE: the -2120 loss on 07-17 already blew through the
                            # old Rs2000 cap — this is a polled software stop, not a
                            # broker-side stop order, so it can slip past the level
                            # between checks (same slippage MINT's comments warn about
                            # for V5.1). Tightening the cap doesn't guarantee the fill,
                            # it lowers the level slippage has to blow through.
COST_RT       = 55.0       # round-trip charges reserved per trade
MIN_PREMIUM   = 20.0       # skip options cheaper than this (dead lottery tickets)
MAX_OTM_STEPS = 3          # never reach further OTM than this to "fit" the budget
CAPITAL_BUFFER = 1.00      # fraction of balance usable (1.0 = all of it, minus cost)

# ── session / cadence ──
ENTRY_START, LATE_ENTRY, HARD_EXIT = "09:25", "14:45", "15:15"
SIGNAL_INTERVAL, MONITOR_INTERVAL, MONITOR_ARMED = 10, 5, 1
ORB_CANDLES = 3
CONFIRM_ADX = 20.0

# ── regime gate (whipsaw guard) ──
# 2026-07-14 lesson: PURSE took ORB breakouts with no "is there a real trend?"
# check and got stopped TWICE on a day with HIGH ADX (25-41) but ~zero net
# direction — a whipsaw. ADX alone is useless there; directional EFFICIENCY
# (|net move| / range over a recent window) is the tell (that day it read
# 0.07-0.17). Require a minimum efficiency before taking any breakout.
REGIME_GATE_ENABLED   = True
REGIME_MIN_EFFICIENCY = 0.40    # below this = chop/whipsaw → stand down.
                                # Measured WHOLE-DAY (open→now), like THETA — a
                                # short rolling window reads a weak drift as
                                # "efficient" and lets the whipsaw through
                                # (verified: 2026-07-14's two losing entries read
                                # 0.25-0.27 whole-day but 0.77-0.78 over 45 candles).
_regime_log_ts = {}             # throttle the stand-down log (per instrument)

# ── exit management — MINT's capture ratchet, ported directly ──
# 2026-07-17: the old R-multiple gates (breakeven at 1R, trail at 2R = 200pts
# on a 100pt stop) almost never armed — MINT/V5.1 arm off small ABSOLUTE
# point moves (6-18pts), not multiples of a risk-sized stop. First live trade
# peaked at 162pts (1.62R) and got zero trailing protection because it never
# reached the 200pt trail gate. Replaced with MINT's proven formula: arm once
# peak reaches ARM_FRAC of the stop distance (~15pts on a 100pt stop, in the
# same range as MINT's SENSEX arm_pts=14), then floor = entry + 50% of peak,
# rises only, never below entry once armed.
ARM_FRAC   = 0.15   # arm once peak profit reaches this fraction of stop_pts
TRAIL_FRAC = 0.50   # once armed, floor = entry + 50% of peak profit, ratchets up

# Instruments, cheapest-lot first so a thin wallet still finds a trade.
# BANKNIFTY off by default (weekly options discontinued → monthly only,
# poorer intraday fit); flip enabled=True if your broker/expiry supports it.
INSTRUMENTS = [
    {"name": "SENSEX", "index_symbol": "SENSEX", "index_exchange": "BSE_INDEX",
     "option_exchange": "BFO", "lot": 20, "strike_interval": 100,
     "expiry_day": 3, "enabled": True},
    {"name": "NIFTY", "index_symbol": "NIFTY", "index_exchange": "NSE_INDEX",
     "option_exchange": "NFO", "lot": 65, "strike_interval": 50,
     "expiry_day": 1, "enabled": True},
    {"name": "BANKNIFTY", "index_symbol": "BANKNIFTY", "index_exchange": "NSE_INDEX",
     "option_exchange": "NFO", "lot": 35, "strike_interval": 100,
     "expiry_day": 2, "enabled": False},
]

_TL = TradeLogger("purse")


def now_ist():
    return datetime.now(IST)


def tstr():
    return now_ist().strftime("%H:%M")


def log(name, msg):
    _TL.activity(name, msg)


def tlog(name, msg):
    _TL.trade(name, msg)


# ============================================================
# THE LEDGER  (the distinctive organ)
# ============================================================
class Ledger:
    def __init__(self):
        if LEDGER_FILE.exists():
            self.d = json.loads(LEDGER_FILE.read_text())
        else:
            self.d = {
                "start_capital": START_CAPITAL,
                "balance": START_CAPITAL,
                "peak": START_CAPITAL,
                "realized_pnl": 0.0,
                "trades": 0,
                "wins": 0,
            }
            self.save()

    def save(self):
        LEDGER_FILE.write_text(json.dumps(self.d, indent=2))

    @property
    def balance(self):
        return self.d["balance"]

    def spendable(self):
        """What we can actually deploy: balance minus the reserved round-trip
        cost, scaled by the usage buffer. Never negative."""
        return max(0.0, self.d["balance"] * CAPITAL_BUFFER - COST_RT)

    def book(self, pnl_rs):
        self.d["balance"] += pnl_rs
        self.d["realized_pnl"] += pnl_rs
        self.d["peak"] = max(self.d["peak"], self.d["balance"])
        self.d["trades"] += 1
        if pnl_rs > 0:
            self.d["wins"] += 1
        self.save()

    def summary(self):
        d = self.d
        wr = (d["wins"] / d["trades"] * 100) if d["trades"] else 0.0
        ret = (d["balance"] / d["start_capital"] - 1) * 100
        return (f"balance Rs {d['balance']:.0f} (start {d['start_capital']:.0f}, "
                f"{ret:+.1f}%) | peak {d['peak']:.0f} | "
                f"{d['trades']} trades {wr:.0f}% WR | realized Rs {d['realized_pnl']:+.0f}")


# ============================================================
# SIGNAL  (thin, swappable placeholder — replace with a real edge)
# ============================================================
class Signal:
    """Placeholder ORB-breakout + EMA/ADX confirmation. Returns 'CE', 'PE',
    or None for a given instrument. The whole rest of PURSE is signal-agnostic
    — swap this class out without touching the ledger/affordability logic."""

    def evaluate(self, inst):
        df = fetch_candles({"index_symbol": inst["index_symbol"],
                            "index_exchange": inst["index_exchange"]})
        if df is None or len(df) < ORB_CANDLES + 22:
            return None, {}
        spot = float(df["close"].iloc[-1])
        orb_high, orb_low = get_orb(df, ORB_CANDLES)
        ema9, ema21 = calc_ema(df, 9), calc_ema(df, 21)
        adx = calc_adx(df)
        # directional efficiency, WHOLE-DAY: |net move since open| / day range.
        # ~1.0 = clean one-way trend · ~0 = whipsaw/chop within a range.
        rng = float(df["high"].max() - df["low"].min())
        drift = abs(float(df["close"].iloc[-1]) - float(df["close"].iloc[0]))
        efficiency = drift / rng if rng > 0 else 0.0
        snap = {"spot": spot, "orb_high": orb_high, "orb_low": orb_low,
                "ema9": ema9, "ema21": ema21, "adx": adx, "efficiency": efficiency}
        if adx != adx or adx < CONFIRM_ADX:        # nan guard + weak-trend skip
            return None, snap
        if spot > orb_high and ema9 > ema21:
            return "CE", snap
        if spot < orb_low and ema9 < ema21:
            return "PE", snap
        return None, snap


# ============================================================
# AFFORDABILITY  — price the trade before entering
# ============================================================
def affordable_option(inst, direction, spot, spendable):
    """Find the strike closest to ATM whose (premium x lot + cost) fits the
    wallet. For a BUYER, stepping OTM lowers premium → more affordable. Returns
    (strike, symbol, premium, cost_rs) or None if nothing sensible fits."""
    iv = inst["strike_interval"]
    lot = inst["lot"]
    atm = get_atm_strike(spot, iv)
    for step in range(0, MAX_OTM_STEPS + 1):
        strike = atm + step * iv if direction == "CE" else atm - step * iv
        sym = build_option_symbol(inst["name"], inst, strike, direction)
        prem = get_quote(sym, inst["option_exchange"])
        if prem is None or prem < MIN_PREMIUM:
            continue
        cost_rs = prem * lot + COST_RT
        if cost_rs <= spendable:
            return {"strike": strike, "symbol": sym, "premium": prem,
                    "cost_rs": cost_rs, "lot": lot}
    return None


def place(sym, exchange, action, qty):
    data = api_post("placeorder", {
        "symbol": sym, "exchange": exchange, "action": action,
        "quantity": qty, "price": 0, "pricetype": "MARKET",
        "product": "MIS", "strategy": STRATEGY_TAG,
    })
    oid = data.get("orderid") if data and data.get("status") == "success" else None
    if not oid:
        return None
    fallback = get_quote(sym, exchange)
    return verify_fill(oid, fallback_price=fallback, fallback_qty=qty,
                       strategy=STRATEGY_TAG)


# ============================================================
# POSITION lifecycle
# ============================================================
def open_position(ledger):
    """Scan instruments in wallet-friendly order; take the first affordable
    signal. Returns a position dict (also persisted) or None."""
    spendable = ledger.spendable()
    for inst in INSTRUMENTS:
        if not inst["enabled"]:
            continue
        direction, snap = Signal().evaluate(inst)
        if direction is None:
            continue

        # ── REGIME GATE: refuse breakouts on a chop/whipsaw day ──
        eff = snap.get("efficiency", 1.0)
        if REGIME_GATE_ENABLED and eff < REGIME_MIN_EFFICIENCY:
            _last = _regime_log_ts.get(inst["name"], 0.0)
            if time.monotonic() - _last >= 60:      # throttle to once/min
                log(inst["name"], f"REGIME GATE: {direction} breakout but efficiency "
                    f"{eff:.2f} < {REGIME_MIN_EFFICIENCY} (chop/whipsaw) — standing down. "
                    f"ADX {snap.get('adx', 0):.0f}.")
                _regime_log_ts[inst["name"]] = time.monotonic()
            continue

        pick = affordable_option(inst, direction, snap["spot"], spendable)
        if pick is None:
            log(inst["name"], f"SIGNAL {direction} but no strike fits wallet "
                f"(spendable Rs {spendable:.0f}, spot {snap['spot']:.0f}). Skipping.")
            continue

        fill = place(pick["symbol"], inst["option_exchange"], "BUY", pick["lot"])
        if fill is None:
            log(inst["name"], f"ENTRY FAILED for {pick['symbol']}.")
            continue
        fill = fill or pick["premium"]

        stop_pts = MAX_LOSS_RS / pick["lot"]        # premium drop = Rs2000 loss
        pos = {
            "instrument": inst["name"], "option_exchange": inst["option_exchange"],
            "direction": direction, "symbol": pick["symbol"],
            "strike": pick["strike"], "lot": pick["lot"],
            "entry": fill, "entry_time": now_ist().isoformat(),
            "cost_rs": round(fill * pick["lot"] + COST_RT, 0),
            "stop_pts": stop_pts, "r_rs": MAX_LOSS_RS,
            "peak_pts": 0.0, "mae_pts": 0.0,
            "stop_level": max(0.0, fill - stop_pts),
            "armed": False, "snap": snap,
        }
        POSITION_FILE.write_text(json.dumps(pos, indent=2))
        tlog(inst["name"],
             f"ENTRY {direction} {pick['symbol']} @ {fill:.2f} x{pick['lot']} | "
             f"cost Rs {pos['cost_rs']:.0f} of wallet Rs {ledger.balance:.0f} | "
             f"stop {pos['stop_level']:.2f} (-Rs{MAX_LOSS_RS:.0f})")
        return pos
    return None


def manage(pos, ledger):
    """Monitor an open position to exit, then book P&L to the ledger."""
    lot = pos["lot"]
    entry = pos["entry"]
    stop_pts = pos["stop_pts"]
    while True:
        if tstr() >= HARD_EXIT:
            return close(pos, ledger, "Time Exit")
        if squareoff_requested():
            log(pos["instrument"], "SQUARE-OFF requested — closing position now.")
            return close(pos, ledger, "Square-Off")

        ltp = get_quote(pos["symbol"], pos["option_exchange"])
        if ltp is None:
            time.sleep(MONITOR_INTERVAL)
            continue

        pnl_pts = ltp - entry
        pos["peak_pts"] = max(pos["peak_pts"], pnl_pts)
        pos["mae_pts"] = min(pos["mae_pts"], pnl_pts)

        # capture ratchet: arm off an absolute point move (MINT's formula),
        # then floor rises with peak — never re-checked against a giant
        # R-multiple that a normal pullback-from-peak trade never reaches.
        arm_pts = stop_pts * ARM_FRAC
        if pos["peak_pts"] >= arm_pts:
            floor = entry + pos["peak_pts"] * TRAIL_FRAC
            if floor > pos["stop_level"]:
                was_armed = pos["armed"]
                pos["armed"] = True
                pos["stop_level"] = floor
                tag = "ARM" if not was_armed else "TRAIL"
                log(pos["instrument"], f"{tag}: peak +{pos['peak_pts']:.2f} → "
                    f"stop {floor:.2f} (locking Rs {(floor - entry) * lot:.0f})")

        POSITION_FILE.write_text(json.dumps(pos, indent=2))

        if ltp <= pos["stop_level"]:
            reason = ("Trail Stop" if pos["armed"] and pos["stop_level"] > entry
                      else "Break-even Stop" if pos["stop_level"] == entry
                      else "Stop Loss")
            return close(pos, ledger, reason)

        time.sleep(MONITOR_ARMED if pos["armed"] else MONITOR_INTERVAL)


def close(pos, ledger, reason):
    lot = pos["lot"]
    exit_p = place(pos["symbol"], pos["option_exchange"], "SELL", lot)
    if exit_p is None:
        exit_p = get_quote(pos["symbol"], pos["option_exchange"]) or pos["entry"]
        log(pos["instrument"], f"CLOSE order failed — booking at quote {exit_p:.2f}. "
            f"CHECK POSITION BOOK.")
    pnl_pts = exit_p - pos["entry"]
    pnl_rs = pnl_pts * lot - COST_RT
    entry_time = datetime.fromisoformat(pos["entry_time"])
    held = int((now_ist() - entry_time).total_seconds())
    mfe = max(0.0, pos["peak_pts"])
    ledger.book(pnl_rs)

    tlog(pos["instrument"],
         f"EXIT {reason} {pos['symbol']} @ {exit_p:.2f} | {pnl_pts:+.2f}pts = "
         f"Rs {pnl_rs:+.0f} | wallet → Rs {ledger.balance:.0f}")
    _TL.csv_row({
        "date": entry_time.strftime("%Y-%m-%d"),
        "entry_time": entry_time.strftime("%H:%M:%S"),
        "exit_time": now_ist().strftime("%H:%M:%S"),
        "strategy": "purse",
        "instrument": pos["instrument"],
        "direction": pos["direction"],
        "symbol": pos["symbol"],
        "strike_type": "OPT",
        "entry_price": round(pos["entry"], 2),
        "exit_price": round(exit_p, 2),
        "quantity": lot,
        "pnl_pts": round(pnl_pts, 2),
        "pnl_rs": round(pnl_rs, 0),
        "mfe_pts": round(mfe, 2),
        "mae_pts": round(pos["mae_pts"], 2),
        "capture_pct": round(max(0.0, pnl_pts / mfe * 100), 1) if mfe > 0 else 0,
        "exit_reason": reason,
        "hold_seconds": held,
        "adx_entry": round(pos["snap"].get("adx", 0), 1),
        "ema9_entry": round(pos["snap"].get("ema9", 0), 2),
        "ema21_entry": round(pos["snap"].get("ema21", 0), 2),
        "orb_high": round(pos["snap"].get("orb_high", 0), 2),
        "orb_low": round(pos["snap"].get("orb_low", 0), 2),
        "momentum": "ORB breakout",
        "high_water_pts": round(mfe, 2),
        "running_total": round(ledger.d["realized_pnl"], 0),
    })
    POSITION_FILE.exists() and POSITION_FILE.unlink()
    return None


# ============================================================
# MAIN
# ============================================================
def main():
    ledger = Ledger()
    tlog("WALLET", "=" * 60)
    tlog("WALLET", "PURSE — capital-aware single-position options trader")
    tlog("WALLET", ledger.summary())
    tlog("WALLET", f"per-trade stop Rs {MAX_LOSS_RS:.0f} | one position at a time | "
                   f"instruments: " + ",".join(i["name"] for i in INSTRUMENTS if i["enabled"]))
    tlog("WALLET", f"regime gate: {'ON' if REGIME_GATE_ENABLED else 'OFF'} — skip breakouts "
                   f"when whole-day efficiency < {REGIME_MIN_EFFICIENCY} (whipsaw guard)")
    tlog("WALLET", "=" * 60)

    # crash recovery: resume an open position if one was left mid-flight
    if POSITION_FILE.exists():
        pos = json.loads(POSITION_FILE.read_text())
        log("WALLET", f"Resuming open position {pos['symbol']} from disk.")
        manage(pos, ledger)

    while True:
        if tstr() >= HARD_EXIT or squareoff_requested():
            _why = "Square-Off requested" if tstr() < HARD_EXIT else "Session end"
            tlog("WALLET", f"{_why}. {ledger.summary()}")
            return
        if tstr() < ENTRY_START or tstr() >= LATE_ENTRY:
            time.sleep(30)
            continue
        if ledger.spendable() < MIN_PREMIUM * 20:   # too broke for even the cheapest lot
            tlog("WALLET", f"Wallet Rs {ledger.balance:.0f} too small to trade. Stopping.")
            return

        pos = open_position(ledger)
        if pos is not None:
            manage(pos, ledger)      # blocks until this trade exits (one at a time)
        else:
            time.sleep(SIGNAL_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        tlog("WALLET", "Interrupted — if a position is open, position.json holds it; "
                       "restart to resume, or CHECK THE BOOK.")
