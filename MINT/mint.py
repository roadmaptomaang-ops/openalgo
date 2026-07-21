"""
MINT — the post-PIT engine
============================================================
The successor to the PIT strategy family, designed from everything the data
proved (NEWPIT capture analysis, SAGE patterns, regime study, cost math).

One design law: NEVER GIVE BACK A GIFT — AND DON'T TRADE WHEN THERE ARE NO GIFTS.

Why the old engines lost (the evidence):
  · NEWPIT: the book captured 5% of the Rs117k the market OFFERED. Breakeven
    needs only ~13%. The 'SL Hit' exit path alone ran at -178% capture —
    winners round-tripped through the stop into losses. Exits, not entries.
  · Regime: avg-MFE collapsed 6.9pts -> 1.0pt when the market went trend->chop;
    every engine bled in chop. Nothing separates chop winners from losers AT
    ENTRY — so the only defence is to not trade chop at all.
  · SAGE patterns: high-ADX LONGS are registered TRAPs (0-46% WR); certain
    short setups are registered EDGEs (57-67% WR). The playbook was never
    consulted live — until now.
  · Costs: ~Rs55/round-trip. 20-trade days pay Rs1,100 before P&L. Quality
    over quantity is not a slogan, it's arithmetic.

MINT's four organs (each maps to one proven failure):
  1. REGIME GATE    — refuses to trade unless recent follow-through health
                      (rolling avg MFE from trading.db) says the market is
                      actually offering moves. In chop, MINT prints NOTHING —
                      which beats printing losses. (Would have sat out
                      24/25/30-Jun and today entirely.)
  2. PATTERN GATE   — before every entry, consults SAGE's pattern_registry
                      (the learned playbook). Registered TRAP -> skip.
                      A strategy that reads its own history live.
  3. CAPTURE RATCHET— the exit. Once a trade's peak (MFE) reaches the arm
                      threshold, a profit FLOOR locks at 50% of peak and only
                      ratchets up. A winner mathematically cannot round-trip
                      into a loss. Replayed on 197 historical scalper trades:
                      actual net -Rs14,718 -> MINT net +Rs10,110 (capture
                      -4.6% -> 24.6%). Plus a zero-MFE fast bailout (90s) so
                      dead trades cost points, not stops.
  4. BUDGET GOVERNOR— max 4 trades/day PER DAY (seeded from trading.db, so
                      reruns cannot re-arm the budget), halt after 2 straight
                      losses, and no entry unless the pattern's expectancy
                      clears 2x the round-trip cost.

HONESTY CLAUSE: nothing is a guaranteed money printer. MINT's numbers come from
replays of YOUR data (approximations: ratchet exits assume fill near the floor;
gaps/slippage will take a bite). Run it in paper (it is paper by default via the
OpenAlgo sandbox) for 10+ sessions and judge it by ONE metric: capture %.
As of 2026-07-02 the regime gauge reads CHOP -> MINT will refuse to trade.
That refusal IS the feature.

Run:
    export OPENALGO_API_KEY=<64-hex>
    cd ~/openalgo/MINT && caffeinate -i uv run python3 mint.py
Logs as strategy "mint" -> logs/YYYY-MM-DD/mint/, logs/mint_master.csv,
trading.db rows (SAGE ingests automatically).
"""

import os
import sqlite3
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import pytz

# Reuse the proven, side-effect-free helpers from the production folder.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "PIT_codex_fixes"))
from scalper_pro_v4_3 import (  # noqa: E402
    api_post, get_quote, fetch_candles, fetch_option_chain,
    calc_ema, calc_vwap, calc_atr, calc_adx, get_orb,
    analyze_option_chain, get_itm_strike, get_atm_strike,
    get_expiry, build_option_symbol, verify_fill, get_momentum_state,
)
from trade_logger import TradeLogger  # noqa: E402
from squareoff import squareoff_requested  # noqa: E402

# ============================================================
# CONFIG
# ============================================================
API_KEY = os.environ.get("OPENALGO_API_KEY", "")
if not API_KEY:
    raise EnvironmentError("Set OPENALGO_API_KEY before running.")

STRATEGY_TAG = "MINT"
IST = pytz.timezone("Asia/Kolkata")
TRADING_DB = Path.home() / "openalgo" / "trading.db"
SAGE_DB    = Path.home() / "openalgo" / "sage.db"

ENTRY_TIME, LATE_ENTRY_CUTOFF, EXIT_TIME = "09:25", "14:45", "15:20"
SIGNAL_INTERVAL, MONITOR_INTERVAL, MOM_CHECK_INTERVAL = 10, 5, 20
# Once the ratchet is ARMED there is locked profit to protect — poll fast so a
# violent reversal can't gap far through the floor between checks. (2026-07-08:
# V5.1's SENSEX trade had +111.3pts "locked" but filled at +55.1 — Rs1,123 of
# slippage in one 5s polling gap. The floor is a trigger, not a fill.)
MONITOR_INTERVAL_ARMED = 1
CANDLE_CONFIRM_COUNT = 2
STALE_QUOTE_EXIT_SECS = 25

# ── organ 4: budget governor ──
MAX_TRADES_PER_DAY   = 6       # per DAY, seeded from trading.db (reruns can't re-arm)
                                # raised from 4 on 2026-07-17: MINT is the only engine
                                # with a demonstrated edge (69% WR, 66.7% green days over
                                # its first 6 live days) — give it more room before the
                                # weaker/unfiltered engines (scalper_v51, purse) instead
MAX_CONSEC_LOSSES    = 2       # tighter than V5's 3: quality over quantity
COST_PER_RT          = 55.0    # Upstox all-in per round-trip
MIN_EDGE_MULTIPLE    = 2.0     # pattern expectancy must clear 2x cost to fire
COOLDOWN_AFTER_EXIT  = 240

# ── organ 1: regime gate ──
REGIME_WINDOW        = 12      # last N index-option trades in trading.db
REGIME_MIN_AVG_MFE   = 3.0     # pts — below this the market offers nothing: stand down
REGIME_RECHECK_SECS  = 900

# ── organ 3: capture ratchet ──
CAPTURE_RATIO        = 0.50    # floor locks at 50% of peak MFE, ratchets up only
ZERO_MFE_SECS        = 90      # dead-trade bailout window
ZERO_MFE_PTS         = 1.0

# ── organ 5: bleed guard (2026-07-17) ──
# Velocity-based accelerated exit. Fires FASTER than the fixed initial_sl_pts
# floor when the position is moving against us unusually fast, instead of
# waiting for a static points-based stop. Two independent triggers:
#   A) raw speed   — adverse move >= BLEED_ATR_MULT x ATR within BLEED_WINDOW_SECS
#   B) giveback    — armed trade gives back >=50% of its peak faster than it
#                     took to build the peak (a sharp reversal, not a normal pullback)
# Both need BLEED_CONFIRM_POLLS consecutive confirming polls (anti-whipsaw,
# same idiom as Scalper V5.1's 2-poll breakout confirm) and won't arm before
# BLEED_MIN_HOLD_SECS so it doesn't fight the entry's own initial noise.
# Tagged as its own exit_reason ("Bleed Guard") so it can be audited against
# SL Hit / Initial SL trades later — a velocity trigger risks cutting trades
# that would have recovered, and that only shows up over a couple weeks of data.
BLEED_ENABLED         = True
BLEED_MIN_HOLD_SECS   = 15
BLEED_WINDOW_SECS      = 30
BLEED_ATR_MULT        = 0.6
BLEED_GIVEBACK_FRAC   = 0.5
BLEED_CONFIRM_POLLS   = 2

# ── chase gate (NEW, 2026-07-08) ──────────────────────────
# Backtested on the full trade history: the 2nd+ same-direction entry taken
# right after the PRIOR same-direction entry WON is the worst trade type in
# the book (2nd-after-win avg Rs-157 vs 2nd-after-loss avg Rs-17). On MINT's
# own history the backtest was NET NEGATIVE (-Rs621) but on only 15 trades /
# ~4 days — too small to trust either way. Left OFF by default; flip on once
# there's a real sample. See PIT_codex_fixes/FIXES_AND_TODO.md.
CHASE_GATE_ENABLED   = False

INSTRUMENTS = {
    "NIFTY": {
        "index_symbol": "NIFTY", "index_exchange": "NSE_INDEX", "option_exchange": "NFO",
        "quantity": 65, "strike_interval": 50, "expiry_day": 1,
        "adx_threshold": 25, "orb_candles": 3, "orb_buffer": 20,
        "initial_sl_pts": 6, "arm_pts": 6,
    },
    "SENSEX": {
        "index_symbol": "SENSEX", "index_exchange": "BSE_INDEX", "option_exchange": "BFO",
        "quantity": 20, "strike_interval": 100, "expiry_day": 3,
        "adx_threshold": 25, "orb_candles": 3, "orb_buffer": 60,
        "initial_sl_pts": 15, "arm_pts": 14,
    },
}

_TL = TradeLogger("mint")


def now_ist():
    return datetime.now(IST)


def tstr():
    return now_ist().strftime("%H:%M")


def log(name, msg):
    _TL.activity(name, msg)


def tlog(name, msg):
    _TL.trade(name, msg)


# ============================================================
# ORGAN 1 — REGIME GATE  (don't trade when nothing is offered)
# ============================================================
class RegimeGate:
    """Rolling follow-through health from trading.db. MFE collapses BEFORE P&L
    does (6.9 -> 1.0pts at the June regime turn), so it's a leading gauge."""

    def __init__(self):
        self._last_check = 0.0
        self._ok = False
        self._avg = 0.0
        self._lock = threading.Lock()

    def check(self, name):
        with self._lock:
            if time.monotonic() - self._last_check < REGIME_RECHECK_SECS:
                return self._ok, self._avg
            try:
                con = sqlite3.connect(str(TRADING_DB))
                rows = con.execute(
                    "SELECT mfe_pts FROM trades WHERE strategy LIKE 'scalper%' OR strategy='mint' "
                    "ORDER BY date DESC, entry_time DESC LIMIT ?", (REGIME_WINDOW,)
                ).fetchall()
                con.close()
                vals = [r[0] or 0.0 for r in rows]
                self._avg = sum(vals) / len(vals) if vals else 0.0
                self._ok = self._avg >= REGIME_MIN_AVG_MFE
            except Exception as e:
                log(name, f"RegimeGate DB error ({e}) — failing CLOSED (no trade).")
                self._ok, self._avg = False, 0.0
            self._last_check = time.monotonic()
            state = "OFFERING MOVES — tradeable" if self._ok else "CHOP — stand down"
            log(name, f"REGIME GATE: rolling avg MFE {self._avg:.1f}pts "
                      f"(need >={REGIME_MIN_AVG_MFE}) -> {state}")
            return self._ok, self._avg


# ============================================================
# ORGAN 2 — PATTERN GATE  (consult the learned playbook, live)
# ============================================================
def _adx_band(adx):
    if adx < 20: return "choppy"
    if adx < 30: return "moderate"
    if adx < 45: return "strong"
    return "vstrong"


def _time_slot(hhmm):
    h, m = map(int, hhmm.split(":")[:2]); t = h + m / 60
    if t < 10.0:  return "open"
    if t < 11.5:  return "morning"
    if t < 13.25: return "midday"
    return "afternoon"


def pattern_gate(name, direction, adx):
    """Look up (options, adx_band, time_slot, LONG/SHORT) in SAGE's registry.
    TRAP -> block. EDGE -> allow (and report expectancy). Unknown -> allow,
    but expectancy defaults conservative. Fails OPEN only for missing DB."""
    dg = "LONG" if direction == "CE" else "SHORT"
    pid = f"OPT-{_adx_band(adx).upper()}-{_time_slot(tstr()).upper()}-{dg}"
    try:
        con = sqlite3.connect(str(SAGE_DB)); con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT description, win_rate, avg_pnl_rs, trade_count FROM pattern_registry "
            "WHERE pattern_id=?", (pid,)).fetchone()
        con.close()
    except Exception as e:
        log(name, f"PatternGate DB error ({e}) — allowing with default expectancy.")
        return True, 0.0, pid
    if row is None:
        log(name, f"PATTERN GATE: {pid} unknown (no history) — allowed, expectancy 0.")
        return True, 0.0, pid
    desc = row["description"] or ""
    exp = row["avg_pnl_rs"] or 0.0
    if desc.startswith("TRAP"):
        log(name, f"PATTERN GATE: {pid} is a registered TRAP "
                  f"({row['win_rate']*100:.0f}% WR, {row['trade_count']} trades) — BLOCKED.")
        return False, exp, pid
    log(name, f"PATTERN GATE: {pid} -> {desc.split(':')[0]} "
              f"({row['win_rate']*100:.0f}% WR, avg Rs{exp:+.0f}) — allowed.")
    return True, exp, pid


# ============================================================
# ORGAN 4 — BUDGET GOVERNOR (per-day, DB-seeded, cost-aware)
# ============================================================
class Governor:
    def __init__(self):
        self._lock = threading.Lock()
        self.consec_losses = 0
        self.halted = False
        self.reason = ""

    @staticmethod
    def today_db_count(instrument):
        """BUG FIX 2026-07-08: this used to count ALL mint trades today across
        BOTH instruments combined, so on a rerun each thread saw NIFTY+SENSEX's
        combined total (e.g. 6) instead of its own count (3) — both threads
        concluded the 4-trade budget was already blown and exited instantly on
        startup. Must filter by instrument; the daily cap is per-instrument."""
        try:
            con = sqlite3.connect(str(TRADING_DB))
            n = con.execute(
                "SELECT COUNT(*) FROM trades WHERE date=? AND strategy='mint' AND instrument=?",
                (now_ist().strftime("%Y-%m-%d"), instrument),
            ).fetchone()[0]
            con.close()
            return int(n)
        except Exception:
            return 0

    def record(self, pnl_rs):
        with self._lock:
            self.consec_losses = self.consec_losses + 1 if pnl_rs <= 0 else 0
            if self.consec_losses >= MAX_CONSEC_LOSSES and not self.halted:
                self.halted = True
                self.reason = f"{self.consec_losses} consecutive losses"

    def status(self):
        with self._lock:
            return self.halted, self.reason


regime = RegimeGate()
governor = Governor()


# ============================================================
# ORDERING
# ============================================================
def place_order(symbol, exchange, action, quantity):
    data = api_post("placeorder", {
        "symbol": symbol, "exchange": exchange, "action": action,
        "quantity": quantity, "price": 0, "pricetype": "MARKET",
        "product": "MIS", "strategy": STRATEGY_TAG,
    })
    return data.get("orderid") if data and data.get("status") == "success" else None


def market_order(symbol, exchange, action, qty, fallback=None):
    oid = place_order(symbol, exchange, action, qty)
    return verify_fill(oid, fallback_price=fallback, fallback_qty=qty, strategy=STRATEGY_TAG) if oid else None


# ============================================================
# TRADE RECORD -> shared logger/DB (SAGE-compatible)
# ============================================================
def check_bleed_guard(vel_hist, pts, held, mfe, peak_val, peak_time, atr, arm_pts, now, t_in):
    """Pure trigger check for ORGAN 5 (bleed guard) — no side effects, so it
    can be exercised in isolation (see test_bleed_guard.py) without a live
    broker connection. Returns (signal: bool, kind: 'velocity'|'giveback'|None).
    vel_hist must already contain (timestamp, pts) with the current sample
    appended and old samples outside BLEED_WINDOW_SECS trimmed."""
    if not BLEED_ENABLED or held < BLEED_MIN_HOLD_SECS:
        return False, None
    atr_ref = (atr or 0) or 1.0
    adverse_move = vel_hist[0][1] - pts if vel_hist else 0.0
    if adverse_move >= BLEED_ATR_MULT * atr_ref:
        return True, "velocity"
    if mfe >= arm_pts and peak_val > 0:
        giveback = peak_val - pts
        if giveback >= BLEED_GIVEBACK_FRAC * peak_val:
            build_secs = (peak_time - t_in).total_seconds()
            giveback_secs = (now - peak_time).total_seconds()
            if 0 < giveback_secs < build_secs:
                return True, "giveback"
    return False, None


def _diagnose(reason, pts, mfe, armed):
    """MINT-flavoured verdict on how the trade played out."""
    if reason == "Capture Floor":
        cap = pts / mfe * 100 if mfe > 0 else 0
        if cap >= 45:
            return ("RATCHET WIN", "Floor locked near half the peak and held. The gift was kept.")
        return ("RATCHET SAVE", "Winner reversed hard but the floor forbade a round-trip. "
                                "Old engines would have ridden this to a loss.")
    if reason == "Zero-MFE Bailout":
        return ("DEAD ENTRY, CHEAP", "Never went green; bailed in seconds for points, not a full stop.")
    if reason == "Initial SL":
        if mfe <= 1.0:
            return ("BAD ENTRY", "Straight against us. Entry timing off — the gates can't catch everything.")
        return ("UNARMED LOSS", f"Reached +{mfe:.1f}pts but never hit the arm threshold before reversing.")
    if reason.startswith("Momentum Flip"):
        return ("TREND OVER", "Underlying flipped while price sat near the floor — took the exit early.")
    if reason == "Hard Exit":
        return ("TIME EXIT", "Session close squared the position.")
    if reason == "Square-Off":
        return ("MANUAL SQUARE-OFF", "Operator squared the position off mid-session.")
    if reason == "Stale Quote Exit":
        return ("DATA GUARD", "Quote feed died — protective exit rather than flying blind.")
    if reason == "Bleed Guard":
        if armed:
            return ("FAST GIVEBACK", "Gave back the peak faster than it built — cut ahead of the "
                                      "fixed floor rather than waiting for a slower ratchet exit.")
        return ("VELOCITY CUT", "Moved against us unusually fast for the ATR — exited ahead of "
                                 "the fixed stop. Audit against Initial SL trades before trusting this.")
    return ("REVIEW", "Check the activity log.")


def record_trade(name, cfg, num, opt_type, symbol, entry, exitp, reason,
                 t_in, t_out, snap, mfe, mae, floor_log, total,
                 floor=None, armed=False, ex_e9=None, ex_e21=None):
    q = cfg["quantity"]; pts = exitp - entry; rs = pts * q
    cap = max(0.0, pts / mfe * 100) if mfe > 0 else 0
    tag, analysis = _diagnose(reason, pts, mfe, armed)
    sep = "=" * 60
    lines = [
        "", sep,
        f"TRADE #{num} | {name} MINT | {t_in.strftime('%Y-%m-%d')} | {t_in.strftime('%H:%M:%S')}",
        sep, "",
        "── MARKET CONDITIONS AT ENTRY ──────────────────────",
        f"  Spot Price     : {snap.get('spot', 0):.2f}",
        f"  ORB High       : {snap['orb_high']:.2f}",
        f"  ORB Low        : {snap['orb_low']:.2f}",
        f"  EMA9           : {snap['ema9']:.2f}",
        f"  EMA21          : {snap['ema21']:.2f}",
        f"  VWAP           : {snap['vwap']:.2f}",
        f"  ADX            : {snap['adx']:.1f} ({'Trending' if snap['adx'] >= 20 else 'Sideways'})",
        f"  ATR            : {snap['atr']:.2f}",
        f"  OI Bias        : {snap['oi_bias']}",
        f"  Momentum       : {get_momentum_state(snap['ema9'], snap['ema21'], snap['adx'])}",
        "",
        "── GATES AT ENTRY (why MINT took this) ─────────────",
        f"  Regime Gate    : rolling avg MFE {snap.get('regime_mfe', 0):.1f}pts "
        f"(needs >={REGIME_MIN_AVG_MFE}) — OPEN",
        f"  Pattern Gate   : {snap.get('pattern', '?')}"
        + (f"  (expectancy Rs {snap.get('pattern_exp', 0):+.0f})" if snap.get('pattern_exp') else "  (no history — neutral)"),
        "",
        "── ENTRY (CAPTURE MODE) ─────────────────────────────",
        f"  Signal         : {opt_type}",
        f"  Symbol         : {symbol}",
        "  Strike Type    : ITM (1 strike in the money)",
        f"  Entry Premium  : Rs {entry:.2f}",
        f"  Initial SL     : Rs {entry - cfg['initial_sl_pts']:.2f} "
        f"(-{cfg['initial_sl_pts']}pts = Rs {cfg['initial_sl_pts'] * q:.0f})",
        f"  Ratchet        : arms at +{cfg['arm_pts']}pts, floor = {CAPTURE_RATIO:.0%} of peak, rises only",
        f"  Entry Time     : {t_in.strftime('%H:%M:%S')}",
        "",
        "── MFE / MAE ANALYTICS ─────────────────────────────",
        f"  MFE (Best point): +{mfe:.1f}pts = Rs {mfe * q:.0f}",
        f"  MAE (Worst point): {mae:.1f}pts = Rs {mae * q:.0f}",
        f"  Profit Captured : {pts:.1f}pts / {mfe:.1f}pts = {cap:.0f}%",
        "",
        "── RATCHET AUDIT ───────────────────────────────────",
        f"  Armed          : {'YES — floor locked at breakeven or better' if armed else 'NO — never reached arm threshold'}",
    ]
    for i, (ts, peak, fl) in enumerate(floor_log, 1):
        lines.append(f"  Ratchet #{i}     : peak {peak}pts -> floor {fl}pts | {ts}")
    if floor is not None:
        lines.append(f"  Floor at Exit  : {floor:+.1f}pts (Rs {floor * q:+.0f} guaranteed)")
    lines += [
        "",
        "── EXIT CONTEXT ─────────────────────────────────────",
        f"  Exit Time      : {t_out.strftime('%H:%M:%S')}",
        f"  Exit Premium   : Rs {exitp:.2f}",
        f"  Exit Reason    : {reason}",
    ]
    if ex_e9 is not None and ex_e21 is not None:
        alive = (ex_e9 >= ex_e21) if opt_type == "CE" else (ex_e9 <= ex_e21)
        lines += [
            f"  EMA9 at Exit   : {ex_e9:.2f}",
            f"  EMA21 at Exit  : {ex_e21:.2f}",
            f"  Trend at Exit  : {'alive' if alive else 'FLIPPED'}",
        ]
    lines += [
        f"  Hold Duration  : {int((t_out - t_in).total_seconds())}s",
        "",
        "── RESULT ───────────────────────────────────────────",
        f"  P&L Points     : {pts:+.1f}pts",
        f"  P&L Rupees     : Rs {rs:+.0f}",
        f"  RUNNING TOTAL  : Rs {total:+.0f}",
        "",
        "── TRADE DIAGNOSIS ──────────────────────────────────",
        f"  Tag            : {tag}",
        f"  Analysis       : {analysis}",
        sep,
    ]
    _TL.trade_card("\n".join(lines))
    _TL.csv_row({
        "date": t_in.strftime("%Y-%m-%d"), "entry_time": t_in.strftime("%H:%M:%S"),
        "exit_time": t_out.strftime("%H:%M:%S"), "strategy": "mint",
        "instrument": name, "direction": opt_type, "symbol": symbol, "strike_type": "ITM",
        "entry_price": round(entry, 2), "exit_price": round(exitp, 2), "quantity": q,
        "pnl_pts": round(pts, 2), "pnl_rs": round(rs, 0),
        "mfe_pts": round(mfe, 2), "mae_pts": round(mae, 2),
        "capture_pct": round(cap, 1), "exit_reason": reason,
        "hold_seconds": int((t_out - t_in).total_seconds()),
        "adx_entry": snap["adx"], "atr_entry": snap["atr"],
        "ema9_entry": snap["ema9"], "ema21_entry": snap["ema21"], "vwap_entry": snap["vwap"],
        "orb_high": snap["orb_high"], "orb_low": snap["orb_low"],
        "oi_bias": snap["oi_bias"], "momentum": get_momentum_state(snap["ema9"], snap["ema21"], snap["adx"]),
        "be_triggered": "YES" if floor_log else "NO", "trail_count": len(floor_log),
        "high_water_pts": round(mfe, 2), "running_total": round(total, 0),
    })


# ============================================================
# ENGINE
# ============================================================
def run_instrument(name, cfg):
    tlog(name, f"MINT started | qty {cfg['quantity']} | budget {MAX_TRADES_PER_DAY}/day "
               f"(DB-seeded) | ratchet {CAPTURE_RATIO:.0%} of peak, arm {cfg['arm_pts']}pts")
    total = 0.0
    trade_count = Governor.today_db_count(name)
    if trade_count:
        tlog(name, f"{trade_count} {name} MINT trades already in DB today — budget continues, not resets.")
    signal_hist = deque(maxlen=CANDLE_CONFIRM_COUNT)
    expiry = get_expiry(cfg)
    chain, last_chain, last_exit = None, None, None
    # Chase gate: True/False/None per direction — result of the most recent
    # CLOSED trade in that direction. None = no trade yet this session.
    last_dir_result = {"CE": None, "PE": None}
    log(name, f"Expiry {expiry}")

    while True:
        ns = tstr()
        if ns >= EXIT_TIME or squareoff_requested():
            _why = "Square-Off requested" if ns < EXIT_TIME else "Session end"
            tlog(name, f"{_why} | trades {trade_count} | P&L Rs {total:+.0f}")
            break
        if ns < ENTRY_TIME:
            time.sleep(5); continue
        if ns >= LATE_ENTRY_CUTOFF:
            time.sleep(30); continue
        if trade_count >= MAX_TRADES_PER_DAY:
            tlog(name, f"Daily budget ({MAX_TRADES_PER_DAY}) used. Done — quality over quantity.")
            break
        halted, why = governor.status()
        if halted:
            tlog(name, f"GOVERNOR HALT — {why}. Standing down for the day.")
            break
        if last_exit and (now_ist() - last_exit).seconds < COOLDOWN_AFTER_EXIT:
            time.sleep(15); continue

        # ── ORGAN 1: regime ──
        ok, avg = regime.check(name)
        if not ok:
            time.sleep(120); continue

        if last_chain is None or (now_ist() - last_chain).seconds >= 900:
            chain = fetch_option_chain(cfg, expiry); last_chain = now_ist()

        df = fetch_candles(cfg, interval="1m")
        if df is None or len(df) < 20:
            time.sleep(15); continue
        try:
            ema9, ema21 = calc_ema(df, 9), calc_ema(df, 21)
            vwap, atr, adx = calc_vwap(df), calc_atr(df), calc_adx(df)
            orb_h, orb_l = get_orb(df, cfg["orb_candles"])
            spot = float(df.iloc[-1]["close"])
            atm = get_atm_strike(spot, cfg["strike_interval"])
        except Exception as e:
            log(name, f"indicator err {e}"); time.sleep(10); continue
        if adx != adx or adx < cfg["adx_threshold"]:   # nan-safe floor
            log(name, f"ADX {'n/a' if adx != adx else f'{adx:.1f}'} < {cfg['adx_threshold']} — "
                      f"no trend. Waiting. | spot {spot:.0f}")
            signal_hist.clear(); time.sleep(20); continue

        buf = cfg["orb_buffer"]; sig = None
        if spot > orb_h + buf and ema9 > ema21 and spot > vwap:  sig = "CE"
        elif spot < orb_l - buf and ema9 < ema21 and spot < vwap: sig = "PE"
        if not sig:
            log(name, f"no setup | spot {spot:.0f} vs ORB {orb_l:.0f}-{orb_h:.0f}")
            signal_hist.clear(); time.sleep(SIGNAL_INTERVAL); continue
        signal_hist.append(sig)
        if len(signal_hist) < CANDLE_CONFIRM_COUNT or not all(s == sig for s in signal_hist):
            time.sleep(SIGNAL_INTERVAL); continue

        # ── ORGAN 2: playbook ──
        allowed, expectancy, pid = pattern_gate(name, sig, adx)
        if not allowed:
            signal_hist.clear(); time.sleep(SIGNAL_INTERVAL); continue

        # ── CHASE GATE (optional, OFF by default — see config note) ──
        if CHASE_GATE_ENABLED and last_dir_result[sig] is True:
            log(name, f"CHASE GATE BLOCKED {sig} — previous {sig} trade won; "
                      f"this would be chasing a paid-off move.")
            signal_hist.clear(); time.sleep(SIGNAL_INTERVAL); continue

        # ── ORGAN 4: edge must clear costs ──
        if 0 < expectancy < MIN_EDGE_MULTIPLE * COST_PER_RT:
            log(name, f"EDGE-vs-COST: {pid} expectancy Rs{expectancy:.0f} < "
                      f"{MIN_EDGE_MULTIPLE}x cost Rs{MIN_EDGE_MULTIPLE*COST_PER_RT:.0f} — pass.")
            signal_hist.clear(); time.sleep(SIGNAL_INTERVAL); continue

        _, mp_bias, oi_bias = analyze_option_chain(chain, atm, cfg["strike_interval"])
        strike = get_itm_strike(sig, atm, cfg["strike_interval"])
        symbol = build_option_symbol(name, cfg, strike, sig)
        quote = get_quote(symbol, cfg["option_exchange"])
        if quote is None:
            time.sleep(10); continue
        fill = market_order(symbol, cfg["option_exchange"], "BUY", cfg["quantity"], quote)
        if not fill:
            log(name, "entry failed"); time.sleep(10); continue

        entry = fill; t_in = now_ist(); trade_count += 1; signal_hist.clear()
        snap = {"adx": adx, "atr": atr, "ema9": ema9, "ema21": ema21, "vwap": vwap,
                "orb_high": orb_h, "orb_low": orb_l, "oi_bias": oi_bias,
                "spot": spot, "regime_mfe": avg, "pattern": pid, "pattern_exp": expectancy}
        tlog(name, f"ENTRY #{trade_count} | {sig} ITM Rs {entry} | {symbol} | "
                   f"regimeMFE {avg:.1f} | pattern {pid}")

        # ── ORGAN 3: CAPTURE RATCHET monitor ────────────────
        floor = -cfg["initial_sl_pts"]      # pts vs entry; only ever rises
        armed = False; floor_log = []
        mfe = mae = 0.0; curr = entry
        last_q = now_ist(); mom_dead = False; last_mom = now_ist()
        ex_e9, ex_e21 = ema9, ema21          # exit-context EMAs (refreshed by mom check)

        # ── ORGAN 5: bleed guard state ──
        vel_hist = deque()                  # (timestamp, pts) trailing BLEED_WINDOW_SECS
        peak_val, peak_time = 0.0, t_in      # tracks when the current MFE peak was set
        bleed_confirm = 0

        while True:
            nps = tstr()
            _sqoff = squareoff_requested()
            if nps >= EXIT_TIME or _sqoff:
                _reason = "Square-Off" if (_sqoff and nps < EXIT_TIME) else "Hard Exit"
                market_order(symbol, cfg["option_exchange"], "SELL", cfg["quantity"], curr)
                px = get_quote(symbol, cfg["option_exchange"]) or curr
                pnl = (px - entry) * cfg["quantity"]; total += pnl
                governor.record(pnl); last_exit = now_ist()
                last_dir_result[sig] = pnl > 0   # feed the chase gate
                tlog(name, f"{_reason.upper()} | Rs {pnl:+.0f} | total Rs {total:+.0f}")
                record_trade(name, cfg, trade_count, sig, symbol, entry, px, _reason,
                             t_in, now_ist(), snap, mfe, mae, floor_log, total,
                             floor=floor, armed=armed, ex_e9=ex_e9, ex_e21=ex_e21)
                break

            q = get_quote(symbol, cfg["option_exchange"])
            if q is None:
                stale = (now_ist() - last_q).total_seconds()
                if stale >= STALE_QUOTE_EXIT_SECS:
                    market_order(symbol, cfg["option_exchange"], "SELL", cfg["quantity"], curr)
                    pnl = (curr - entry) * cfg["quantity"]; total += pnl
                    governor.record(pnl); last_exit = now_ist()
                    last_dir_result[sig] = pnl > 0   # feed the chase gate
                    tlog(name, f"STALE-QUOTE EXIT | Rs {pnl:+.0f}")
                    record_trade(name, cfg, trade_count, sig, symbol, entry, curr,
                                 "Stale Quote Exit", t_in, now_ist(), snap, mfe, mae, floor_log, total,
                                 floor=floor, armed=armed, ex_e9=ex_e9, ex_e21=ex_e21)
                    break
                time.sleep(3); continue
            curr = q; last_q = now_ist()
            pts = curr - entry
            if pts > mfe:
                mfe = pts; peak_val = pts; peak_time = now_ist()
            mae = min(mae, pts)
            held = (now_ist() - t_in).seconds

            # ── ORGAN 5: bleed guard — velocity check ──
            bleed_now = now_ist()
            vel_hist.append((bleed_now, pts))
            while vel_hist and (bleed_now - vel_hist[0][0]).total_seconds() > BLEED_WINDOW_SECS:
                vel_hist.popleft()

            bleed_signal, bleed_kind = check_bleed_guard(
                vel_hist, pts, held, mfe, peak_val, peak_time,
                snap.get("atr"), cfg["arm_pts"], bleed_now, t_in)

            bleed_confirm = bleed_confirm + 1 if bleed_signal else 0
            if bleed_confirm == 1:
                tlog(name, f"BLEED GUARD watch ({bleed_kind}) | {pts:+.1f}pts, "
                           f"confirming next poll before cutting.")

            # arm + ratchet the floor
            if mfe >= cfg["arm_pts"]:
                new_floor = max(0.0, CAPTURE_RATIO * mfe)   # never below breakeven once armed
                if new_floor > floor:
                    floor = new_floor; armed = True
                    floor_log.append((now_ist().strftime("%H:%M:%S"), round(mfe, 1), round(floor, 1)))
                    tlog(name, f"RATCHET #{len(floor_log)}: peak {mfe:.1f} -> floor {floor:.1f}pts "
                               f"(win locked: Rs {floor*cfg['quantity']:.0f})")

            # trend re-check (for context/exit)
            if (now_ist() - last_mom).seconds >= MOM_CHECK_INTERVAL:
                df2 = fetch_candles(cfg, interval="1m"); last_mom = now_ist()
                if df2 is not None and len(df2) >= 21:
                    e9, e21 = calc_ema(df2, 9), calc_ema(df2, 21)
                    ex_e9, ex_e21 = e9, e21
                    mom_dead = (e9 < e21) if sig == "CE" else (e9 > e21)

            log(name, f"Rs{curr} | {pts:+.1f}pts | floor {floor:+.1f} | peak {mfe:.1f} "
                      f"| {'ARMED' if armed else 'unarmed'} | {held}s")

            exit_reason = None
            if bleed_confirm >= BLEED_CONFIRM_POLLS:
                exit_reason = "Bleed Guard"
            elif pts <= floor:
                exit_reason = "Capture Floor" if armed else "Initial SL"
            elif not armed and held >= ZERO_MFE_SECS and mfe <= ZERO_MFE_PTS:
                exit_reason = "Zero-MFE Bailout"
            elif armed and mom_dead and pts <= floor + 2:
                exit_reason = "Momentum Flip (near floor)"

            if exit_reason:
                market_order(symbol, cfg["option_exchange"], "SELL", cfg["quantity"], curr)
                pnl = pts * cfg["quantity"]; total += pnl
                governor.record(pnl); last_exit = now_ist()
                last_dir_result[sig] = pnl > 0   # feed the chase gate
                tlog(name, f"{exit_reason.upper()} | {pts:+.1f}pts = Rs {pnl:+.0f} "
                           f"| total Rs {total:+.0f}")
                record_trade(name, cfg, trade_count, sig, symbol, entry, curr, exit_reason,
                             t_in, now_ist(), snap, mfe, mae, floor_log, total,
                             floor=floor, armed=armed, ex_e9=ex_e9, ex_e21=ex_e21)
                break
            # fast-poll while armed: locked profit deserves a tight watch
            time.sleep(MONITOR_INTERVAL_ARMED if armed else MONITOR_INTERVAL)

        time.sleep(SIGNAL_INTERVAL)

    tlog(name, f"=== {name} done | trades {trade_count} | Rs {total:+.0f} ===")


def main():
    _TL.raw_trade("=" * 60)
    _TL.raw_trade("MINT — the post-PIT engine")
    _TL.raw_trade("regime gate + pattern gate + capture ratchet + budget governor")
    _TL.raw_trade(f"budget {MAX_TRADES_PER_DAY}/day · halt after {MAX_CONSEC_LOSSES} losses · "
                  f"floor {CAPTURE_RATIO:.0%} of peak")
    _TL.raw_trade("=" * 60)
    threads = [threading.Thread(target=run_instrument, args=(n, c), daemon=True)
               for n, c in INSTRUMENTS.items()]
    for t in threads: t.start()
    for t in threads: t.join()
    _TL.raw_trade("MINT complete for the day.")


if __name__ == "__main__":
    main()
