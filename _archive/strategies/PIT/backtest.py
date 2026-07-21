"""
backtest.py — SMA 9/21 Crossover Backtesting Engine
=====================================================
Instruments : NIFTY50, SENSEX, BANKNIFTY + all NIFTY50 stocks
Strategy    : SMA 9/21 crossover — long on golden cross, short on death cross
Stop Loss   : Trailing stop (ATR-based, 2x ATR)
Data        : Yahoo Finance, 5 years daily candles
Output      : HTML report + CSV results

Usage:
    python3 backtest.py
    python3 backtest.py --sma-fast 9 --sma-slow 21
    python3 backtest.py --trail-atr 2.0
    python3 backtest.py --instruments NIFTY SENSEX
"""

import argparse
import json
import math
from datetime import datetime, timedelta
from pathlib import Path
import sys

import pandas as pd
import numpy as np

try:
    import yfinance as yf
except ImportError:
    print("Install yfinance: pip install yfinance")
    sys.exit(1)


# ── Instrument universe ───────────────────────────────────────────────────────

INDICES = {
    "NIFTY":     "^NSEI",
    "SENSEX":    "^BSESN",
    "BANKNIFTY": "NIFTYBEES.NS",  # BANKNIFTY proxy via ETF
}

NIFTY50_STOCKS = {
    "RELIANCE":    "RELIANCE.NS",  "TCS":         "TCS.NS",
    "HDFCBANK":    "HDFCBANK.NS",  "BHARTIARTL":  "BHARTIARTL.NS",
    "ICICIBANK":   "ICICIBANK.NS", "INFOSYS":     "INFY.NS",
    "SBIN":        "SBIN.NS",      "HINDUNILVR":  "HINDUNILVR.NS",
    "ITC":         "ITC.NS",       "KOTAKBANK":   "KOTAKBANK.NS",
    "LT":          "LT.NS",        "AXISBANK":    "AXISBANK.NS",
    "BAJFINANCE":  "BAJFINANCE.NS","ASIANPAINT":  "ASIANPAINT.NS",
    "MARUTI":      "MARUTI.NS",    "HCLTECH":     "HCLTECH.NS",
    "SUNPHARMA":   "SUNPHARMA.NS", "TITAN":       "TITAN.NS",
    "ULTRACEMCO":  "ULTRACEMCO.NS","NTPC":        "NTPC.NS",
    "WIPRO":       "WIPRO.NS",     "POWERGRID":   "POWERGRID.NS",
    "BAJAJFINSV":  "BAJAJFINSV.NS","TECHM":       "TECHM.NS",
    "NESTLEIND":   "NESTLEIND.NS", "ADANIENT":    "ADANIENT.NS",
    "ADANIPORTS":  "ADANIPORTS.NS","TATAMOTORS":  "TATAMOTORS.NS",
    "TATASTEEL":   "TATASTEEL.NS", "JSWSTEEL":    "JSWSTEEL.NS",
    "HINDALCO":    "HINDALCO.NS",  "COALINDIA":   "COALINDIA.NS",
    "ONGC":        "ONGC.NS",      "BPCL":        "BPCL.NS",
    "IOC":         "IOC.NS",       "GRASIM":      "GRASIM.NS",
    "DIVISLAB":    "DIVISLAB.NS",  "CIPLA":       "CIPLA.NS",
    "DRREDDY":     "DRREDDY.NS",   "APOLLOHOSP":  "APOLLOHOSP.NS",
    "EICHERMOT":   "EICHERMOT.NS", "HEROMOTOCO":  "HEROMOTOCO.NS",
    "BAJAJ-AUTO":  "BAJAJ-AUTO.NS","M&M":         "M&M.NS",
    "TATACONSUM":  "TATACONSUM.NS","BRITANNIA":   "BRITANNIA.NS",
    "SHRIRAMFIN":  "SHRIRAMFIN.NS","BEL":         "BEL.NS",
    "INDUSINDBK":  "INDUSINDBK.NS","HDFCLIFE":    "HDFCLIFE.NS",
}

ALL_INSTRUMENTS = {**INDICES, **NIFTY50_STOCKS}


# ── Data fetcher ──────────────────────────────────────────────────────────────

def fetch_data(ticker: str, years: int = 5) -> pd.DataFrame:
    end   = datetime.today()
    start = end - timedelta(days=years * 365)
    try:
        df = yf.download(ticker, start=start.strftime("%Y-%m-%d"),
                         end=end.strftime("%Y-%m-%d"),
                         progress=False, auto_adjust=True)
        if df.empty:
            return None
        # Flatten MultiIndex columns if present
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df[["Open","High","Low","Close","Volume"]].copy()
        df.dropna(inplace=True)
        return df
    except Exception as e:
        return None


# ── Indicators ────────────────────────────────────────────────────────────────

def add_indicators(df: pd.DataFrame, fast: int, slow: int) -> pd.DataFrame:
    df = df.copy()
    df["SMA_fast"]  = df["Close"].rolling(fast).mean()
    df["SMA_slow"]  = df["Close"].rolling(slow).mean()

    # ATR for trailing stop
    hl  = df["High"] - df["Low"]
    hc  = (df["High"] - df["Close"].shift()).abs()
    lc  = (df["Low"]  - df["Close"].shift()).abs()
    tr  = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    df["ATR"] = tr.rolling(14).mean()

    # Crossover signals
    df["cross"] = 0
    df.loc[(df["SMA_fast"] > df["SMA_slow"]) &
           (df["SMA_fast"].shift() <= df["SMA_slow"].shift()), "cross"] =  1   # golden
    df.loc[(df["SMA_fast"] < df["SMA_slow"]) &
           (df["SMA_fast"].shift() >= df["SMA_slow"].shift()), "cross"] = -1   # death

    return df.dropna()


# ── Backtesting engine ────────────────────────────────────────────────────────

def backtest(df: pd.DataFrame, trail_atr: float = 2.0,
             capital: float = 100_000) -> dict:
    """
    Simulate SMA 9/21 crossover with trailing ATR stop.
    Long on golden cross, short on death cross.
    Returns full trade list + equity curve.
    """
    trades      = []
    equity      = [capital]
    cash        = capital
    position    = 0          # 0 = flat, 1 = long, -1 = short
    entry_price = 0.0
    trail_stop  = 0.0
    entry_date  = None
    shares      = 0
    high_water  = 0.0
    low_water   = 0.0
    direction   = 0



    for i, row in df2.iterrows():
        price = float(row["Close"])
        atr   = float(row["ATR"])
        cross = int(row["cross"])
        date  = str(row.index if "Date" not in row else row.get("Date", row.name))
        try:
            idx = row.name
            if hasattr(idx, 'strftime'):
                date = idx.strftime("%Y-%m-%d")
            elif isinstance(idx, str):
                date = idx[:10]
            else:
                date = pd.Timestamp(idx).strftime("%Y-%m-%d")
        except Exception:
            date = str(i)

        # ── Manage open position ──────────────────────────────
        if position != 0:
            # Update trailing stop
            if position == 1:
                # Long: trail stop moves up
                new_stop = price - trail_atr * atr
                if new_stop > trail_stop:
                    trail_stop = new_stop
                high_water = max(high_water, price)
                # Exit if price hits trail stop or death cross
                if price <= trail_stop or cross == -1:
                    pnl    = (price - entry_price) * shares
                    cash  += price * shares
                    reason = "Trailing Stop" if price <= trail_stop else "Death Cross"
                    trades.append(_make_trade(
                        entry_date, date, "LONG", entry_price, price,
                        shares, pnl, high_water, low_water, reason
                    ))
                    position = 0; shares = 0

            elif position == -1:
                # Short: trail stop moves down
                new_stop = price + trail_atr * atr
                if new_stop < trail_stop:
                    trail_stop = new_stop
                low_water = min(low_water, price)
                # Exit if price hits trail stop or golden cross
                if price >= trail_stop or cross == 1:
                    pnl    = (entry_price - price) * shares
                    cash  += entry_price * shares  # return notional
                    reason = "Trailing Stop" if price >= trail_stop else "Golden Cross"
                    trades.append(_make_trade(
                        entry_date, date, "SHORT", entry_price, price,
                        shares, pnl, high_water, low_water, reason
                    ))
                    position = 0; shares = 0

        # ── Open new position on crossover ────────────────────
        if position == 0 and cross != 0:
            shares      = max(1, int(cash * 0.95 / price))  # 95% of capital
            entry_price = price
            entry_date  = date
            high_water  = price
            low_water   = price

            if cross == 1:   # Golden cross → long
                position   = 1
                trail_stop = price - trail_atr * atr
                cash      -= shares * price
            elif cross == -1:  # Death cross → short
                position   = -1
                trail_stop = price + trail_atr * atr
                # Short: reserve capital as margin
                cash      -= shares * price  # notional

        # Track equity
        if position == 1:
            equity.append(cash + shares * price)
        elif position == -1:
            equity.append(cash + shares * entry_price + (entry_price - price) * shares)
        else:
            equity.append(cash)

    # Close any open position at end
    if position != 0 and shares > 0:
        last_price = float(df2.iloc[-1]["Close"])
        last_date  = df2.iloc[-1][date_col]
        try:
            last_date = pd.Timestamp(last_date).strftime("%Y-%m-%d")
        except Exception:
            last_date = str(last_date)

        if position == 1:
            pnl = (last_price - entry_price) * shares
            trades.append(_make_trade(
                entry_date, last_date, "LONG", entry_price, last_price,
                shares, pnl, high_water, low_water, "End of Period"
            ))
        else:
            pnl = (entry_price - last_price) * shares
            trades.append(_make_trade(
                entry_date, last_date, "SHORT", entry_price, last_price,
                shares, pnl, high_water, low_water, "End of Period"
            ))

    # ── Compute stats ─────────────────────────────────────────
    final_equity = equity[-1] if equity else capital
    total_return = (final_equity - capital) / capital * 100

    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gp     = sum(t["pnl"] for t in wins)
    gl     = abs(sum(t["pnl"] for t in losses))
    pf     = gp / gl if gl > 0 else float("inf")
    wr     = len(wins) / len(trades) * 100 if trades else 0
    avg_w  = gp / len(wins) if wins else 0
    avg_l  = gl / len(losses) if losses else 0
    exp    = (wr/100 * avg_w) - ((1 - wr/100) * avg_l)

    # Max drawdown
    peak = capital; max_dd = 0
    for e in equity:
        if e > peak: peak = e
        dd = (peak - e) / peak * 100
        if dd > max_dd: max_dd = dd

    # Sharpe proxy (annualised daily returns)
    if len(equity) > 2:
        daily_rets = pd.Series(equity).pct_change().dropna()
        sharpe = (daily_rets.mean() / daily_rets.std() * math.sqrt(252)
                  if daily_rets.std() > 0 else 0)
    else:
        sharpe = 0

    return {
        "trades":        trades,
        "equity":        equity,
        "capital":       capital,
        "final_equity":  final_equity,
        "total_return":  total_return,
        "n":             len(trades),
        "wins":          len(wins),
        "losses":        len(losses),
        "win_rate":      wr,
        "profit_factor": pf,
        "expectancy":    exp,
        "avg_winner":    avg_w,
        "avg_loser":     avg_l,
        "max_drawdown":  max_dd,
        "sharpe":        sharpe,
        "gross_profit":  gp,
        "gross_loss":    gl,
        "net_pnl":       final_equity - capital,
    }


def _make_trade(entry_date, exit_date, direction,
                entry_price, exit_price, shares, pnl,
                high_water, low_water, reason):
    mfe = (high_water - entry_price if direction == "LONG"
           else entry_price - low_water)
    mae = (entry_price - low_water if direction == "LONG"
           else high_water - entry_price)
    hold = 0
    try:
        h = (datetime.strptime(exit_date, "%Y-%m-%d") -
             datetime.strptime(entry_date, "%Y-%m-%d")).days
        hold = max(0, h)
    except Exception:
        pass

    return {
        "entry_date":  entry_date,
        "exit_date":   exit_date,
        "direction":   direction,
        "entry_price": round(entry_price, 2),
        "exit_price":  round(exit_price, 2),
        "shares":      shares,
        "pnl":         round(pnl, 2),
        "pnl_pct":     round((exit_price - entry_price) / entry_price * 100
                             * (1 if direction == "LONG" else -1), 2),
        "mfe":         round(mfe, 2),
        "mae":         round(mae, 2),
        "hold_days":   hold,
        "exit_reason": reason,
    }


# ── HTML report ───────────────────────────────────────────────────────────────

def build_html(results: dict, params: dict) -> str:
    fast    = params["fast"]
    slow    = params["slow"]
    trail   = params["trail_atr"]
    capital = params["capital"]
    period  = params["period"]

    # Sort instruments by total return
    sorted_insts = sorted([(k,v) for k,v in results.items() if "error" not in v],
                          key=lambda x: x[1].get("total_return", -999),
                          reverse=True)

    # Summary cards data
    all_trades = [t for r in results.values() if "error" not in r for t in r.get("trades", [])]
    all_wins   = [t for t in all_trades if t["pnl"] > 0]
    all_losses = [t for t in all_trades if t["pnl"] <= 0]
    valid_results = {k:v for k,v in results.items() if "error" not in v}
    avg_wr     = (sum(r["win_rate"] for r in valid_results.values()) /
                  len(valid_results) if valid_results else 0)
    best_inst  = sorted_insts[0][0] if sorted_insts else "—"
    worst_inst = sorted_insts[-1][0] if sorted_insts else "—"
    best_ret   = sorted_insts[0][1]["total_return"] if sorted_insts else 0
    worst_ret  = sorted_insts[-1][1]["total_return"] if sorted_insts else 0

    # Per-instrument table rows
    inst_rows = ""
    for name, r in sorted_insts:
        if "error" in r:
            inst_rows += f'<tr><td class="td-hi">{name}</td><td colspan="9" class="td-dim">{r["error"]}</td></tr>'
            continue
        ret_cls = "td-pos" if r["total_return"] >= 0 else "td-neg"
        pf_cls  = "td-pos" if r["profit_factor"] >= 1 else "td-neg"
        wr_cls  = "td-pos" if r["win_rate"] >= 50 else "td-neg"
        pf_str  = f'{r["profit_factor"]:.2f}' if r["profit_factor"] != float("inf") else "∞"
        inst_rows += f"""<tr>
          <td class="td-hi">{name}</td>
          <td>{r['n']}</td>
          <td class="{wr_cls}">{r['win_rate']:.1f}%</td>
          <td class="{pf_cls}">{pf_str}</td>
          <td class="{ret_cls}">{'+'if r['total_return']>=0 else ''}{r['total_return']:.1f}%</td>
          <td class="td-neg">-{r['max_drawdown']:.1f}%</td>
          <td>{r['sharpe']:.2f}</td>
          <td class="{'td-pos'if r['expectancy']>=0 else 'td-neg'}">₹{r['expectancy']:,.0f}</td>
          <td class="td-pos">₹{r['gross_profit']:,.0f}</td>
          <td class="td-neg">-₹{r['gross_loss']:,.0f}</td>
        </tr>"""

    # Trade log (all instruments combined, sortable)
    all_trade_rows = ""
    for name, r in results.items():
        if "error" in r: continue
        for t in r.get("trades", []):
            pnl_cls = "td-pos" if t["pnl"] >= 0 else "td-neg"
            dir_cls = "pill-buy" if t["direction"] == "LONG" else "pill-pe"
            all_trade_rows += f"""<tr>
              <td class="td-dim">{t['entry_date']}</td>
              <td class="td-dim">{t['exit_date']}</td>
              <td class="td-hi">{name}</td>
              <td><span class="pill {dir_cls}">{t['direction']}</span></td>
              <td>{t['entry_price']:,.2f}</td>
              <td>{t['exit_price']:,.2f}</td>
              <td>{t['shares']}</td>
              <td class="{pnl_cls}">{'+'if t['pnl']>=0 else ''}₹{t['pnl']:,.0f}</td>
              <td class="{pnl_cls}">{'+'if t['pnl_pct']>=0 else ''}{t['pnl_pct']:.2f}%</td>
              <td>{t['hold_days']}d</td>
              <td class="td-dim">{t['exit_reason']}</td>
            </tr>"""

    # Equity curves data for JS
    equity_data = {}
    for name, r in results.items():
        if "error" not in r:
            equity_data[name] = r["equity"][::5]  # sample every 5 points

    # Best/worst trade
    best_trade  = max(all_trades, key=lambda t: t["pnl"]) if all_trades else None
    worst_trade = min(all_trades, key=lambda t: t["pnl"]) if all_trades else None

    # ADX not available in backtest — analyse by hold days
    avg_hold = (sum(t["hold_days"] for t in all_trades) /
                len(all_trades) if all_trades else 0)
    avg_mfe  = (sum(t["mfe"] for t in all_trades) /
                len(all_trades) if all_trades else 0)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>SMA Crossover Backtest — {fast}/{slow}</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@300;400;500;600;700&display=swap');
  :root {{
    --bg:#0a0c0e;--bg1:#0d1014;--bg2:#111518;--bg3:#161b1f;
    --border:#1e2428;--border2:#252c32;--muted:#5a6a78;
    --text:#c8d4dc;--text-hi:#e8f0f4;--label:#7a8c98;
    --green:#00c87a;--red:#e8374a;--yellow:#e8b84a;--cyan:#4ab8d8;
    --mono:'IBM Plex Mono',monospace;
  }}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--text);font-family:var(--mono);font-size:12px;line-height:1.5}}
  .hdr{{background:var(--bg1);border-bottom:1px solid var(--border2);padding:10px 24px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100}}
  .logo{{font-size:13px;font-weight:700;letter-spacing:.12em;color:var(--text-hi)}}
  .logo span{{color:var(--cyan)}}
  .hdr-meta{{font-size:11px;color:var(--muted)}}
  .page{{max-width:1400px;margin:0 auto;padding:16px 24px 64px}}
  .date-bar{{display:flex;align-items:center;justify-content:space-between;padding:12px 0;border-bottom:1px solid var(--border);margin-bottom:16px}}
  .date-label{{font-size:18px;font-weight:600;color:var(--text-hi)}}
  .sec{{margin-top:20px;margin-bottom:8px}}
  .sec-hdr{{font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);padding-bottom:6px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px}}
  .sec-hdr::before{{content:'▪';color:var(--cyan)}}
  .sg{{display:grid;grid-template-columns:repeat(8,1fr);gap:1px;background:var(--border);border:1px solid var(--border);margin-top:8px}}
  .sc{{background:var(--bg2);padding:12px 14px}}
  .sc:hover{{background:var(--bg3)}}
  .sc-lbl{{font-size:9px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin-bottom:6px}}
  .sc-val{{font-size:18px;font-weight:600;color:var(--text-hi);line-height:1.2}}
  .sc-val.pos{{color:var(--green)}} .sc-val.neg{{color:var(--red)}} .sc-val.warn{{color:var(--yellow)}}
  .sc-sub{{font-size:9px;color:var(--muted);margin-top:3px}}
  .tbl{{width:100%;border-collapse:collapse;font-size:11px}}
  .tbl th{{background:var(--bg1);color:var(--muted);font-size:9px;letter-spacing:.08em;text-transform:uppercase;padding:6px 10px;text-align:left;border-bottom:1px solid var(--border2);cursor:pointer}}
  .tbl th:hover{{color:var(--cyan)}}
  .tbl th.sort-asc::after{{content:' ↑';color:var(--cyan)}}
  .tbl th.sort-desc::after{{content:' ↓';color:var(--cyan)}}
  .tbl td{{padding:5px 10px;border-bottom:1px solid var(--border)}}
  .tbl tr:hover td{{background:var(--bg3)}}
  .td-pos{{color:var(--green)}} .td-neg{{color:var(--red)}} .td-warn{{color:var(--yellow)}} .td-dim{{color:var(--muted)}} .td-hi{{color:var(--text-hi)}}
  .pill{{font-size:9px;padding:2px 7px;font-weight:600;letter-spacing:.06em}}
  .pill-buy{{background:rgba(0,200,122,.15);color:var(--green);border:1px solid rgba(0,200,122,.3)}}
  .pill-pe{{background:rgba(232,55,74,.15);color:var(--red);border:1px solid rgba(232,55,74,.3)}}
  .canvas-wrap{{background:var(--bg2);border:1px solid var(--border);padding:16px;margin-top:8px}}
  .filter-bar{{background:var(--bg2);border:1px solid var(--border);border-bottom:none;padding:10px 14px;display:flex;flex-wrap:wrap;gap:8px}}
  select,input{{background:var(--bg3);color:var(--text);border:1px solid var(--border2);padding:4px 8px;font-family:var(--mono);font-size:11px}}
  button{{background:var(--bg3);color:var(--muted);border:1px solid var(--border2);padding:4px 12px;font-family:var(--mono);font-size:11px;cursor:pointer}}
  button:hover{{color:var(--text-hi)}}
  .two-col{{display:grid;grid-template-columns:1fr 1fr;gap:1px;margin-top:8px}}
  .panel{{background:var(--bg2);border:1px solid var(--border);padding:14px}}
  .stat-row{{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid var(--border)}}
  .stat-row:last-child{{border-bottom:none}}
  .stat-key{{color:var(--muted);font-size:11px}}
  .stat-val{{font-size:11px;color:var(--text-hi)}}
  .stat-val.pos{{color:var(--green)}} .stat-val.neg{{color:var(--red)}}
  .best-tag{{display:inline-block;font-size:9px;padding:2px 8px;font-weight:700;letter-spacing:.06em;background:rgba(0,200,122,.15);color:var(--green);border:1px solid rgba(0,200,122,.3)}}
  .worst-tag{{display:inline-block;font-size:9px;padding:2px 8px;font-weight:700;letter-spacing:.06em;background:rgba(232,55,74,.15);color:var(--red);border:1px solid rgba(232,55,74,.3)}}
  .live-bar{{display:flex;align-items:center;gap:12px;margin-left:auto;font-size:11px}}
</style>
</head>
<body>

<div class="hdr">
  <div class="logo">OPEN<span>ALGO</span> &nbsp;·&nbsp; Backtest Engine</div>
  <div class="hdr-meta">SMA {fast}/{slow} &nbsp;·&nbsp; {period}Y Daily &nbsp;·&nbsp; Trail ATR×{trail} &nbsp;·&nbsp; Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}</div>
</div>

<div class="page">

<div class="date-bar">
  <div>
    <div class="date-label">SMA {fast}/{slow} Crossover Backtest</div>
    <div style="font-size:11px;color:var(--muted);margin-top:4px">
      {len(results)} instruments &nbsp;·&nbsp; {period} years daily &nbsp;·&nbsp;
      Capital ₹{capital:,.0f} per instrument &nbsp;·&nbsp;
      Long + Short &nbsp;·&nbsp; Trailing ATR×{trail}
    </div>
  </div>
  <div style="display:flex;gap:20px;text-align:center">
    <div><div style="font-size:9px;color:var(--muted);letter-spacing:.1em;text-transform:uppercase">Best</div>
      <div style="font-size:14px;font-weight:700;color:var(--green)">{best_inst} +{best_ret:.1f}%</div></div>
    <div><div style="font-size:9px;color:var(--muted);letter-spacing:.1em;text-transform:uppercase">Worst</div>
      <div style="font-size:14px;font-weight:700;color:var(--red)">{worst_inst} {worst_ret:.1f}%</div></div>
  </div>
</div>

<!-- SUMMARY CARDS -->
<div class="sec"><div class="sec-hdr">Portfolio Summary</div></div>
<div class="sg">
  <div class="sc"><div class="sc-lbl">Total Trades</div>
    <div class="sc-val">{len(all_trades)}</div>
    <div class="sc-sub">across {len(results)} instruments</div></div>
  <div class="sc"><div class="sc-lbl">Avg Win Rate</div>
    <div class="sc-val {'pos' if avg_wr>=50 else 'neg'}">{avg_wr:.1f}%</div>
    <div class="sc-sub">{len(all_wins)}W / {len(all_losses)}L total</div></div>
  <div class="sc"><div class="sc-lbl">Best Trade</div>
    <div class="sc-val pos">+₹{best_trade['pnl']:,.0f}</div>
    <div class="sc-sub">{best_trade['entry_date'] if best_trade else '—'}</div></div>
  <div class="sc"><div class="sc-lbl">Worst Trade</div>
    <div class="sc-val neg">₹{worst_trade['pnl']:,.0f}</div>
    <div class="sc-sub">{worst_trade['entry_date'] if worst_trade else '—'}</div></div>
  <div class="sc"><div class="sc-lbl">Avg Hold</div>
    <div class="sc-val">{avg_hold:.0f}d</div>
    <div class="sc-sub">per trade</div></div>
  <div class="sc"><div class="sc-lbl">Avg MFE</div>
    <div class="sc-val">{avg_mfe:.1f}</div>
    <div class="sc-sub">pts per trade</div></div>
  <div class="sc"><div class="sc-lbl">Capital/Inst</div>
    <div class="sc-val">₹{capital/1000:.0f}K</div>
    <div class="sc-sub">starting capital</div></div>
  <div class="sc"><div class="sc-lbl">Strategy</div>
    <div class="sc-val" style="font-size:14px">SMA {fast}/{slow}</div>
    <div class="sc-sub">Long + Short</div></div>
</div>

<!-- INSTRUMENT LEADERBOARD -->
<div class="sec"><div class="sec-hdr">Instrument Leaderboard — sorted by return</div></div>
<div style="background:var(--bg2);border:1px solid var(--border);overflow-x:auto">
  <table class="tbl" id="inst-tbl">
    <thead><tr>
      <th data-col="name">Instrument</th>
      <th data-col="n">Trades</th>
      <th data-col="win_rate">WR%</th>
      <th data-col="profit_factor">PF</th>
      <th data-col="total_return">Return%</th>
      <th data-col="max_drawdown">MaxDD%</th>
      <th data-col="sharpe">Sharpe</th>
      <th data-col="expectancy">Expectancy</th>
      <th data-col="gross_profit">Gross Profit</th>
      <th data-col="gross_loss">Gross Loss</th>
    </tr></thead>
    <tbody>{inst_rows}</tbody>
  </table>
</div>

<!-- EQUITY CURVES -->
<div class="sec"><div class="sec-hdr">Equity Curves — NIFTY / SENSEX / BANKNIFTY</div></div>
<div class="canvas-wrap"><canvas id="eq-canvas" height="200"></canvas></div>

<!-- TRADE LOG -->
<div class="sec"><div class="sec-hdr">Trade Log
  <div class="live-bar">
    <span id="live-n" style="color:var(--muted)"></span>
    <span id="live-wr"></span>
    <span id="live-net"></span>
  </div>
</div></div>
<div class="filter-bar">
  <select id="f-inst" onchange="filterTrades()"><option value="">All Instruments</option></select>
  <select id="f-dir" onchange="filterTrades()">
    <option value="">Long + Short</option>
    <option value="LONG">Long Only</option>
    <option value="SHORT">Short Only</option>
  </select>
  <select id="f-exit" onchange="filterTrades()"><option value="">All Exit Reasons</option></select>
  <select id="f-result" onchange="filterTrades()">
    <option value="">Win + Loss</option>
    <option value="win">Wins Only</option>
    <option value="loss">Losses Only</option>
  </select>
  <input id="f-year" oninput="filterTrades()" placeholder="Year e.g. 2022" style="width:120px">
  <button onclick="resetTrades()">RESET</button>
</div>
<div style="background:var(--bg2);border:1px solid var(--border);overflow-x:auto">
  <table class="tbl">
    <thead><tr>
      <th>Entry Date</th><th>Exit Date</th><th>Instrument</th><th>Dir</th>
      <th>Entry ₹</th><th>Exit ₹</th><th>Qty</th>
      <th data-col="pnl">P&L ₹</th><th>P&L%</th>
      <th>Hold</th><th>Exit Reason</th>
    </tr></thead>
    <tbody id="trade-tbody"></tbody>
  </table>
</div>

</div>

<script>
const EQUITY_DATA = {json.dumps({k: v for k,v in list(equity_data.items())[:5]}, default=str)};
const CAPITAL     = {capital};
const ALL_TRADES  = {json.dumps([
    dict(inst=name, **t)
    for name, r in results.items()
    if "error" not in r
    for t in r.get("trades", [])
], default=str)};

// Equity curve for indices
(function(){{
  const canvas = document.getElementById('eq-canvas');
  if(!canvas) return;
  const W = canvas.parentElement.offsetWidth - 32;
  canvas.width = W; const H = 200;
  const ctx = canvas.getContext('2d');
  const colors = ['#00c87a','#4ab8d8','#e8b84a','#e88040','#c87ace'];
  const keys = Object.keys(EQUITY_DATA).slice(0,5);
  const pad = {{t:10,r:120,b:20,l:80}};

  // Find global min/max
  let allVals = [CAPITAL];
  keys.forEach(k=>allVals=allVals.concat(EQUITY_DATA[k]));
  const minY=Math.min(...allVals), maxY=Math.max(...allVals), rY=maxY-minY||1;
  const n = Math.max(...keys.map(k=>EQUITY_DATA[k].length));

  const toX = i => pad.l + (i/(n-1||1))*(W-pad.l-pad.r);
  const toY = v => pad.t + (1-(v-minY)/rY)*(H-pad.t-pad.b);

  // Grid
  ctx.strokeStyle='#1e2428'; ctx.lineWidth=1;
  for(let g=0;g<=4;g++){{
    const y=pad.t+g*(H-pad.t-pad.b)/4;
    ctx.beginPath();ctx.moveTo(pad.l,y);ctx.lineTo(W-pad.r,y);ctx.stroke();
    const val=maxY-g*rY/4;
    ctx.fillStyle='#5a6a78';ctx.font='9px IBM Plex Mono';ctx.textAlign='right';
    ctx.fillText('₹'+Math.round(val/1000)+'K',pad.l-4,y+3);
  }}

  // Zero/capital line
  ctx.beginPath();ctx.moveTo(pad.l,toY(CAPITAL));ctx.lineTo(W-pad.r,toY(CAPITAL));
  ctx.strokeStyle='#3a4450';ctx.lineWidth=1;ctx.setLineDash([4,4]);ctx.stroke();ctx.setLineDash([]);

  // Equity lines
  keys.forEach((k,ci)=>{{
    const pts = EQUITY_DATA[k];
    ctx.beginPath();
    pts.forEach((v,i)=>{{
      if(i===0) ctx.moveTo(toX(i*(n/pts.length)),toY(v));
      else ctx.lineTo(toX(i*(n/pts.length)),toY(v));
    }});
    ctx.strokeStyle=colors[ci];ctx.lineWidth=1.5;ctx.stroke();
    // Label
    const last=pts[pts.length-1];
    ctx.fillStyle=colors[ci];ctx.font='10px IBM Plex Mono';ctx.textAlign='left';
    ctx.fillText(k+' ₹'+Math.round(last/1000)+'K',W-pad.r+6,toY(last)+4);
  }});
}})();

// Trade table filter
let filteredTrades = [...ALL_TRADES];
let tradeSortCol=null, tradeSortDir=1;

function populateTrades(){{
  const insts=[...new Set(ALL_TRADES.map(t=>t.inst))].sort();
  const exits=[...new Set(ALL_TRADES.map(t=>t.exit_reason))].sort();
  const si=document.getElementById('f-inst');
  const se=document.getElementById('f-exit');
  insts.forEach(v=>{{const o=document.createElement('option');o.value=v;o.text=v;si.appendChild(o);}});
  exits.forEach(v=>{{const o=document.createElement('option');o.value=v;o.text=v;se.appendChild(o);}});
}}

function filterTrades(){{
  const inst=document.getElementById('f-inst').value;
  const dir=document.getElementById('f-dir').value;
  const exit=document.getElementById('f-exit').value;
  const result=document.getElementById('f-result').value;
  const year=document.getElementById('f-year').value;
  filteredTrades=ALL_TRADES.filter(t=>{{
    if(inst&&t.inst!==inst) return false;
    if(dir&&t.direction!==dir) return false;
    if(exit&&t.exit_reason!==exit) return false;
    if(result==='win'&&t.pnl<=0) return false;
    if(result==='loss'&&t.pnl>0) return false;
    if(year&&!t.entry_date.startsWith(year)) return false;
    return true;
  }});
  renderTrades(); updateTradeBar();
}}

function resetTrades(){{
  ['f-inst','f-dir','f-exit','f-result'].forEach(id=>document.getElementById(id).value='');
  document.getElementById('f-year').value='';
  filteredTrades=[...ALL_TRADES]; renderTrades(); updateTradeBar();
}}

function renderTrades(){{
  const tbody=document.getElementById('trade-tbody');
  tbody.innerHTML='';
  if(!filteredTrades.length){{
    tbody.innerHTML='<tr><td colspan="11" style="text-align:center;color:var(--muted);padding:20px">No trades match filters</td></tr>';
    return;
  }}
  filteredTrades.slice(0,500).forEach(t=>{{
    const pc=t.pnl>=0?'td-pos':'td-neg';
    const dc=t.direction==='LONG'?'pill-buy':'pill-pe';
    tbody.innerHTML+=`<tr>
      <td class="td-dim">${{t.entry_date}}</td>
      <td class="td-dim">${{t.exit_date}}</td>
      <td class="td-hi">${{t.inst}}</td>
      <td><span class="pill ${{dc}}">${{t.direction}}</span></td>
      <td>${{t.entry_price.toLocaleString('en-IN')}}</td>
      <td>${{t.exit_price.toLocaleString('en-IN')}}</td>
      <td class="td-dim">${{t.shares}}</td>
      <td class="${{pc}}">${{t.pnl>=0?'+':''}}₹${{Math.abs(t.pnl).toLocaleString('en-IN')}}</td>
      <td class="${{pc}}">${{t.pnl_pct>=0?'+':''}}${{t.pnl_pct}}%</td>
      <td class="td-dim">${{t.hold_days}}d</td>
      <td class="td-dim">${{t.exit_reason}}</td>
    </tr>`;
  }});
  if(filteredTrades.length>500){{
    tbody.innerHTML+=`<tr><td colspan="11" style="text-align:center;color:var(--muted);padding:8px">Showing 500 of ${{filteredTrades.length}} trades</td></tr>`;
  }}
}}

function updateTradeBar(){{
  const n=filteredTrades.length;
  const wins=filteredTrades.filter(t=>t.pnl>0).length;
  const net=filteredTrades.reduce((s,t)=>s+t.pnl,0);
  const wr=n>0?(wins/n*100).toFixed(1):0;
  document.getElementById('live-n').textContent=n+' trades';
  const wre=document.getElementById('live-wr');wre.textContent=wr+'%';
  wre.style.color=parseFloat(wr)>=50?'var(--green)':'var(--red)';
  const ne=document.getElementById('live-net');
  ne.textContent=(net>=0?'+':'')+'₹'+Math.abs(Math.round(net)).toLocaleString('en-IN');
  ne.style.color=net>=0?'var(--green)':'var(--red)';
}}

populateTrades(); renderTrades(); updateTradeBar();
</script>
</body>
</html>"""


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="SMA Crossover Backtest")
    parser.add_argument("--sma-fast",    type=int,   default=9)
    parser.add_argument("--sma-slow",    type=int,   default=21)
    parser.add_argument("--trail-atr",   type=float, default=2.0)
    parser.add_argument("--capital",     type=float, default=100_000)
    parser.add_argument("--years",       type=int,   default=5)
    parser.add_argument("--instruments", nargs="+",  default=None)
    parser.add_argument("--out",         type=str,   default=None)
    args = parser.parse_args()

    # Instrument selection
    if args.instruments:
        universe = {k: v for k, v in ALL_INSTRUMENTS.items()
                    if k in args.instruments}
    else:
        universe = ALL_INSTRUMENTS

    print(f"\nSMA {args.sma_fast}/{args.sma_slow} Crossover Backtest")
    print(f"Instruments: {len(universe)} | Years: {args.years} | Capital: ₹{args.capital:,.0f}")
    print(f"Trail ATR×{args.trail_atr} | Long + Short\n")

    results = {}
    total   = len(universe)

    for i, (name, ticker) in enumerate(universe.items(), 1):
        print(f"[{i:>2}/{total}] {name:<15} ({ticker}) ... ", end="", flush=True)
        df = fetch_data(ticker, args.years)
        if df is None or len(df) < args.sma_slow + 20:
            print("NO DATA")
            results[name] = {"error": "Insufficient data"}
            continue

        df  = add_indicators(df, args.sma_fast, args.sma_slow)
        res = backtest(df, args.trail_atr, args.capital)
        results[name] = res

        ret_str = f"{res['total_return']:+.1f}%"
        print(f"{res['n']:>3} trades | WR:{res['win_rate']:.0f}% | "
              f"PF:{res['profit_factor']:.2f} | "
              f"Return:{ret_str} | DD:{res['max_drawdown']:.1f}%")

    # Save CSV
    csv_rows = []
    for name, r in results.items():
        if "error" in r: continue
        for t in r["trades"]:
            csv_rows.append({"instrument": name, **t})

    csv_path = Path.home() / "openalgo" / "PIT" / "backtest_trades.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(csv_rows).to_csv(csv_path, index=False)
    print(f"\nTrade CSV saved: {csv_path}")

    # Save HTML
    params = {"fast": args.sma_fast, "slow": args.sma_slow,
              "trail_atr": args.trail_atr, "capital": args.capital,
              "period": args.years}
    html = build_html(results, params)
    out_path = Path(args.out) if args.out else \
               Path.home() / "openalgo" / "PIT" / "backtest_report.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"HTML report saved: {out_path}")
    print(f"\nOpen: open '{out_path}'")

    # Print leaderboard
    print("\n" + "="*70)
    print(f"{'INSTRUMENT':<15} {'TRADES':>6} {'WR%':>6} {'PF':>6} {'RETURN%':>9} {'SHARPE':>7} {'MAXDD%':>7}")
    print("="*70)
    sorted_r = sorted(results.items(),
                      key=lambda x: x[1].get("total_return", -999), reverse=True)
    for name, r in sorted_r:
        if "error" in r:
            print(f"  {name:<15} NO DATA")
            continue
        pf_s = f'{r["profit_factor"]:.2f}' if r["profit_factor"] != float("inf") else "∞"
        print(f"  {name:<15} {r['n']:>6} {r['win_rate']:>5.1f}% {pf_s:>6} "
              f"{r['total_return']:>+8.1f}% {r['sharpe']:>7.2f} {r['max_drawdown']:>6.1f}%")
    print("="*70)


if __name__ == "__main__":
    main()
