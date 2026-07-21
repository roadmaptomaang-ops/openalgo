"""
THETA — chop-day defined-risk premium seller (NIFTY iron condor)
================================================================
Built 2026-07-11. The inverse of every other engine in this book.

The book's own data (380+ trades): the market is in chop most days, and
every existing engine BUYS movement — so 60-70% of days are structurally
against them (the 40% book win rate is the market's chop ratio showing up
in the P&L). THETA sells that premium instead: on days classified as
chop, it sells a defined-risk NIFTY iron condor and lets time decay do
the work. It earns from time passing, not from predicting direction.

The three organs:
  1. DAY-TYPE CLASSIFIER (not a direction predictor):
     from 10:15, on 1m NIFTY candles: ADX + directional efficiency
     (|net drift| / range). Low ADX + low efficiency = CHOP -> deploy.
     Trend or unclear -> re-check every 15 min until the entry cutoff,
     else stay flat all day. Being flat is a position.
  2. DEFINED-RISK STRUCTURE: short strangle wrapped in wings
     (iron condor). Short strikes anchored OUTSIDE the day's traded
     range; wings a fixed width beyond. Max loss is capped by
     construction — no naked legs, ever.
  3. STRUCTURE-LEVEL RISK: profit-take at 60% of credit kept, stop at
     0.75x credit lost, hard close if spot touches a short strike,
     time exit 15:12. One structure per day, no re-entry.

Isolation: strategy label "theta" -> logs/YYYY-MM-DD/theta/,
logs/theta_master.csv, trading.db rows (SAGE ingests automatically).
The 4 legs are one trade economically, so it logs ONE row per condor:
entry_price = net credit (pts), exit_price = cost to close, direction
SELL, strike_type CONDOR. Costs: 4 legs = ~Rs110/RT (2x the Rs55 model)
— judge it net like everything else.
"""

import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pytz

# Reuse the proven, side-effect-free helpers from the production folder.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "PIT_codex_fixes"))
from scalper_pro_v4_3 import (  # noqa: E402
    api_post, get_quote, fetch_candles,
    calc_ema, calc_vwap, calc_atr, calc_adx, get_orb,
    get_atm_strike, get_expiry, build_option_symbol, verify_fill,
)
from trade_logger import TradeLogger  # noqa: E402
from squareoff import squareoff_requested  # noqa: E402  shared square-off flag

# ============================================================
# CONFIG
# ============================================================
API_KEY = os.environ.get("OPENALGO_API_KEY", "")
if not API_KEY:
    raise EnvironmentError("Set OPENALGO_API_KEY before running.")

STRATEGY_TAG = "THETA"
IST = pytz.timezone("Asia/Kolkata")

CFG = {
    "index_symbol": "NIFTY", "index_exchange": "NSE_INDEX",
    "option_exchange": "NFO", "quantity": 65, "strike_interval": 50,
    "expiry_day": 1, "orb_candles": 3,
}

CLASSIFY_FROM   = "10:15"   # first hour must be complete before judging the day
ENTRY_CUTOFF    = "13:00"   # after this, not enough theta left to harvest
HARD_EXIT       = "15:12"
RECHECK_SECS    = 900       # unclear day -> re-judge every 15 min
MONITOR_SECS    = 5

# classifier thresholds
CHOP_ADX_MAX        = 22.0   # 1m ADX below this = no trend
CHOP_EFFICIENCY_MAX = 0.45   # |drift|/range below this = going nowhere
TREND_ADX_MIN       = 30.0   # above = trend day, stand down for good

# structure
SHORT_RANGE_BUFFER  = 0.25   # short strikes beyond day range extended by this fraction
MIN_SHORT_OFFSET    = 100    # never sell closer than this many pts from spot
WING_WIDTH          = 100    # pts beyond the short strike (defines max loss)
MIN_CREDIT_PTS      = 12.0   # skip if the condor pays less than this (not worth the risk)

# risk (all as fractions of entry credit)
PROFIT_TAKE_FRAC    = 0.40   # buy back when cost-to-close <= 40% of credit (keep 60%)
STOP_FRAC           = 1.75   # buy back when cost-to-close >= 175% of credit (lose 75%)

_TL = TradeLogger("theta")


def now_ist():
    return datetime.now(IST)


def tstr():
    return now_ist().strftime("%H:%M")


def log(msg):
    _TL.activity("NIFTY", msg)


def tlog(msg):
    _TL.trade("NIFTY", msg)


# ============================================================
# ORGAN 1 — DAY-TYPE CLASSIFIER
# ============================================================
def classify_day():
    """Returns (verdict, snapshot). verdict: CHOP | TREND | UNCLEAR."""
    df = fetch_candles(CFG)
    if df is None or len(df) < 30:
        return "UNCLEAR", {}

    adx = calc_adx(df)
    day_high, day_low = float(df["high"].max()), float(df["low"].min())
    day_range = day_high - day_low
    drift = abs(float(df["close"].iloc[-1]) - float(df["open"].iloc[0]))
    efficiency = drift / day_range if day_range > 0 else 0.0
    orb_high, orb_low = get_orb(df, CFG["orb_candles"])

    snap = {
        "adx": adx if adx == adx else 0.0,
        "efficiency": efficiency,
        "day_high": day_high, "day_low": day_low, "day_range": day_range,
        "orb_high": orb_high, "orb_low": orb_low,
        "ema9": calc_ema(df, 9), "ema21": calc_ema(df, 21),
        "vwap": calc_vwap(df), "atr": calc_atr(df),
        "spot": float(df["close"].iloc[-1]),
    }

    if adx != adx:                       # nan guard — never classify blind
        return "UNCLEAR", snap
    if adx >= TREND_ADX_MIN or efficiency > 0.60:
        return "TREND", snap
    if adx < CHOP_ADX_MAX and efficiency < CHOP_EFFICIENCY_MAX:
        return "CHOP", snap
    return "UNCLEAR", snap


# ============================================================
# ORGAN 2 — THE STRUCTURE
# ============================================================
def place_leg(symbol, action, qty):
    data = api_post("placeorder", {
        "symbol": symbol, "exchange": CFG["option_exchange"], "action": action,
        "quantity": qty, "price": 0, "pricetype": "MARKET",
        "product": "MIS", "strategy": STRATEGY_TAG,
    })
    oid = data.get("orderid") if data and data.get("status") == "success" else None
    if not oid:
        return None
    fallback = get_quote(symbol, CFG["option_exchange"])
    price = verify_fill(oid, fallback_price=fallback, fallback_qty=qty,
                        strategy=STRATEGY_TAG)
    return price


def pick_strikes(snap):
    """Short strikes anchored outside the day's traded range (extended by a
    buffer), never closer than MIN_SHORT_OFFSET to spot. Wings beyond."""
    iv = CFG["strike_interval"]
    spot = snap["spot"]
    ext = snap["day_range"] * SHORT_RANGE_BUFFER
    ce_level = max(snap["day_high"] + ext, spot + MIN_SHORT_OFFSET)
    pe_level = min(snap["day_low"] - ext, spot - MIN_SHORT_OFFSET)
    short_ce = int(-(-ce_level // iv) * iv)          # ceil to strike
    short_pe = int(pe_level // iv * iv)              # floor to strike
    return {
        "short_ce": short_ce, "short_pe": short_pe,
        "wing_ce": short_ce + WING_WIDTH, "wing_pe": short_pe - WING_WIDTH,
    }


class Condor:
    def __init__(self, strikes, qty):
        self.qty = qty
        self.strikes = strikes
        self.legs = {  # leg -> (symbol, entry_action)
            "wing_pe":  (build_option_symbol("NIFTY", CFG, strikes["wing_pe"], "PE"),  "BUY"),
            "wing_ce":  (build_option_symbol("NIFTY", CFG, strikes["wing_ce"], "CE"),  "BUY"),
            "short_pe": (build_option_symbol("NIFTY", CFG, strikes["short_pe"], "PE"), "SELL"),
            "short_ce": (build_option_symbol("NIFTY", CFG, strikes["short_ce"], "CE"), "SELL"),
        }
        self.fills = {}
        self.entry_credit = 0.0
        self.entry_time = None

    def open(self):
        """Wings first (defined risk before short exposure), then shorts.
        Any failed leg -> unwind everything placed so far and abort."""
        placed = []
        for leg in ("wing_pe", "wing_ce", "short_pe", "short_ce"):
            sym, action = self.legs[leg]
            price = place_leg(sym, action, self.qty)
            if price is None:
                log(f"LEG FAILED: {action} {sym} — unwinding {len(placed)} placed leg(s).")
                for done in placed:
                    dsym, daction = self.legs[done]
                    place_leg(dsym, "SELL" if daction == "BUY" else "BUY", self.qty)
                return False
            self.fills[leg] = price
            placed.append(leg)
            log(f"LEG {action} {sym} @ {price:.2f}")
        self.entry_credit = (self.fills["short_pe"] + self.fills["short_ce"]
                             - self.fills["wing_pe"] - self.fills["wing_ce"])
        self.entry_time = now_ist()
        return self.entry_credit > 0

    def cost_to_close(self):
        """Current premium to buy back shorts minus what the wings fetch.
        None if any quote is missing (never risk-manage blind)."""
        q = {}
        for leg, (sym, _) in self.legs.items():
            p = get_quote(sym, CFG["option_exchange"])
            if p is None:
                return None
            q[leg] = p
        return q["short_pe"] + q["short_ce"] - q["wing_pe"] - q["wing_ce"]

    def close(self):
        """Unwind all legs: shorts first (kill the risk), then wings."""
        exit_cost = 0.0
        for leg in ("short_pe", "short_ce", "wing_pe", "wing_ce"):
            sym, entry_action = self.legs[leg]
            exit_action = "SELL" if entry_action == "BUY" else "BUY"
            price = place_leg(sym, exit_action, self.qty)
            if price is None:
                price = get_quote(sym, CFG["option_exchange"]) or self.fills[leg]
                log(f"CLOSE LEG FAILED for {sym} — booking at quote {price:.2f}; "
                    f"CHECK POSITION BOOK MANUALLY.")
            exit_cost += price if entry_action == "SELL" else -price
        return exit_cost


# ============================================================
# TRADE RECORD  (one row per condor — the structure is the trade)
# ============================================================
def record(condor, exit_cost, reason, snap, mfe_pts, mae_pts, verdict_log):
    pnl_pts = condor.entry_credit - exit_cost
    pnl_rs = pnl_pts * condor.qty
    t_out = now_ist()
    held = int((t_out - condor.entry_time).total_seconds())
    s = condor.strikes
    sym = f"NIFTY-IC {s['short_pe']}P/{s['short_ce']}C"

    sep = "=" * 60
    for ln in [
        "", sep,
        f"CONDOR | {condor.entry_time.strftime('%Y-%m-%d %H:%M:%S')} -> {t_out.strftime('%H:%M:%S')}",
        sep,
        f"  Structure      : short {s['short_pe']}PE + {s['short_ce']}CE, "
        f"wings {s['wing_pe']}PE / {s['wing_ce']}CE",
        f"  Classifier     : {verdict_log}",
        f"  Entry credit   : {condor.entry_credit:.2f}pts (Rs {condor.entry_credit * condor.qty:.0f})",
        f"  Exit cost      : {exit_cost:.2f}pts",
        f"  Exit reason    : {reason}",
        f"  Held           : {held}s",
        f"  Max win seen   : +{mfe_pts:.2f}pts | worst: {mae_pts:+.2f}pts",
        f"  P&L            : {pnl_pts:+.2f}pts = Rs {pnl_rs:+.0f}",
        sep, "",
    ]:
        tlog(ln)

    _TL.csv_row({
        "date": condor.entry_time.strftime("%Y-%m-%d"),
        "entry_time": condor.entry_time.strftime("%H:%M:%S"),
        "exit_time": t_out.strftime("%H:%M:%S"),
        "strategy": "theta",
        "instrument": "NIFTY",
        "direction": "SELL",
        "symbol": sym,
        "strike_type": "CONDOR",
        "entry_price": round(condor.entry_credit, 2),
        "exit_price": round(exit_cost, 2),
        "quantity": condor.qty,
        "pnl_pts": round(pnl_pts, 2),
        "pnl_rs": round(pnl_rs, 0),
        "mfe_pts": round(mfe_pts, 2),
        "mae_pts": round(mae_pts, 2),
        "capture_pct": round(max(0.0, pnl_pts / mfe_pts * 100), 1) if mfe_pts > 0 else 0,
        "exit_reason": reason,
        "hold_seconds": held,
        "adx_entry": round(snap.get("adx", 0), 1),
        "atr_entry": round(snap.get("atr", 0), 2),
        "ema9_entry": round(snap.get("ema9", 0), 2),
        "ema21_entry": round(snap.get("ema21", 0), 2),
        "vwap_entry": round(snap.get("vwap", 0), 2),
        "orb_high": round(snap.get("orb_high", 0), 2),
        "orb_low": round(snap.get("orb_low", 0), 2),
        "momentum": "Chop (classified)",
        "be_triggered": "", "trail_count": 0,
        "high_water_pts": round(mfe_pts, 2),
        "running_total": round(pnl_rs, 0),
    })
    return pnl_rs


# ============================================================
# MAIN
# ============================================================
def main():
    qty = CFG["quantity"]
    tlog("=" * 60)
    tlog("THETA — chop-day premium seller (NIFTY iron condor)")
    tlog(f"classify from {CLASSIFY_FROM} | entry cutoff {ENTRY_CUTOFF} | "
         f"hard exit {HARD_EXIT} | one condor/day")
    tlog(f"profit-take {PROFIT_TAKE_FRAC:.0%} of credit left | "
         f"stop {STOP_FRAC:.0%} of credit | wings {WING_WIDTH}pts")
    tlog("=" * 60)

    # already traded today? (rerun guard — one structure per day, DB-checked)
    import sqlite3
    try:
        con = sqlite3.connect(str(Path.home() / "openalgo" / "trading.db"))
        n = con.execute("SELECT COUNT(*) FROM trades WHERE date=? AND strategy='theta'",
                        (now_ist().strftime("%Y-%m-%d"),)).fetchone()[0]
        con.close()
        if n:
            tlog(f"{n} theta trade(s) already in DB today — one/day budget spent. Exiting.")
            return
    except Exception:
        pass

    # ── wait for the classification window ──
    while tstr() < CLASSIFY_FROM:
        time.sleep(30)

    # ── classify until CHOP or cutoff ──
    verdict, snap = "UNCLEAR", {}
    while tstr() < ENTRY_CUTOFF:
        verdict, snap = classify_day()
        log(f"CLASSIFIER: {verdict} | ADX {snap.get('adx', 0):.1f} "
            f"(chop<{CHOP_ADX_MAX}) | efficiency {snap.get('efficiency', 0):.2f} "
            f"(chop<{CHOP_EFFICIENCY_MAX}) | range {snap.get('day_range', 0):.0f}pts")
        if verdict == "CHOP":
            break
        if verdict == "TREND":
            tlog(f"TREND day (ADX {snap.get('adx', 0):.1f}, "
                 f"eff {snap.get('efficiency', 0):.2f}) — premium selling is the "
                 f"wrong side today. Standing down. Flat is a position.")
            return
        time.sleep(RECHECK_SECS)

    if verdict != "CHOP":
        tlog("Never classified CHOP before cutoff — no trade today.")
        return

    verdict_log = (f"CHOP @ {tstr()} (ADX {snap['adx']:.1f}, "
                   f"efficiency {snap['efficiency']:.2f})")

    # ── build & open the condor ──
    strikes = pick_strikes(snap)
    condor = Condor(strikes, qty)
    log(f"CHOP confirmed. Condor: short {strikes['short_pe']}PE/{strikes['short_ce']}CE "
        f"wings {strikes['wing_pe']}PE/{strikes['wing_ce']}CE | spot {snap['spot']:.0f}")

    if not condor.open():
        if condor.entry_credit <= 0 and condor.fills:
            tlog("Condor opened with non-positive credit — closing immediately.")
            condor.close()
        else:
            tlog("Condor entry failed — no trade today.")
        return

    credit_rs = condor.entry_credit * qty
    if condor.entry_credit < MIN_CREDIT_PTS:
        tlog(f"Credit {condor.entry_credit:.1f}pts < {MIN_CREDIT_PTS} minimum — "
             f"not worth the risk. Closing for a scratch.")
        exit_cost = condor.close()
        record(condor, exit_cost, "Credit Too Thin", snap, 0.0, 0.0, verdict_log)
        return

    tlog(f"CONDOR OPEN | credit {condor.entry_credit:.2f}pts = Rs {credit_rs:.0f} | "
         f"take-profit at cost<={condor.entry_credit * PROFIT_TAKE_FRAC:.1f} | "
         f"stop at cost>={condor.entry_credit * STOP_FRAC:.1f}")

    # ── monitor ──
    mfe = 0.0   # best structure P&L seen (pts)
    mae = 0.0
    reason = None
    while True:
        if tstr() >= HARD_EXIT:
            reason = "Time Exit"
            break
        if squareoff_requested():
            log("SQUARE-OFF requested — closing condor now.")
            reason = "Square-Off"
            break

        spot = get_quote(CFG["index_symbol"], CFG["index_exchange"])
        if spot is not None:
            if spot >= strikes["short_ce"] or spot <= strikes["short_pe"]:
                log(f"SHORT STRIKE TOUCHED (spot {spot:.0f}) — closing structure.")
                reason = "Strike Breach"
                break

        cost = condor.cost_to_close()
        if cost is not None:
            pnl_pts = condor.entry_credit - cost
            mfe = max(mfe, pnl_pts)
            mae = min(mae, pnl_pts)
            if cost <= condor.entry_credit * PROFIT_TAKE_FRAC:
                reason = "Profit Take"
                break
            if cost >= condor.entry_credit * STOP_FRAC:
                reason = "Credit Stop"
                break
            log(f"cost {cost:.2f} vs credit {condor.entry_credit:.2f} | "
                f"P&L {pnl_pts:+.2f}pts = Rs {pnl_pts * qty:+.0f} | spot {spot or 0:.0f}")

        time.sleep(MONITOR_SECS)

    exit_cost = condor.close()
    pnl_rs = record(condor, exit_cost, reason, snap, mfe, mae, verdict_log)
    tlog(f"THETA done for the day | {reason} | Rs {pnl_rs:+.0f}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        tlog("Interrupted — if a condor is open, CHECK THE POSITION BOOK.")
