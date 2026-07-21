"""
Equity Dashboard — live NIFTY 100 display server
==================================================
Pure display, read-only. Never places an order, never imports strategy code.
A background thread polls OpenAlgo's /quotes endpoint for the NIFTY 100
universe every REFRESH_SECS, computes % change from today's open, and stores
a snapshot in memory. The frontend (index.html) polls GET /api/data every
~12s and repaints the DOM in place — no page reload, so it feels live.

Run:
    export OPENALGO_API_KEY=<64-hex key>
    cd ~/openalgo/EQUITY_DASHBOARD && uv run python3 dashboard_server.py
Then open http://127.0.0.1:5051 in a browser.

Standalone process, own port (5051) — does not touch app.py or the main
OpenAlgo Flask app in any way.
"""

import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pytz
import requests
from flask import Flask, jsonify, send_from_directory

sys.path.insert(0, str(Path(__file__).parent))
from nifty100_symbols import NIFTY_100, EXCHANGE
from our_20 import OUR_20
from market_weights import weight_for

API_KEY = os.environ.get("OPENALGO_API_KEY", "")
if not API_KEY:
    raise EnvironmentError(
        "OPENALGO_API_KEY not set. Export it before running:\n"
        "  export OPENALGO_API_KEY=your_key_here"
    )

BASE_URL = "http://127.0.0.1:5000/api/v1"
PORT = 5051
REFRESH_SECS = 20          # how often the background fetcher refreshes ALL symbols
PER_CALL_DELAY = 0.02      # small gap between quote calls (rate-limit is 50/sec)
IST = pytz.timezone("Asia/Kolkata")

app = Flask(__name__)
_session = requests.Session()

_lock = threading.Lock()
_snapshot = {
    "updated_at": None,
    "market_open": None,
    "rows": [],          # list of dicts: symbol, ltp, open, chg_pct, volume, ours(bool)
    "error": None,
}

OUR_20_SET = set(OUR_20)


def _api_post(endpoint, payload, timeout=5):
    payload = {"apikey": API_KEY, **payload}
    try:
        r = _session.post(f"{BASE_URL}/{endpoint}", json=payload, timeout=timeout)
        data = r.json()
        if r.status_code >= 400:
            return None
        return data
    except Exception:
        return None


def _get_quote(symbol):
    data = _api_post("quotes", {"symbol": symbol, "exchange": EXCHANGE})
    if not data or data.get("status") != "success":
        return None
    d = data.get("data", {})
    ltp = d.get("ltp") or 0
    open_ = d.get("open") or 0
    prev_close = d.get("prev_close") or 0
    high = d.get("high") or 0
    low = d.get("low") or 0
    volume = d.get("volume") or 0
    if not ltp or not open_:
        return None
    return {
        "ltp": float(ltp),
        "open": float(open_),
        "prev_close": float(prev_close),
        "high": float(high),
        "low": float(low),
        "volume": int(volume),
    }


def _fetch_cycle():
    rows = []
    for sym in NIFTY_100:
        q = _get_quote(sym)
        if q:
            chg_pct = (q["ltp"] - q["open"]) / q["open"] * 100 if q["open"] else 0.0
            rows.append({
                "symbol": sym,
                "ltp": round(q["ltp"], 2),
                "open": round(q["open"], 2),
                "high": round(q["high"], 2),
                "low": round(q["low"], 2),
                "chg_pct": round(chg_pct, 2),
                "volume": q["volume"],
                "weight": weight_for(sym),
                "ours": sym in OUR_20_SET,
            })
        time.sleep(PER_CALL_DELAY)

    rows.sort(key=lambda r: r["chg_pct"], reverse=True)
    with _lock:
        _snapshot["rows"] = rows
        _snapshot["updated_at"] = datetime.now(IST).strftime("%H:%M:%S")
        _snapshot["error"] = None if rows else "No quotes returned — market closed or feed down."


def _background_loop():
    while True:
        try:
            _fetch_cycle()
        except Exception as e:
            with _lock:
                _snapshot["error"] = f"Fetch cycle error: {e}"
        time.sleep(REFRESH_SECS)


@app.route("/")
def index():
    return send_from_directory(Path(__file__).parent, "index.html")


@app.route("/api/data")
def api_data():
    with _lock:
        return jsonify(dict(_snapshot))


if __name__ == "__main__":
    t = threading.Thread(target=_background_loop, daemon=True)
    t.start()
    print(f"Equity Dashboard — {len(NIFTY_100)} symbols ({len(OUR_20)} in our arsenal)")
    print(f"Serving on http://127.0.0.1:{PORT}  (Ctrl+C to stop)")
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)
