"""
report_generator.py — Generates the full HTML analytics report
==============================================================
Pulls real data from all four modules and injects into the
existing report_dashboard.html template.

Usage:
    python3 report_generator.py
    python3 report_generator.py --date 2026-06-05
    python3 report_generator.py --strategy scalper
    python3 report_generator.py --last 7
    python3 report_generator.py --out ~/Desktop/report.html
"""

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path.home() / "openalgo" / "PIT"))

from analytics_core import (
    load_trades, profit_factor, win_rate, expectancy,
    max_drawdown, rolling_windows,
    consecutive_runs, grade, normalize_exit_reason
)
from edge_decay       import run_edge_decay
from cooling_simulator import run_cooling_simulator
from loss_autopsy      import run_loss_autopsy
from whatif            import run_whatif

IST = ZoneInfo("Asia/Kolkata")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_rs(v):
    if v is None: return "—"
    sign = "+" if v >= 0 else "-"
    return f"{sign}₹{abs(int(v)):,}"

def _pct(v):
    if v is None: return "—"
    return f"{v:.1f}%"

def _pf(v):
    if v is None: return "—"
    if v == float("inf"): return "∞"
    return f"{v:.2f}"

def _grade_color(g):
    return {"A":"#00c87a","B":"#4ab8d8","C":"#e8b84a","D":"#e88040","F":"#e8374a"}.get(g,"#5a6a78")

def _verdict_css(v):
    return {"ADOPT":"verdict-adopt","TEST":"verdict-test","SKIP":"verdict-skip"}.get(v,"")

def _flag_css(f):
    return {"OK":"flag-ok","WARN":"flag-warn","ALERT":"flag-alert",
            "INSUFFICIENT_DATA":"flag-dim","BASELINE":"flag-dim"}.get(f,"")


# ── Data builders ─────────────────────────────────────────────────────────────

def _build_trades_js(trades):
    rows = []
    for t in trades:
        rows.append({
            "id":          t.get("id") or 0,
            "date":        t.get("date",""),
            "strategy":    t.get("strategy",""),
            "instrument":  t.get("instrument",""),
            "symbol":      t.get("symbol",""),
            "direction":   t.get("direction",""),
            "entry_time":  (t.get("entry_time") or "")[:8],
            "exit_time":   (t.get("exit_time") or "")[:8],
            "entry_price": float(t.get("entry_price") or 0),
            "exit_price":  float(t.get("exit_price") or 0),
            "quantity":    int(t.get("quantity") or 0),
            "pnl_rs":      float(t.get("pnl_rs") or 0),
            "mfe_pts":     float(t.get("mfe_pts") or 0),
            "mae_pts":     float(t.get("mae_pts") or 0),
            "capture_pct": float(t.get("capture_pct") or 0),
            "adx_entry":   float(t.get("adx_entry") or 0),
            "exit_reason": normalize_exit_reason(t.get("exit_reason")),
            "hold_seconds":int(t.get("hold_seconds") or 0),
            "trend_score": float(t.get("trend_score") or 0),
        })
    return json.dumps(rows)


def _build_hourly_js(trades):
    by_hour = {}
    for t in trades:
        et = (t.get("entry_time") or "")[:2]
        try:
            h = int(et)
        except Exception:
            continue
        if h not in by_hour:
            by_hour[h] = {"pnl": 0, "trades": 0}
        by_hour[h]["pnl"]    += float(t.get("pnl_rs") or 0)
        by_hour[h]["trades"] += 1

    rows = []
    for h in range(9, 16):
        d = by_hour.get(h, {"pnl": 0, "trades": 0})
        rows.append({
            "hour":   f"{h:02d}:xx",
            "pnl":    round(d["pnl"], 0),
            "trades": d["trades"],
        })
    return json.dumps(rows)


def _build_rolling_js(trades):
    windows = rolling_windows(trades, [7, 14, 30])
    windows["all"] = trades

    rows = []
    for label, tw in [("7 Day", windows[7]), ("14 Day", windows[14]),
                      ("30 Day", windows[30]), ("All Time", windows["all"])]:
        if not tw:
            rows.append({"period": label, "pnl": "—", "wr": "—",
                          "pf": "—", "dd": "—", "best": "—", "worst": "—"})
            continue
        net  = sum(t["pnl_rs"] for t in tw)
        wr   = win_rate(tw)
        pf   = profit_factor(tw)
        dd   = max_drawdown(tw)
        best  = max(t["pnl_rs"] for t in tw)
        worst = min(t["pnl_rs"] for t in tw)
        rows.append({
            "period": label,
            "pnl":    _fmt_rs(net),
            "wr":     _pct((wr or 0) * 100),
            "pf":     _pf(pf),
            "dd":     _fmt_rs(-dd),
            "best":   _fmt_rs(best),
            "worst":  _fmt_rs(worst),
        })
    return json.dumps(rows)


def _build_whatif_js(whatif_result):
    rows = []
    for r in (whatif_result.get("results") or [])[:12]:
        rows.append({
            "filter":  r["filter"],
            "removed": r["removed"],
            "sim_pnl": r["sim_pnl"],
            "verdict": r["verdict"].lower(),
        })
    return json.dumps(rows)


def _build_summary(trades):
    if not trades:
        return {}
    wins   = [t for t in trades if t["pnl_rs"] > 0]
    losses = [t for t in trades if t["pnl_rs"] <= 0]
    n      = len(trades)
    gp     = sum(t["pnl_rs"] for t in wins)
    gl     = abs(sum(t["pnl_rs"] for t in losses))
    net    = sum(t["pnl_rs"] for t in trades)
    wr     = win_rate(trades)
    pf     = profit_factor(trades)
    exp    = expectancy(trades)
    dd     = max_drawdown(trades)
    g      = grade(wr, pf, exp, dd, n)
    avg_mfe = sum(t.get("mfe_pts") or 0 for t in trades) / n
    avg_cap = sum(t.get("capture_pct") or 0 for t in trades) / n
    best   = max(trades, key=lambda t: t["pnl_rs"])
    worst  = min(trades, key=lambda t: t["pnl_rs"])

    # Best/worst instrument
    by_inst = {}
    for t in trades:
        i = t.get("instrument","UNK")
        by_inst.setdefault(i, []).append(t["pnl_rs"])
    inst_net = {i: sum(v) for i, v in by_inst.items()}
    best_inst  = max(inst_net, key=inst_net.get) if inst_net else "—"
    worst_inst = min(inst_net, key=inst_net.get) if inst_net else "—"

    _, _, streak = consecutive_runs(trades)

    return {
        "n": n, "wins": len(wins), "losses": len(losses),
        "net": net, "gp": gp, "gl": gl,
        "wr": wr, "pf": pf, "exp": exp, "dd": dd, "grade": g,
        "grade_color": _grade_color(g),
        "avg_mfe": avg_mfe, "avg_cap": avg_cap,
        "best_trade": best, "worst_trade": worst,
        "best_inst": best_inst, "worst_inst": worst_inst,
        "streak": streak,
    }


# ── HTML builder ──────────────────────────────────────────────────────────────

def build_report(
    date: str = None,
    date_from: str = None,
    date_to: str = None,
    strategy: str = None,
    last_n: int = None,
) -> str:
    # Resolve date range
    today = datetime.now(IST).strftime("%Y-%m-%d")
    if last_n:
        cutoff = (datetime.now(IST) - timedelta(days=last_n)).strftime("%Y-%m-%d")
        date_from = cutoff
        date_to   = today
        label = f"Last {last_n} Days"
    elif date:
        date_from = date_to = date
        label = date
    elif date_from and date_to:
        label = f"{date_from} → {date_to}"
    else:
        date_from = date_to = today
        label = today

    # Load data
    trades = load_trades(date_from=date_from, date_to=date_to, strategy=strategy)
    summary = _build_summary(trades)

    # Run analytics modules
    decay_result   = run_edge_decay(strategy=strategy, save_snapshot=True)
    cooling_result = run_cooling_simulator(strategy=strategy,
                                           date_from=date_from, date_to=date_to)
    autopsy_result = run_loss_autopsy(strategy=strategy,
                                      date_from=date_from, date_to=date_to)
    whatif_result  = run_whatif(strategy=strategy,
                                date_from=date_from, date_to=date_to)

    # Build JS data blobs
    trades_js  = _build_trades_js(trades)
    hourly_js  = _build_hourly_js(trades)
    rolling_js = _build_rolling_js(trades)
    whatif_js  = _build_whatif_js(whatif_result)

    # ── Inline analytics sections ────────────────────────────

    # Edge Decay section HTML
    decay_html  = _render_edge_decay(decay_result)
    autopsy_html = _render_autopsy(autopsy_result)
    cooling_html = _render_cooling(cooling_result)
    whatif_extra = _render_whatif_detail(whatif_result)

    # ── CEO Summary values ───────────────────────────────────
    n        = summary.get("n", 0)
    net      = summary.get("net", 0)
    wr_pct   = (summary.get("wr") or 0) * 100
    pf_val   = summary.get("pf")
    g        = summary.get("grade", "N/A")
    gcolor   = summary.get("grade_color", "#5a6a78")
    best_inst = summary.get("best_inst", "—")
    streak   = summary.get("streak", "—")
    dd       = summary.get("dd", 0)
    avg_cap  = summary.get("avg_cap", 0)
    sys_status = decay_result.get("system_status", "N/A") if "error" not in decay_result else "N/A"
    status_css = _flag_css(sys_status)

    # Top autopsy cause
    top_cause = "—"
    if "error" not in autopsy_result:
        causes = autopsy_result.get("cause_ranking", [])
        if causes:
            top_cause = causes[0]["label"]

    # Strategy coach
    coach_html = _render_coach(summary, autopsy_result, whatif_result, decay_result)

    net_cls  = "pos" if net >= 0 else "neg"
    pf_cls   = "pos" if (pf_val or 0) >= 1.0 else "neg"
    wr_cls   = "pos" if wr_pct >= 50 else "warn" if wr_pct >= 40 else "neg"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OpenAlgo Research Platform — {label}</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@300;400;500;600;700&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap');
  :root {{
    --bg:#0a0c0e;--bg1:#0d1014;--bg2:#111518;--bg3:#161b1f;
    --border:#1e2428;--border2:#252c32;--dim:#3a4450;--muted:#5a6a78;
    --text:#c8d4dc;--text-hi:#e8f0f4;--label:#7a8c98;
    --green:#00c87a;--green-dim:#00441a;--red:#e8374a;--red-dim:#3d0a10;
    --yellow:#e8b84a;--yellow-dim:#3d2c00;--cyan:#4ab8d8;--cyan-dim:#0a2030;
    --orange:#e88040;--accent:#1e6fa8;
    --mono:'IBM Plex Mono',monospace;--sans:'IBM Plex Sans',sans-serif;
  }}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--text);font-family:var(--mono);font-size:12px;line-height:1.5;min-height:100vh}}
  .hdr{{background:var(--bg1);border-bottom:1px solid var(--border2);padding:10px 24px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100}}
  .logo{{font-size:13px;font-weight:700;letter-spacing:.12em;color:var(--text-hi);text-transform:uppercase}}
  .logo span{{color:var(--cyan)}}
  .hdr-meta{{font-size:11px;color:var(--muted);letter-spacing:.04em}}
  .status-dot{{display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--green);box-shadow:0 0 6px var(--green);margin-right:6px}}
  .page{{max-width:1400px;margin:0 auto;padding:16px 24px 64px}}
  .date-bar{{display:flex;align-items:center;justify-content:space-between;padding:12px 0;border-bottom:1px solid var(--border);margin-bottom:16px}}
  .date-label{{font-size:18px;font-weight:600;color:var(--text-hi);letter-spacing:.04em}}
  .date-sub{{font-size:11px;color:var(--muted);margin-top:2px}}
  .grade-box{{font-size:28px;font-weight:700;width:52px;height:52px;display:flex;align-items:center;justify-content:center;border:2px solid {gcolor};color:{gcolor}}}
  .sec{{margin-top:20px;margin-bottom:8px}}
  .sec-hdr{{font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);padding-bottom:6px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px}}
  .sec-hdr::before{{content:'▪';color:var(--cyan)}}
  /* Summary grid */
  .sg{{display:grid;grid-template-columns:repeat(8,1fr);gap:1px;background:var(--border);border:1px solid var(--border);margin-top:8px}}
  .sc{{background:var(--bg2);padding:12px 14px}}
  .sc:hover{{background:var(--bg3)}}
  .sc-lbl{{font-size:9px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin-bottom:6px}}
  .sc-val{{font-size:18px;font-weight:600;color:var(--text-hi);line-height:1.2}}
  .sc-val.pos{{color:var(--green)}} .sc-val.neg{{color:var(--red)}} .sc-val.warn{{color:var(--yellow)}}
  .sc-sub{{font-size:9px;color:var(--muted);margin-top:3px}}
  /* Two col */
  .two-col{{display:grid;grid-template-columns:1fr 1fr;gap:1px;margin-top:8px}}
  .three-col{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:1px;margin-top:8px}}
  /* Panel */
  .panel{{background:var(--bg2);border:1px solid var(--border);padding:16px}}
  .panel-title{{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin-bottom:12px}}
  /* Stat rows */
  .stat-row{{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid var(--border)}}
  .stat-row:last-child{{border-bottom:none}}
  .stat-key{{color:var(--muted);font-size:11px}}
  .stat-val{{font-size:11px;color:var(--text-hi)}}
  .stat-val.pos{{color:var(--green)}} .stat-val.neg{{color:var(--red)}} .stat-val.warn{{color:var(--yellow)}}
  /* Trade table */
  .tbl{{width:100%;border-collapse:collapse;margin-top:8px;font-size:11px}}
  .tbl th{{background:var(--bg1);color:var(--muted);font-size:9px;letter-spacing:.08em;text-transform:uppercase;padding:6px 10px;text-align:left;border-bottom:1px solid var(--border2)}}
  .tbl td{{padding:5px 10px;border-bottom:1px solid var(--border);color:var(--text)}}
  .tbl tr:hover td{{background:var(--bg3)}}
  .td-pos{{color:var(--green)}} .td-neg{{color:var(--red)}} .td-warn{{color:var(--yellow)}} .td-dim{{color:var(--muted)}} .td-hi{{color:var(--text-hi)}}
  /* Pills */
  .pill{{font-size:9px;letter-spacing:.06em;padding:2px 7px;text-transform:uppercase;font-weight:600}}
  .pill-ce{{background:rgba(0,200,122,.15);color:var(--green);border:1px solid var(--green-dim)}}
  .pill-pe{{background:rgba(232,55,74,.15);color:var(--red);border:1px solid var(--red-dim)}}
  .pill-buy{{background:rgba(74,184,216,.15);color:var(--cyan);border:1px solid var(--cyan-dim)}}
  .pill-target{{background:rgba(0,200,122,.15);color:var(--green)}}
  .pill-sl{{background:rgba(232,55,74,.15);color:var(--red)}}
  .pill-trail{{background:rgba(74,184,216,.15);color:var(--cyan)}}
  .pill-hw{{background:rgba(232,184,74,.15);color:var(--yellow)}}
  .pill-nm{{background:rgba(232,128,64,.15);color:var(--orange)}}
  .pill-time{{background:rgba(90,106,120,.15);color:var(--muted)}}
  .pill-manual{{background:rgba(90,106,120,.15);color:var(--muted)}}
  /* Verdict */
  .verdict{{font-size:9px;font-weight:700;letter-spacing:.08em;padding:2px 8px;text-transform:uppercase}}
  .verdict-adopt{{background:rgba(0,200,122,.2);color:var(--green);border:1px solid var(--green)}}
  .verdict-test{{background:rgba(232,184,74,.2);color:var(--yellow);border:1px solid var(--yellow)}}
  .verdict-skip{{background:rgba(90,106,120,.15);color:var(--muted);border:1px solid var(--border2)}}
  /* Flag */
  .flag{{font-size:9px;font-weight:700;letter-spacing:.1em;padding:2px 8px;text-transform:uppercase}}
  .flag-ok{{color:var(--green);border:1px solid var(--green)}}
  .flag-warn{{color:var(--yellow);border:1px solid var(--yellow)}}
  .flag-alert{{color:var(--red);border:1px solid var(--red)}}
  .flag-dim{{color:var(--muted);border:1px solid var(--border2)}}
  /* Decay window cards */
  .decay-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--border);margin-top:8px}}
  .decay-card{{background:var(--bg2);padding:14px}}
  .decay-period{{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin-bottom:10px}}
  /* Cause bars */
  .cause-row{{display:flex;align-items:center;gap:12px;padding:6px 0;border-bottom:1px solid var(--border)}}
  .cause-label{{width:240px;font-size:11px;color:var(--text)}}
  .cause-bar-track{{flex:1;height:4px;background:var(--border2)}}
  .cause-bar-fill{{height:100%;background:var(--red)}}
  .cause-count{{width:30px;text-align:right;font-size:11px;color:var(--muted)}}
  .cause-pnl{{width:90px;text-align:right;font-size:11px;color:var(--red)}}
  /* Equity canvas */
  .tbl th[data-col]:hover{{color:var(--cyan)}}
  .tbl th.sort-asc::after{{content:' ↑';color:var(--cyan)}}
  .tbl th.sort-desc::after{{content:' ↓';color:var(--cyan)}}
  select:focus,input:focus{{outline:1px solid var(--cyan)}}
  .canvas-wrap{{background:var(--bg2);border:1px solid var(--border);padding:16px;margin-top:8px}}
  /* Cooling grid */
  .cool-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--border);margin-top:8px}}
  .cool-card{{background:var(--bg2);padding:12px 14px}}
  .cool-trigger{{font-size:9px;letter-spacing:.08em;text-transform:uppercase;color:var(--cyan);margin-bottom:6px}}
  /* Coach */
  .coach-section{{background:var(--bg2);border:1px solid var(--border);border-left:3px solid var(--cyan);padding:16px;margin-top:8px}}
  .coach-row{{padding:6px 0;border-bottom:1px solid var(--border);display:flex;gap:12px}}
  .coach-label{{width:200px;font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);flex-shrink:0}}
  .coach-text{{font-size:11px;color:var(--text-hi)}}
  /* Whatif table */
  .wi-row{{display:grid;grid-template-columns:3fr 80px 140px 110px 100px;gap:0;padding:6px 12px;border-bottom:1px solid var(--border)}}
  .wi-row:hover{{background:var(--bg3)}}
  .wi-hdr{{display:grid;grid-template-columns:3fr 80px 140px 110px 100px;padding:6px 12px;background:var(--bg1);font-size:9px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--border2)}}
  .wi-filter{{font-size:11px;color:var(--text)}}
  .wi-cell{{font-size:11px;text-align:right;color:var(--text-hi)}}
  /* Status badge */
  .status-badge{{display:inline-flex;align-items:center;gap:6px;font-size:10px;padding:4px 12px;border:1px solid;letter-spacing:.08em;text-transform:uppercase}}
</style>
</head>
<body>

<div class="hdr">
  <div class="logo">OPEN<span>ALGO</span> &nbsp;·&nbsp; Research Platform</div>
  <div class="hdr-meta">
    <span class="status-dot"></span>
    {label} &nbsp;·&nbsp; Generated {datetime.now(IST).strftime('%H:%M IST')}
    {' &nbsp;·&nbsp; ' + strategy.upper() if strategy else ''}
  </div>
</div>

<div class="page">

<!-- DATE BAR -->
<div class="date-bar">
  <div>
    <div class="date-label">{label}</div>
    <div class="date-sub">{n} trades &nbsp;·&nbsp; {summary.get("wins",0)}W {summary.get("losses",0)}L &nbsp;·&nbsp; Streak: {streak}</div>
  </div>
  <div style="display:flex;align-items:center;gap:16px">
    <div class="status-badge {status_css}">Edge: {sys_status}</div>
    <div class="grade-box">{g}</div>
  </div>
</div>

<!-- CEO SUMMARY -->
<div class="sec"><div class="sec-hdr">Executive Summary</div></div>
<div class="sg">
  <div class="sc">
    <div class="sc-lbl">Net P&amp;L</div>
    <div class="sc-val {net_cls}">{_fmt_rs(net)}</div>
    <div class="sc-sub">Gross +{_fmt_rs(summary.get("gp",0))} / {_fmt_rs(-summary.get("gl",0))}</div>
  </div>
  <div class="sc">
    <div class="sc-lbl">Win Rate</div>
    <div class="sc-val {wr_cls}">{wr_pct:.1f}%</div>
    <div class="sc-sub">{summary.get("wins",0)}W / {summary.get("losses",0)}L</div>
  </div>
  <div class="sc">
    <div class="sc-lbl">Profit Factor</div>
    <div class="sc-val {pf_cls}">{_pf(pf_val)}</div>
    <div class="sc-sub">≥1.5 = good</div>
  </div>
  <div class="sc">
    <div class="sc-lbl">Expectancy</div>
    <div class="sc-val {'pos' if (summary.get('exp') or 0)>0 else 'neg'}">{_fmt_rs(summary.get('exp'))}</div>
    <div class="sc-sub">per trade avg</div>
  </div>
  <div class="sc">
    <div class="sc-lbl">Max Drawdown</div>
    <div class="sc-val neg">{_fmt_rs(-dd)}</div>
    <div class="sc-sub">peak to trough</div>
  </div>
  <div class="sc">
    <div class="sc-lbl">Avg MFE</div>
    <div class="sc-val">{summary.get('avg_mfe',0):.1f} pts</div>
    <div class="sc-sub">Avg capture: {avg_cap:.0f}%</div>
  </div>
  <div class="sc">
    <div class="sc-lbl">Best Instrument</div>
    <div class="sc-val" style="font-size:14px">{best_inst}</div>
    <div class="sc-sub">by net P&amp;L</div>
  </div>
  <div class="sc">
    <div class="sc-lbl">Top Failure</div>
    <div class="sc-val warn" style="font-size:11px">{top_cause[:18]}</div>
    <div class="sc-sub">primary loss cause</div>
  </div>
</div>

<!-- EQUITY CURVE -->
<div class="sec"><div class="sec-hdr">Equity Curve</div></div>
<div class="canvas-wrap"><canvas id="equity-canvas" height="160"></canvas></div>

<!-- EDGE DECAY -->
{decay_html}

<!-- LOSS AUTOPSY -->
{autopsy_html}

<!-- COOLING SIMULATOR -->
{cooling_html}

<!-- WHAT-IF -->
{whatif_extra}

<!-- STRATEGY COACH -->
{coach_html}

<!-- TRADE LOG -->
<div class="sec"><div class="sec-hdr">Trade Log
  <span style="margin-left:auto;display:flex;gap:8px">
    <span id="live-n" style="font-size:11px;color:var(--muted)"></span>
    <span id="live-wr" style="font-size:11px"></span>
    <span id="live-pf" style="font-size:11px"></span>
    <span id="live-net" style="font-size:11px;font-weight:600"></span>
  </span>
</div></div>
<div style="background:var(--bg2);border:1px solid var(--border);border-bottom:none;padding:10px 14px;display:flex;flex-wrap:wrap;gap:8px">
  <select id="f-date" onchange="applyFilters()" style="background:var(--bg3);color:var(--text);border:1px solid var(--border2);padding:4px 8px;font-family:var(--mono);font-size:11px"><option value="">All Dates</option></select>
  <select id="f-strat" onchange="applyFilters()" style="background:var(--bg3);color:var(--text);border:1px solid var(--border2);padding:4px 8px;font-family:var(--mono);font-size:11px"><option value="">All Strategies</option></select>
  <select id="f-dir" onchange="applyFilters()" style="background:var(--bg3);color:var(--text);border:1px solid var(--border2);padding:4px 8px;font-family:var(--mono);font-size:11px"><option value="">All Directions</option></select>
  <select id="f-exit" onchange="applyFilters()" style="background:var(--bg3);color:var(--text);border:1px solid var(--border2);padding:4px 8px;font-family:var(--mono);font-size:11px"><option value="">All Exit Reasons</option></select>
  <select id="f-result" onchange="applyFilters()" style="background:var(--bg3);color:var(--text);border:1px solid var(--border2);padding:4px 8px;font-family:var(--mono);font-size:11px"><option value="">Win + Loss</option><option value="win">Wins Only</option><option value="loss">Losses Only</option></select>
  <input id="f-search" oninput="applyFilters()" placeholder="Search symbol..." style="background:var(--bg3);color:var(--text);border:1px solid var(--border2);padding:4px 8px;font-family:var(--mono);font-size:11px;width:140px">
  <button onclick="resetFilters()" style="background:var(--bg3);color:var(--muted);border:1px solid var(--border2);padding:4px 12px;font-family:var(--mono);font-size:11px;cursor:pointer">RESET</button>
</div>
<div style="background:var(--bg2);border:1px solid var(--border);overflow-x:auto">
  <table class="tbl">
    <thead>
      <tr>
        <th>#</th><th>Strategy</th><th>Symbol</th><th>Dir</th>
        <th>Entry</th><th>Exit</th><th>Entry ₹</th><th>Exit ₹</th>
        <th>P&amp;L</th><th>MFE</th><th>MAE</th><th>Cap%</th>
        <th>Hold</th><th>Exit</th>
      </tr>
    </thead>
    <tbody id="trade-tbody"></tbody>
  </table>
</div>

</div><!-- /page -->

<script>
const TRADES  = {trades_js};
const HOURLY  = {hourly_js};
const ROLLING = {rolling_js};
const WHATIF  = {whatif_js};

// Equity curve
(function(){{
  const canvas = document.getElementById('equity-canvas');
  if (!canvas) return;
  const W = canvas.parentElement.offsetWidth - 32;
  canvas.width = W;
  const H = 160;
  const ctx = canvas.getContext('2d');
  let running = 0;
  const pts = [{{x:0,y:0}}];
  TRADES.forEach((t,i)=>{{ running+=t.pnl_rs; pts.push({{x:i+1,y:running}}); }});
  const minY=Math.min(...pts.map(p=>p.y));
  const maxY=Math.max(...pts.map(p=>p.y));
  const rY=maxY-minY||1;
  const pad={{t:10,r:10,b:20,l:72}};
  const toX=i=>pad.l+(i/(pts.length-1||1))*(W-pad.l-pad.r);
  const toY=v=>pad.t+(1-(v-minY)/rY)*(H-pad.t-pad.b);
  ctx.strokeStyle='#1e2428'; ctx.lineWidth=1;
  for(let g=0;g<=4;g++){{
    const y=pad.t+g*(H-pad.t-pad.b)/4;
    ctx.beginPath();ctx.moveTo(pad.l,y);ctx.lineTo(W-pad.r,y);ctx.stroke();
    const val=maxY-g*rY/4;
    ctx.fillStyle='#5a6a78';ctx.font='9px IBM Plex Mono';ctx.textAlign='right';
    ctx.fillText((val>0?'+':'')+'₹'+Math.round(val).toLocaleString('en-IN'),pad.l-4,y+3);
  }}
  const grad=ctx.createLinearGradient(0,0,0,H);
  grad.addColorStop(0,'rgba(0,200,122,0.12)');grad.addColorStop(1,'rgba(0,200,122,0)');
  ctx.beginPath();ctx.moveTo(toX(0),toY(0));
  pts.forEach((p,i)=>{{if(i>0)ctx.lineTo(toX(i),toY(p.y));}});
  ctx.lineTo(toX(pts.length-1),H-pad.b);ctx.lineTo(toX(0),H-pad.b);
  ctx.closePath();ctx.fillStyle=grad;ctx.fill();
  ctx.beginPath();
  pts.forEach((p,i)=>{{if(i===0)ctx.moveTo(toX(i),toY(p.y));else ctx.lineTo(toX(i),toY(p.y));}});
  ctx.strokeStyle='#00c87a';ctx.lineWidth=1.5;ctx.stroke();
  if(minY<0&&maxY>0){{
    ctx.beginPath();ctx.moveTo(pad.l,toY(0));ctx.lineTo(W-pad.r,toY(0));
    ctx.strokeStyle='#3a4450';ctx.lineWidth=1;ctx.setLineDash([4,4]);ctx.stroke();ctx.setLineDash([]);
  }}
  pts.forEach((p,i)=>{{
    if(i===0)return;
    ctx.beginPath();ctx.arc(toX(i),toY(p.y),3,0,Math.PI*2);
    ctx.fillStyle=TRADES[i-1].pnl_rs>0?'#00c87a':'#e8374a';ctx.fill();
  }});
}})();

// ── Interactive trade table ──────────────────────────────────
let filtered=[...TRADES]; let sortCol=null; let sortDir=1;
function populateFilters(){{
  const sel=(id,vals,labels)=>{{const s=document.getElementById(id);vals.forEach((v,i)=>{{const o=document.createElement('option');o.value=v;o.text=labels?labels[i]:v;s.appendChild(o);}});}};
  sel('f-date',[...new Set(TRADES.map(t=>t.date))].sort());
  sel('f-strat',[...new Set(TRADES.map(t=>t.strategy))].sort());
  sel('f-dir',[...new Set(TRADES.map(t=>t.direction))].sort());
  sel('f-exit',[...new Set(TRADES.map(t=>t.exit_reason))].sort(),[...new Set(TRADES.map(t=>t.exit_reason))].sort().map(v=>v.replace(/_/g,' ')));
}}
function applyFilters(){{
  const d=document.getElementById('f-date').value,s=document.getElementById('f-strat').value,
    dr=document.getElementById('f-dir').value,e=document.getElementById('f-exit').value,
    r=document.getElementById('f-result').value,q=document.getElementById('f-search').value.toLowerCase();
  filtered=TRADES.filter(t=>{{
    if(d&&t.date!==d)return false; if(s&&t.strategy!==s)return false;
    if(dr&&t.direction!==dr)return false; if(e&&t.exit_reason!==e)return false;
    if(r==='win'&&t.pnl_rs<=0)return false; if(r==='loss'&&t.pnl_rs>0)return false;
    if(q&&!t.symbol.toLowerCase().includes(q)&&!t.strategy.toLowerCase().includes(q))return false;
    return true;
  }});
  if(sortCol)filtered.sort((a,b)=>{{let av=a[sortCol],bv=b[sortCol];if(typeof av==='string'){{av=av.toLowerCase();bv=bv.toLowerCase();}}return av>bv?sortDir:av<bv?-sortDir:0;}});
  renderTable(); updateBar();
}}
function resetFilters(){{
  ['f-date','f-strat','f-dir','f-exit','f-result'].forEach(id=>document.getElementById(id).value='');
  document.getElementById('f-search').value='';
  sortCol=null;sortDir=1;
  document.querySelectorAll('.tbl th').forEach(th=>th.classList.remove('sort-asc','sort-desc'));
  filtered=[...TRADES];renderTable();updateBar();
}}
function setSort(col){{
  if(sortCol===col)sortDir*=-1;else{{sortCol=col;sortDir=1;}}
  document.querySelectorAll('.tbl th').forEach(th=>th.classList.remove('sort-asc','sort-desc'));
  const th=document.querySelector(`th[data-col="${{col}}"]`);
  if(th)th.classList.add(sortDir===1?'sort-asc':'sort-desc');
  applyFilters();
}}
function updateBar(){{
  const n=filtered.length,wins=filtered.filter(t=>t.pnl_rs>0).length;
  const net=filtered.reduce((s,t)=>s+t.pnl_rs,0);
  const gp=filtered.filter(t=>t.pnl_rs>0).reduce((s,t)=>s+t.pnl_rs,0);
  const gl=Math.abs(filtered.filter(t=>t.pnl_rs<=0).reduce((s,t)=>s+t.pnl_rs,0));
  const pf=gl>0?(gp/gl).toFixed(2):'∞';
  const wr=n>0?(wins/n*100).toFixed(1):0;
  document.getElementById('live-n').textContent=n+' trades';
  const wre=document.getElementById('live-wr');wre.textContent=wr+'%';wre.className=parseFloat(wr)>=50?'td-pos':'td-neg';
  const pfe=document.getElementById('live-pf');pfe.textContent='PF '+pf;pfe.className=parseFloat(pf)>=1?'td-pos':'td-neg';
  const ne=document.getElementById('live-net');ne.textContent=(net>=0?'+':'')+'₹'+Math.abs(Math.round(net)).toLocaleString('en-IN');ne.className=net>=0?'td-pos':'td-neg';
}}
const tbody=document.getElementById('trade-tbody');
const dirPillMap={{CE:'pill-ce',PE:'pill-pe',BUY:'pill-buy'}};
const exitPillMap={{TARGET:'pill-target',TRAIL:'pill-trail',TRAILING_SL:'pill-trail',
  SL:'pill-sl',SL_HIT:'pill-sl',HIGH_WATER_EXIT:'pill-hw',
  NO_MOMENTUM_EXIT:'pill-nm',TIME_EXIT:'pill-time',HARD_EXIT:'pill-time'}};
function renderTable(){{
  tbody.innerHTML='';
  if(!filtered.length){{tbody.innerHTML='<tr><td colspan="15" style="text-align:center;color:var(--muted);padding:20px">No trades match filters</td></tr>';return;}}
  filtered.forEach((t,i)=>{{
  const pnlCls=t.pnl_rs>0?'td-pos':t.pnl_rs<0?'td-neg':'td-dim';
  const pnlStr=(t.pnl_rs>=0?'+':'')+'₹'+Math.abs(t.pnl_rs).toLocaleString('en-IN');
  const capStr=t.capture_pct>0?t.capture_pct.toFixed(0)+'%':'—';
  const dur=t.hold_seconds<60?t.hold_seconds+'s':Math.floor(t.hold_seconds/60)+'m';
  const dp=dirPillMap[t.direction]||'';
  const ep=exitPillMap[t.exit_reason]||'';
  tbody.innerHTML+=`<tr>
    <td class="td-dim">${{i+1}}</td>
    <td class="td-hi">${{t.strategy}}</td>
    <td>${{t.symbol}}</td>
    <td><span class="pill ${{dp}}">${{t.direction}}</span></td>
    <td class="td-dim">${{t.entry_time}}</td>
    <td class="td-dim">${{t.exit_time}}</td>
    <td>${{t.entry_price.toFixed(1)}}</td>
    <td>${{t.exit_price.toFixed(1)}}</td>
    <td class="${{pnlCls}}">${{pnlStr}}</td>
    <td>${{t.mfe_pts.toFixed(1)}}</td>
    <td class="td-neg">${{t.mae_pts.toFixed(1)}}</td>
    <td class="${{t.capture_pct>=70?'td-pos':t.capture_pct>0?'td-warn':'td-neg'}}">${{capStr}}</td>
    <td class="td-dim">${{dur}}</td>
    <td><span class="pill ${{ep}}">${{t.exit_reason.replace(/_/g,' ')}}</span></td>
  </tr>`;
  }});
}}
populateFilters();renderTable();updateBar();
document.querySelectorAll('.tbl th[data-col]').forEach(th=>{{th.style.cursor='pointer';th.addEventListener('click',()=>setSort(th.dataset.col));}});
</script>
</body>
</html>"""
    return html


# ── Section renderers ─────────────────────────────────────────────────────────

def _render_edge_decay(result):
    if "error" in result:
        return f'<div class="sec"><div class="sec-hdr">Edge Decay Detector</div></div><div class="panel"><div class="panel-title" style="color:var(--red)">{result["error"]}</div></div>'

    windows = result.get("windows", {})
    trends  = result.get("trends", {})
    streaks = result.get("streaks", {})
    daily   = result.get("daily_metrics", [])

    def _arrow(t):
        return {"IMPROVING":"↑","DETERIORATING":"↓","FLAT":"→"}.get(t,"·")
    def _arrow_css(t):
        return {"IMPROVING":"td-pos","DETERIORATING":"td-neg","FLAT":"td-dim"}.get(t,"td-dim")

    cards = ""
    for label in ["7d","14d","30d","all"]:
        w = windows.get(label, {})
        s = w.get("stats", {})
        f = w.get("flag", "N/A")
        reasons = w.get("reasons", [])
        flag_css = _flag_css(f)
        n  = s.get("n") or 0
        wr = f'{(s.get("win_rate") or 0)*100:.1f}%' if s.get("win_rate") is not None else "—"
        pf = f'{s.get("profit_factor"):.2f}' if s.get("profit_factor") else "—"
        ex = f'₹{s.get("expectancy"):.0f}' if s.get("expectancy") is not None else "—"
        g  = s.get("grade","N/A")
        cards += f"""
        <div class="decay-card">
          <div class="decay-period">{label.upper()} &nbsp;<span class="flag {flag_css}">{f}</span></div>
          <div class="stat-row"><span class="stat-key">Trades</span><span class="stat-val">{n}</span></div>
          <div class="stat-row"><span class="stat-key">Win Rate</span><span class="stat-val {'pos' if n>0 and (s.get('win_rate') or 0)>=0.5 else 'neg'}">{wr}</span></div>
          <div class="stat-row"><span class="stat-key">Profit Factor</span><span class="stat-val {'pos' if s.get('profit_factor') and s['profit_factor']>=1 else 'neg'}">{pf}</span></div>
          <div class="stat-row"><span class="stat-key">Expectancy</span><span class="stat-val {'pos' if (s.get('expectancy') or 0)>0 else 'neg'}">{ex}</span></div>
          <div class="stat-row"><span class="stat-key">Grade</span><span class="stat-val">{g}</span></div>
          <div style="margin-top:8px;font-size:9px;color:var(--muted)">{reasons[0][:60] if reasons else ''}</div>
        </div>"""

    trend_row = ""
    for metric, t in trends.items():
        trend_row += f'<span style="margin-right:16px;font-size:11px">{metric.replace("_"," ").title()}: <span class="{_arrow_css(t)}">{_arrow(t)} {t}</span></span>'

    # Daily performance mini-table
    daily_rows = ""
    for d in daily[-7:]:
        net_cls = "td-pos" if (d.get("net_pnl") or 0) >= 0 else "td-neg"
        daily_rows += f"""<tr>
          <td class="td-dim">{d['date']}</td>
          <td>{d['n']}</td>
          <td class="{'td-pos' if (d.get('win_rate') or 0)>=50 else 'td-neg'}">{d.get('win_rate','—')}%</td>
          <td class="{'td-pos' if (d.get('profit_factor') or 0)>=1 else 'td-neg'}">{d.get('profit_factor','—')}</td>
          <td class="{net_cls}">{'+'if(d.get('net_pnl') or 0)>=0 else ''}₹{abs(d.get('net_pnl') or 0):,.0f}</td>
        </tr>"""

    return f"""
<div class="sec"><div class="sec-hdr">Edge Decay Detector
  <span style="margin-left:auto;font-size:10px">{trend_row}</span>
</div></div>
<div class="decay-grid">{cards}</div>
<div style="margin-top:8px;background:var(--bg2);border:1px solid var(--border);padding:12px">
  <div style="font-size:9px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin-bottom:8px">Daily Breakdown (last 7 days)</div>
  <table class="tbl">
    <thead><tr><th>Date</th><th>Trades</th><th>Win%</th><th>PF</th><th>Net</th></tr></thead>
    <tbody>{daily_rows}</tbody>
  </table>
  <div style="margin-top:10px;font-size:11px;color:var(--muted)">
    Max win streak: <span style="color:var(--green)">{streaks.get('max_wins',0)}W</span> &nbsp;·&nbsp;
    Max loss streak: <span style="color:var(--red)">{streaks.get('max_losses',0)}L</span> &nbsp;·&nbsp;
    Current: <span style="color:var(--cyan)">{streaks.get('current','—')}</span>
  </div>
</div>"""


def _render_autopsy(result):
    if "error" in result:
        return f'<div class="sec"><div class="sec-hdr">Loss Autopsy Engine</div></div><div class="panel"><div class="panel-title" style="color:var(--muted)">{result["error"]}</div></div>'

    s       = result.get("summary", {})
    causes  = result.get("cause_ranking", [])
    by_time = result.get("loss_by_time", [])
    adx_a   = result.get("adx_analysis", [])
    recs    = result.get("recommendations", [])

    max_loss = abs(causes[0]["total_loss"]) if causes else 1

    cause_rows = ""
    for c in causes[:6]:
        pct  = min(100, abs(c["total_loss"]) / max_loss * 100)
        type_css = "td-warn" if c["type"] == "behavioral" else "td-neg"
        cause_rows += f"""
        <div class="cause-row">
          <div class="cause-label">{c['label']}</div>
          <div style="font-size:9px;width:60px;color:var(--muted)">{c['type']}</div>
          <div class="cause-bar-track"><div class="cause-bar-fill" style="width:{pct:.0f}%"></div></div>
          <div class="cause-count">{c['count']}</div>
          <div class="cause-pnl">₹{abs(c['total_loss']):,.0f}</div>
        </div>"""

    time_rows = ""
    for b in by_time:
        time_rows += f"""<tr>
          <td class="td-dim">{b['bucket']}</td>
          <td>{b['count']}</td>
          <td class="td-neg">₹{abs(b['total_loss']):,.0f}</td>
          <td class="td-neg">₹{abs(b['avg_loss']):,.0f}</td>
        </tr>"""

    adx_rows = ""
    for a in adx_a:
        wr_css = "td-pos" if a["win_rate"] >= 50 else "td-neg"
        net_css = "td-pos" if a["net_pnl"] >= 0 else "td-neg"
        adx_rows += f"""<tr>
          <td class="td-dim">{a['bracket']}</td>
          <td>{a['n']}</td>
          <td class="{wr_css}">{a['win_rate']}%</td>
          <td class="{'td-pos' if (a.get('pf') or 0)>=1 else 'td-neg'}">{a.get('pf','—')}</td>
          <td class="{net_css}">{'+'if a['net_pnl']>=0 else ''}₹{abs(a['net_pnl']):,.0f}</td>
        </tr>"""

    rec_html = ""
    for r in recs:
        p_css = {"HIGH":"red","MEDIUM":"yellow","LOW":"cyan"}.get(r.get("priority",""),"muted")
        rec_html += f"""
        <div style="padding:8px 0;border-bottom:1px solid var(--border)">
          <div style="font-size:9px;color:var(--{p_css});letter-spacing:.08em;text-transform:uppercase;margin-bottom:4px">{r.get('priority','')} PRIORITY — {r.get('action','')}</div>
          <div style="font-size:11px;color:var(--muted)">{r.get('rationale','')}</div>
        </div>"""

    b_loss = s.get("behavioral_loss", 0)
    sy_loss = s.get("system_loss", 0)

    return f"""
<div class="sec"><div class="sec-hdr">Loss Autopsy Engine
  <span class="td-dim" style="margin-left:auto;font-size:10px">{s.get('total_losses',0)} losses analysed · {s.get('classified_pct',0):.0f}% classified</span>
</div></div>
<div class="two-col">
  <div style="background:var(--bg2);border:1px solid var(--border);padding:14px">
    <div class="panel-title">Failure Cause Ranking</div>
    {cause_rows}
    <div style="margin-top:12px;display:flex;gap:24px;font-size:11px">
      <span>Behavioral: <span class="td-warn">₹{abs(b_loss):,.0f} ({s.get('behavioral_count',0)} trades)</span></span>
      <span>System: <span class="td-neg">₹{abs(sy_loss):,.0f} ({s.get('system_count',0)} trades)</span></span>
    </div>
  </div>
  <div style="background:var(--bg2);border:1px solid var(--border);padding:14px">
    <div class="panel-title">ADX Performance Matrix</div>
    <table class="tbl"><thead><tr><th>ADX</th><th>Trades</th><th>WR%</th><th>PF</th><th>Net</th></tr></thead>
    <tbody>{adx_rows}</tbody></table>
    <div style="margin-top:14px">
      <div class="panel-title">Loss by Time Bucket</div>
      <table class="tbl"><thead><tr><th>Window</th><th>Losses</th><th>Total</th><th>Avg</th></tr></thead>
      <tbody>{time_rows}</tbody></table>
    </div>
  </div>
</div>
{ f'<div style="background:var(--bg2);border:1px solid var(--border);border-left:3px solid var(--yellow);padding:14px;margin-top:8px"><div class="panel-title">Recommendations</div>{rec_html}</div>' if rec_html else '' }"""


def _render_cooling(result):
    if "error" in result:
        return f'<div class="sec"><div class="sec-hdr">Cooling Period Simulator</div></div><div class="panel"><div class="panel-title" style="color:var(--muted)">{result["error"]}</div></div>'

    summary = result.get("trigger_summary", {})
    best    = result.get("best_recommendation")

    best_html = ""
    if best:
        delta = best["delta"]["pnl"]
        best_html = f"""
        <div style="background:rgba(0,200,122,.06);border:1px solid var(--green);padding:12px;margin-top:8px;display:flex;align-items:center;justify-content:space-between">
          <div>
            <div style="font-size:9px;letter-spacing:.1em;text-transform:uppercase;color:var(--green);margin-bottom:4px">Best Recommendation</div>
            <div style="font-size:13px;color:var(--text-hi)">{best['trigger_label']} · {best['cooldown_min']}min cooldown</div>
            <div style="font-size:11px;color:var(--muted);margin-top:4px">Removes {best['trades_removed']} trades · Simulated P&L improvement</div>
          </div>
          <div style="text-align:right">
            <div style="font-size:22px;font-weight:700;color:var(--green)">+₹{delta:,.0f}</div>
            <span class="verdict verdict-adopt">ADOPT</span>
          </div>
        </div>"""

    # Table of best per trigger
    trigger_rows = ""
    for trigger, r in summary.items():
        delta = r["delta"]["pnl"]
        v_css = _verdict_css(r["verdict"])
        trigger_rows += f"""<tr>
          <td class="td-hi">{r['trigger_label']}</td>
          <td>{r['cooldown_min']}min</td>
          <td>{r['trades_removed']}</td>
          <td class="{'td-pos' if r['simulated']['net_pnl']>=0 else 'td-neg'}">₹{r['simulated']['net_pnl']:,.0f}</td>
          <td class="{'td-pos' if delta>0 else 'td-neg'}">{'+' if delta>=0 else ''}₹{delta:,.0f}</td>
          <td><span class="verdict {v_css}">{r['verdict']}</span></td>
        </tr>"""

    return f"""
<div class="sec"><div class="sec-hdr">Cooling Period Simulator</div></div>
{best_html}
<div style="background:var(--bg2);border:1px solid var(--border);margin-top:8px">
  <table class="tbl">
    <thead><tr><th>Trigger</th><th>Cooldown</th><th>Removed</th><th>Sim P&L</th><th>Delta</th><th>Verdict</th></tr></thead>
    <tbody>{trigger_rows}</tbody>
  </table>
</div>"""


def _render_whatif_detail(result):
    if "error" in result:
        return f'<div class="sec"><div class="sec-hdr">What-If Research Engine</div></div><div class="panel"><div class="panel-title" style="color:var(--muted)">{result["error"]}</div></div>'

    rows_html = ""
    for r in result.get("results", [])[:16]:
        delta = r["delta"]
        v_css = _verdict_css(r["verdict"])
        delta_css = "td-pos" if delta > 0 else "td-neg" if delta < 0 else "td-dim"
        rows_html += f"""
        <div class="wi-row">
          <div class="wi-filter">{r['filter']}</div>
          <div class="wi-cell td-dim">{r['removed']}</div>
          <div class="wi-cell {'td-pos' if r['sim_pnl']>=0 else 'td-neg'}">₹{r['sim_pnl']:,.0f}</div>
          <div class="wi-cell {delta_css}">{'+' if delta>=0 else ''}₹{delta:,.0f}</div>
          <div class="wi-cell" style="text-align:right;padding-right:12px"><span class="verdict {v_css}">{r['verdict']}</span></div>
        </div>"""

    adopt_count = result.get("adopt_count", 0)
    test_count  = result.get("test_count", 0)

    return f"""
<div class="sec"><div class="sec-hdr">What-If Research Engine
  <span class="td-dim" style="margin-left:auto;font-size:10px">
    <span class="td-pos">{adopt_count} ADOPT</span> &nbsp;·&nbsp;
    <span class="td-warn">{test_count} TEST</span>
  </span>
</div></div>
<div style="background:var(--bg2);border:1px solid var(--border)">
  <div class="wi-hdr">
    <div>Filter</div><div style="text-align:right">Removed</div>
    <div style="text-align:right">Sim P&L</div>
    <div style="text-align:right">Delta</div>
    <div style="text-align:right;padding-right:12px">Verdict</div>
  </div>
  {rows_html}
</div>"""


def _render_coach(summary, autopsy, whatif, decay):
    n   = summary.get("n", 0)
    net = summary.get("net", 0)
    g   = summary.get("grade", "N/A")
    best_t  = summary.get("best_trade")
    worst_t = summary.get("worst_trade")

    if not n:
        return ""

    # Derive coach narrative
    wr_pct = (summary.get("wr") or 0) * 100
    pf_val = summary.get("pf")

    if net > 0 and wr_pct >= 50:
        day_assessment = f"Profitable session. Grade {g}. Strategy showing edge in current conditions."
    elif net > 0 and wr_pct < 50:
        day_assessment = f"Profitable but low win rate ({wr_pct:.0f}%). Winning on size, not frequency. Verify entries are high quality."
    elif net < 0 and wr_pct >= 50:
        day_assessment = f"Majority winners but still negative P&L. SL slippage or loss sizing is the problem."
    else:
        day_assessment = f"Below expectations. Win rate {wr_pct:.0f}%, PF {_pf(pf_val)}. Review entry filters."

    # Top autopsy cause
    causes = autopsy.get("cause_ranking", []) if "error" not in autopsy else []
    top_cause_text = causes[0]["label"] if causes else "Insufficient data"

    # Best whatif
    best_wi = whatif.get("best_recommendation") if "error" not in whatif else None
    tomorrow_rule = f"Test: {best_wi['filter']} (simulated +₹{best_wi['delta']:,.0f})" if best_wi else "Collect more data before changing rules"

    # Edge status
    sys_status = decay.get("system_status","N/A") if "error" not in decay else "N/A"
    edge_text = {
        "OK": "Edge intact. Continue with current parameters.",
        "WARN": "Early degradation detected. Monitor closely. Do not increase position size.",
        "ALERT": "Edge deteriorating. Reduce trade frequency. Review rules before tomorrow.",
        "INSUFFICIENT_DATA": "Insufficient data for reliable edge assessment."
    }.get(sys_status, "Monitor.")

    best_str  = f"₹{best_t['pnl_rs']:+,.0f} — {best_t.get('symbol','')} {best_t.get('direction','')} @ {best_t.get('entry_time','')}" if best_t else "—"
    worst_str = f"₹{worst_t['pnl_rs']:+,.0f} — {worst_t.get('symbol','')} {worst_t.get('direction','')} @ {worst_t.get('entry_time','')}" if worst_t else "—"

    rows = [
        ("Session Assessment",    day_assessment),
        ("Best Trade",            best_str),
        ("Worst Trade",           worst_str),
        ("Primary Failure Cause", top_cause_text),
        ("Edge Status",           edge_text),
        ("Rule to Test Tomorrow", tomorrow_rule),
        ("Confidence Level",      f"{'HIGH' if n >= 5 and (pf_val or 0) >= 1.5 else 'MEDIUM' if n >= 3 else 'LOW — need more data'}"),
    ]

    rows_html = "".join(
        f'<div class="coach-row"><div class="coach-label">{k}</div><div class="coach-text">{v}</div></div>'
        for k, v in rows
    )

    return f"""
<div class="sec"><div class="sec-hdr">Strategy Coach</div></div>
<div class="coach-section">{rows_html}</div>"""


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="OpenAlgo Research Report")
    parser.add_argument("--date",     type=str)
    parser.add_argument("--strategy", type=str)
    parser.add_argument("--last",     type=int)
    parser.add_argument("--from",     dest="date_from", type=str)
    parser.add_argument("--to",       dest="date_to",   type=str)
    parser.add_argument("--out",      type=str)
    args = parser.parse_args()

    html = build_report(
        date=args.date, date_from=args.date_from, date_to=args.date_to,
        strategy=args.strategy, last_n=args.last,
    )

    # Output path
    if args.out:
        out = Path(args.out)
    else:
        today = datetime.now(IST).strftime("%Y-%m-%d")
        out   = Path.home() / "openalgo" / "PIT" / "reports" / f"report_{today}.html"

    out.write_text(html, encoding="utf-8")
    print(f"Report saved: {out}")
    print(f"Open in browser: open '{out}'")


if __name__ == "__main__":
    main()
