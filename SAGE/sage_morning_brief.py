"""
sage_morning_brief.py — Morning Hypothesis Generator
=====================================================
Runs at 9:00–9:10 IST before market open.
Generates one HTML page + JSON with today's hypothesis.

What it does:
1. Characterises "today" from available pre-market signals
   (VIX, previous day's NIFTY return, SGX gap — fetched from
   OpenAlgo if available; otherwise from trading.db recent history)
2. Finds past trading days that resemble today
3. Reads SAGE's pattern registry for active patterns
4. Generates: regime, direction bias, P&L range, rules, reasoning
5. Writes logs/YYYY-MM-DD/morning_brief.html + .json
6. Appends one row to logs/sage_master.csv

Usage:
    uv run python3 sage_morning_brief.py
    uv run python3 sage_morning_brief.py --date 2026-06-17
"""

import csv
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
import sys
import urllib.request
import urllib.error

import pytz

sys.path.insert(0, str(Path(__file__).parent))
from sage_db import get_sage_db
from sage_enricher import backfill_stories, story_count, _safe_float

IST       = pytz.timezone("Asia/Kolkata")
LOGS_DIR  = Path(__file__).parent / "logs"
MASTER_CSV = LOGS_DIR / "sage_master.csv"
OPENALGO_URL = "http://127.0.0.1:5000"

MASTER_COLS = [
    "date", "regime_predicted", "direction_bias",
    "pnl_low", "pnl_high", "confidence",
    "similar_days", "patterns_matched",
    "entry_window", "adx_floor", "max_trades",
    "sage_recommendation",
    "actual_pnl", "actual_trades", "hypothesis_correct",
    "direction_correct", "pnl_in_range",
]


# ── OpenAlgo helpers ──────────────────────────────────────────────

def _openalgo_get(path: str, timeout: int = 3) -> dict:
    try:
        url = f"{OPENALGO_URL}{path}"
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return {}


def _get_prev_day_pnl() -> float:
    """Sum yesterday's P&L from sage.db stories."""
    db = get_sage_db()
    yesterday = (datetime.now(IST) - timedelta(days=1)).strftime("%Y-%m-%d")
    rows = db.query(
        "SELECT SUM(pnl_rs) as total FROM trade_stories WHERE date=?",
        (yesterday,)
    )
    return _safe_float(rows[0]["total"]) if rows else 0.0


def _get_recent_win_rate(n_days: int = 5) -> float:
    db = get_sage_db()
    cutoff = (datetime.now(IST) - timedelta(days=n_days)).strftime("%Y-%m-%d")
    rows = db.query(
        "SELECT outcome FROM trade_stories WHERE date >= ?", (cutoff,)
    )
    if not rows:
        return 0.5
    wins = sum(1 for r in rows if r["outcome"] == "WIN")
    return wins / len(rows)


# ── Day similarity ────────────────────────────────────────────────

def _get_past_day_stats() -> list:
    """Return per-day P&L and win rate from story library."""
    db = get_sage_db()
    rows = db.query(
        """SELECT date,
               SUM(pnl_rs) as day_pnl,
               COUNT(*) as trades,
               SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) as wins,
               AVG(adx_entry) as avg_adx,
               AVG(CASE WHEN direction='CE' THEN 1 WHEN direction='PE' THEN -1 ELSE 0 END) as dir_bias
           FROM trade_stories
           GROUP BY date
           ORDER BY date"""
    )
    return rows


def _classify_regime(avg_adx: float, prev_wr: float, prev_pnl: float) -> str:
    if avg_adx >= 28 and prev_pnl > 500:
        return "TRENDING"
    if avg_adx <= 20 or (prev_wr < 0.40 and prev_pnl < 0):
        return "RANGE"
    if prev_pnl < -1500:
        return "VOLATILE"
    return "MIXED"


def _find_similar_days(today_regime: str, recent_wr: float,
                        past_days: list) -> list:
    """Simple regime + WR based day matching."""
    matches = []
    for d in past_days:
        trades = d.get("trades", 0)
        if trades < 1:
            continue
        day_wr = _safe_float(d.get("wins")) / max(trades, 1)
        day_adx = _safe_float(d.get("avg_adx"), 25.0)
        day_regime = _classify_regime(day_adx, day_wr, _safe_float(d.get("day_pnl")))
        if day_regime == today_regime:
            matches.append(d)
    return matches


def _direction_bias(similar_days: list) -> str:
    if not similar_days:
        return "NEUTRAL"
    dir_scores = [_safe_float(d.get("dir_bias")) for d in similar_days]
    avg = sum(dir_scores) / len(dir_scores)
    if avg > 0.25:
        return "CE"
    if avg < -0.25:
        return "PE"
    return "NEUTRAL"


def _pnl_range(similar_days: list) -> tuple[float, float]:
    if not similar_days:
        return (-2000.0, 2000.0)
    pnls = sorted(_safe_float(d.get("day_pnl")) for d in similar_days)
    low  = pnls[len(pnls) // 4]      # 25th percentile
    high = pnls[3 * len(pnls) // 4]  # 75th percentile
    return (round(low, 0), round(high, 0))


# ── Pattern registry ──────────────────────────────────────────────

def _load_active_patterns() -> list:
    db = get_sage_db()
    return db.query("SELECT * FROM pattern_registry WHERE status='ACTIVE'")


# ── HTML renderer ─────────────────────────────────────────────────

def _render_html(brief: dict, today: str) -> str:
    regime      = brief["regime"]
    dir_bias    = brief["direction_bias"]
    pnl_low     = brief["pnl_low"]
    pnl_high    = brief["pnl_high"]
    confidence  = brief["confidence"]
    similar_n   = brief["similar_days"]
    reasoning   = brief["reasoning"]
    rules       = brief["rules"]
    patterns    = brief["patterns_matched"]
    recent_wr   = brief.get("recent_5d_wr", 0.5)
    story_ct    = brief.get("story_count", {})

    regime_color = {
        "TRENDING": "#22c55e",
        "RANGE":    "#f59e0b",
        "VOLATILE": "#ef4444",
        "MIXED":    "#6366f1",
        "THIN":     "#94a3b8",
    }.get(regime, "#6366f1")

    dir_color = "#22c55e" if dir_bias == "CE" else \
                "#ef4444" if dir_bias == "PE" else "#94a3b8"

    pnl_color = "#22c55e" if pnl_high > 0 else "#ef4444"

    reason_rows = "\n".join(
        f'<li style="margin:6px 0;color:#94a3b8">{r}</li>'
        for r in reasoning
    )
    def _pat_meta(p):
        """Pull tag/recommendation from the mined feature_profile JSON."""
        try:
            fp = json.loads(p.get("feature_profile") or "{}")
        except Exception:
            fp = {}
        return fp.get("tag", "NEUTRAL"), fp.get("recommendation", "CAUTION")

    # Order the playbook: EDGE first, then TRAP, then NEUTRAL; strongest by |P&L|.
    _rank = {"EDGE": 0, "TRAP": 1, "NEUTRAL": 2}
    patterns_sorted = sorted(
        patterns,
        key=lambda p: (_rank.get(_pat_meta(p)[0], 3), -abs(_safe_float(p.get("avg_pnl_rs")))),
    )

    _tag_color = {"EDGE": "#22c55e", "TRAP": "#ef4444", "NEUTRAL": "#f59e0b"}
    pattern_rows = ""
    for p in patterns_sorted:
        tag, rec = _pat_meta(p)
        color   = _tag_color.get(tag, "#94a3b8")
        wr_pct  = round(p.get("win_rate", 0) * 100, 0)
        wr_col  = "#22c55e" if wr_pct >= 55 else ("#ef4444" if wr_pct <= 35 else "#f59e0b")
        avg_pnl = _safe_float(p.get("avg_pnl_rs"))
        pnl_col = "#22c55e" if avg_pnl > 0 else "#ef4444"
        pattern_rows += f"""
        <tr>
          <td style="padding:8px;color:{color};font-weight:700">{tag}</td>
          <td style="padding:8px;color:#e2e8f0">{p.get('name','')}</td>
          <td style="padding:8px;color:{wr_col}">{wr_pct:.0f}%</td>
          <td style="padding:8px;color:{pnl_col}">₹{avg_pnl:+,.0f}</td>
          <td style="padding:8px;color:#94a3b8">{p.get('trade_count',0)}</td>
          <td style="padding:8px;color:{color};font-weight:600">{rec}</td>
        </tr>"""

    conf_pct = round(confidence * 100, 0)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>SAGE Morning Brief — {today}</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
        background:#0f172a;color:#e2e8f0;padding:24px}}
  .header{{display:flex;justify-content:space-between;align-items:center;
           margin-bottom:28px;border-bottom:1px solid #1e293b;padding-bottom:18px}}
  .sage-badge{{font-size:11px;font-weight:700;letter-spacing:.2em;
               color:#6366f1;border:1px solid #6366f1;padding:3px 10px;
               border-radius:4px}}
  h1{{font-size:22px;color:#e2e8f0;font-weight:700}}
  .date{{font-size:13px;color:#64748b}}
  .cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;
          margin-bottom:24px}}
  .card{{background:#1e293b;border-radius:10px;padding:20px;
         border:1px solid #334155}}
  .card-label{{font-size:11px;color:#64748b;text-transform:uppercase;
               letter-spacing:.1em;margin-bottom:8px}}
  .card-val{{font-size:26px;font-weight:700}}
  .card-sub{{font-size:12px;color:#64748b;margin-top:4px}}
  .section{{background:#1e293b;border-radius:10px;padding:20px;
            border:1px solid #334155;margin-bottom:16px}}
  .section-title{{font-size:13px;font-weight:700;color:#64748b;
                  text-transform:uppercase;letter-spacing:.1em;
                  margin-bottom:16px}}
  .rules-grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}}
  .rule-box{{background:#0f172a;border-radius:8px;padding:12px 16px;
             border:1px solid #1e293b}}
  .rule-label{{font-size:11px;color:#64748b;margin-bottom:4px}}
  .rule-val{{font-size:16px;font-weight:700;color:#e2e8f0}}
  table{{width:100%;border-collapse:collapse}}
  th{{padding:8px;text-align:left;font-size:11px;color:#64748b;
      text-transform:uppercase;border-bottom:1px solid #334155}}
  tr:hover td{{background:#0f172a}}
  .conf-bar{{height:8px;border-radius:4px;background:#1e293b;
             margin-top:8px;overflow:hidden}}
  .conf-fill{{height:100%;border-radius:4px;
              background:linear-gradient(90deg,#6366f1,#22c55e);
              width:{conf_pct}%}}
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>Morning Brief &nbsp;<span class="sage-badge">SAGE</span></h1>
    <div class="date">{today} · Generated {datetime.now(IST).strftime('%H:%M IST')}</div>
  </div>
  <div style="text-align:right">
    <div style="font-size:12px;color:#64748b">Story Library</div>
    <div style="font-size:20px;font-weight:700;color:#6366f1">
      {story_ct.get('total',0)}
    </div>
    <div style="font-size:11px;color:#64748b">
      {story_ct.get('wins',0)}W · {story_ct.get('losses',0)}L
    </div>
  </div>
</div>

<div class="cards">
  <div class="card">
    <div class="card-label">Today's Regime</div>
    <div class="card-val" style="color:{regime_color}">{regime}</div>
    <div class="card-sub">Based on {similar_n} similar past days</div>
  </div>
  <div class="card">
    <div class="card-label">Direction Bias</div>
    <div class="card-val" style="color:{dir_color}">{dir_bias}</div>
    <div class="card-sub">Historical pattern on {regime} days</div>
  </div>
  <div class="card">
    <div class="card-label">Expected P&amp;L Range</div>
    <div class="card-val" style="color:{pnl_color}">
      ₹{int(pnl_low):,} → ₹{int(pnl_high):,}
    </div>
    <div class="card-sub">25th–75th pct on similar days</div>
  </div>
  <div class="card">
    <div class="card-label">Hypothesis Confidence</div>
    <div class="card-val" style="color:#6366f1">{conf_pct:.0f}%</div>
    <div class="conf-bar"><div class="conf-fill"></div></div>
    <div class="card-sub">5-day WR {round(recent_wr*100,1)}%</div>
  </div>
</div>

<div class="section">
  <div class="section-title">Today's Rules (auto-generated)</div>
  <div class="rules-grid">
    <div class="rule-box">
      <div class="rule-label">Entry Window</div>
      <div class="rule-val">{rules.get('entry_window','09:20 – 14:30')}</div>
    </div>
    <div class="rule-box">
      <div class="rule-label">ADX Floor</div>
      <div class="rule-val">{rules.get('adx_floor', 22)}</div>
    </div>
    <div class="rule-box">
      <div class="rule-label">Max Trades</div>
      <div class="rule-val">{rules.get('max_trades', 4)}</div>
    </div>
    <div class="rule-box">
      <div class="rule-label">Direction</div>
      <div class="rule-val" style="color:{dir_color}">{dir_bias}</div>
    </div>
  </div>
</div>

<div class="section">
  <div class="section-title">Reasoning</div>
  <ul style="list-style:disc;padding-left:20px">
    {reason_rows}
  </ul>
</div>

{"" if not pattern_rows else f'''
<div class="section">
  <div class="section-title">Learned Patterns — your playbook</div>
  <table>
    <thead><tr>
      <th>Verdict</th><th>Pattern</th><th>Win Rate</th><th>Avg P&amp;L</th><th>Trades</th><th>Action</th>
    </tr></thead>
    <tbody>{pattern_rows}</tbody>
  </table>
</div>
'''}

<div style="margin-top:24px;font-size:11px;color:#334155;text-align:center">
  SAGE · Situational Awareness &amp; Growth Engine · Not financial advice
</div>
</body>
</html>"""


# ── Master CSV ────────────────────────────────────────────────────

def _append_master_csv(row: dict) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    exists = MASTER_CSV.exists()
    with open(MASTER_CSV, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MASTER_COLS, extrasaction="ignore")
        if not exists:
            w.writeheader()
        w.writerow(row)


# ── Main ──────────────────────────────────────────────────────────

def generate_morning_brief(date: str = None) -> dict:
    """
    Generate today's morning brief. Returns the brief dict.
    Writes HTML + JSON to logs/YYYY-MM-DD/ and appends to sage_master.csv.
    """
    if date is None:
        date = datetime.now(IST).strftime("%Y-%m-%d")

    # ensure stories are up-to-date
    backfill_stories(verbose=False)
    sc = story_count()

    db = get_sage_db()
    past_days   = _get_past_day_stats()
    recent_wr   = _get_recent_win_rate(5)
    prev_pnl    = _get_prev_day_pnl()

    # exclude today from past_days
    past_days_excl = [d for d in past_days if d["date"] != date]

    # classify today's likely regime from recent context
    recent_avg_adx = 0.0
    recent_rows = db.query(
        "SELECT AVG(adx_entry) as a FROM trade_stories WHERE date >= ?",
        ((datetime.now(IST) - timedelta(days=3)).strftime("%Y-%m-%d"),)
    )
    if recent_rows:
        recent_avg_adx = _safe_float(recent_rows[0].get("a"), 25.0)

    regime = _classify_regime(recent_avg_adx, recent_wr, prev_pnl)

    similar = _find_similar_days(regime, recent_wr, past_days_excl)
    dir_bias = _direction_bias(similar)
    pnl_low, pnl_high = _pnl_range(similar) if similar else (-1500.0, 1500.0)

    # confidence: penalise if sample small or recent WR very low
    raw_conf = min(len(similar) / 20.0, 1.0)          # saturates at 20 days
    adj_conf = raw_conf * (0.5 + 0.5 * recent_wr)     # shrink if losing streak
    confidence = round(adj_conf, 3)

    # rules
    if regime == "TRENDING":
        adx_floor     = 24
        entry_window  = "09:20 – 11:30"
        max_trades    = 3
    elif regime == "RANGE":
        adx_floor     = 26
        entry_window  = "09:30 – 11:00"
        max_trades    = 2
    elif regime == "VOLATILE":
        adx_floor     = 28
        entry_window  = "10:00 – 11:00"
        max_trades    = 1
    else:  # MIXED
        adx_floor     = 22
        entry_window  = "09:20 – 14:30"
        max_trades    = 4

    rules = {
        "entry_window": entry_window,
        "adx_floor":    adx_floor,
        "max_trades":   max_trades,
        "direction":    dir_bias,
    }

    # reasoning
    reasoning = []
    reasoning.append(f"5-day win rate is {round(recent_wr*100,1)}% "
                     f"→ {'edge holding' if recent_wr >= 0.50 else 'under pressure'}")
    reasoning.append(f"Previous day P&L: ₹{int(prev_pnl):,} "
                     f"({'profit' if prev_pnl >= 0 else 'loss'})")
    reasoning.append(f"Regime classified as {regime} "
                     f"(avg ADX {round(recent_avg_adx,1)}, matched {len(similar)} similar past days)")
    if similar:
        avg_similar_pnl = sum(_safe_float(d.get("day_pnl")) for d in similar) / len(similar)
        reasoning.append(f"On past {regime} days: avg P&L ₹{int(avg_similar_pnl):,}, "
                         f"direction bias {dir_bias}")
    if sc["total"] < 50:
        reasoning.append(f"⚠ Story library only {sc['total']} trades — predictions improve with more data")

    patterns = _load_active_patterns()

    # sage_recommendation
    if regime == "VOLATILE":
        sage_rec = "SIT OUT or 1 trade max — volatile conditions"
    elif regime == "TRENDING" and dir_bias != "NEUTRAL":
        sage_rec = f"FAVOUR {dir_bias} — trending day, {entry_window}"
    elif regime == "RANGE":
        sage_rec = "CAUTIOUS — range day, tighter filters"
    else:
        sage_rec = f"NORMAL — mixed day, follow standard rules"

    brief = {
        "date":              date,
        "regime":            regime,
        "direction_bias":    dir_bias,
        "pnl_low":           pnl_low,
        "pnl_high":          pnl_high,
        "confidence":        confidence,
        "similar_days":      len(similar),
        "patterns_matched":  patterns,
        "rules":             rules,
        "reasoning":         reasoning,
        "sage_recommendation": sage_rec,
        "recent_5d_wr":      recent_wr,
        "story_count":       sc,
    }

    # write files
    day_dir = LOGS_DIR / date
    day_dir.mkdir(parents=True, exist_ok=True)

    html_path = day_dir / "morning_brief.html"
    html_path.write_text(_render_html(brief, date), encoding="utf-8")

    json_brief = {k: v for k, v in brief.items() if k != "story_count"}
    (day_dir / "morning_brief.json").write_text(
        json.dumps(json_brief, indent=2, default=str), encoding="utf-8"
    )

    # save to sage.db
    hyp_data = {
        "regime":            regime,
        "direction_bias":    dir_bias,
        "pnl_low":           pnl_low,
        "pnl_high":          pnl_high,
        "confidence":        confidence,
        "patterns_matched":  json.dumps([p.get("pattern_id") for p in patterns]),
        "similar_days":      len(similar),
        "reasoning":         json.dumps(reasoning),
        "entry_window_start": entry_window.split("–")[0].strip(),
        "entry_window_end":   entry_window.split("–")[1].strip(),
        "adx_floor":         adx_floor,
        "max_trades":        max_trades,
        "sage_recommendation": sage_rec,
    }
    db.upsert_hypothesis(date, hyp_data)

    # master CSV row
    _append_master_csv({
        "date":               date,
        "regime_predicted":   regime,
        "direction_bias":     dir_bias,
        "pnl_low":            pnl_low,
        "pnl_high":           pnl_high,
        "confidence":         confidence,
        "similar_days":       len(similar),
        "patterns_matched":   len(patterns),
        "entry_window":       entry_window,
        "adx_floor":          adx_floor,
        "max_trades":         max_trades,
        "sage_recommendation": sage_rec,
        "actual_pnl":         "",
        "actual_trades":      "",
        "hypothesis_correct": "",
        "direction_correct":  "",
        "pnl_in_range":       "",
    })

    print(f"[SAGE] Morning brief written → {html_path}")
    print(f"[SAGE] Regime: {regime} | Bias: {dir_bias} | "
          f"P&L range: ₹{int(pnl_low):,}–₹{int(pnl_high):,} | "
          f"Confidence: {round(confidence*100)}%")
    return brief


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="SAGE Morning Brief")
    p.add_argument("--date", type=str, default=None)
    a = p.parse_args()
    generate_morning_brief(date=a.date)
