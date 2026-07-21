"""
filter_audit.py — Does each filter earn its place?
===================================================
Reads the master trade CSV written by trade_logger (logs/{strategy}_master.csv)
and answers the only question that matters before adding/removing filters:

    "What is the expectancy of trades WHERE the filter agreed
     vs trades WHERE it conflicted?"

Covers:
  - Overall stats: n, win rate, avg win, avg loss, expectancy, profit factor
  - OI bias       : agreed vs conflicted with trade direction
  - Max pain bias : agreed vs conflicted with trade direction
  - ADX buckets   : <25 / 25-30 / 30-35 / >35  (tests the adx_max=35 cap)
  - Momentum state, exit reason, break-even flag, instrument, entry hour

Every split prints n, win rate, and expectancy so you can see whether a
filter's "conflict" cohort actually loses money. Cohorts with n < 30 are
flagged LOW SAMPLE — do not act on them yet.

Usage:
    uv run python filter_audit.py                 # scalper, default log dir
    uv run python filter_audit.py trend_rider
    uv run python filter_audit.py scalper --csv /path/to/master.csv
"""

import argparse
import csv
import os
from collections import defaultdict
from pathlib import Path

MIN_SAMPLE = 30


def _f(row, key, default=0.0):
    try:
        return float(row.get(key) or default)
    except (TypeError, ValueError):
        return default


def load_trades(csv_path: Path) -> list[dict]:
    if not csv_path.exists():
        raise SystemExit(f"No trade CSV found at {csv_path} — collect trades first.")
    with open(csv_path, newline="") as fh:
        return list(csv.DictReader(fh))


def cohort_stats(trades: list[dict]) -> dict:
    pnls = [_f(t, "pnl_rs") for t in trades]
    n = len(pnls)
    if n == 0:
        return {"n": 0}
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win  = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "n":          n,
        "win_rate":   len(wins) / n * 100,
        "avg_win":    gross_win / len(wins) if wins else 0,
        "avg_loss":   gross_loss / len(losses) if losses else 0,
        "expectancy": sum(pnls) / n,
        "pf":         gross_win / gross_loss if gross_loss > 0 else float("inf"),
        "total":      sum(pnls),
    }


def fmt_stats(s: dict) -> str:
    if s["n"] == 0:
        return "n=0"
    flag = "  ⚠ LOW SAMPLE" if s["n"] < MIN_SAMPLE else ""
    pf = f"{s['pf']:.2f}" if s["pf"] != float("inf") else "inf"
    return (f"n={s['n']:<4} WR={s['win_rate']:5.1f}%  "
            f"Exp=Rs {s['expectancy']:+8.0f}  PF={pf:<5}  "
            f"Total=Rs {s['total']:+8.0f}{flag}")


def split_by(trades: list[dict], label_fn) -> dict[str, list[dict]]:
    groups = defaultdict(list)
    for t in trades:
        groups[label_fn(t)].append(t)
    return groups


def print_split(title: str, groups: dict[str, list[dict]]):
    print(f"\n── {title} " + "─" * max(1, 52 - len(title)))
    for label in sorted(groups, key=lambda k: -len(groups[k])):
        print(f"  {label:<22}: {fmt_stats(cohort_stats(groups[label]))}")


def bias_label(t: dict, bias_col: str) -> str:
    bias = (t.get(bias_col) or "").strip().upper()
    direction = (t.get("direction") or "").strip().upper()
    if bias in ("", "N/A", "NONE", "0"):
        return "no data"
    return "agreed" if bias == direction else "CONFLICTED"


def adx_bucket(t: dict) -> str:
    adx = _f(t, "adx_entry")
    if adx < 25:
        return "ADX < 25"
    if adx < 30:
        return "ADX 25-30"
    if adx <= 35:
        return "ADX 30-35"
    return "ADX > 35"


def entry_hour(t: dict) -> str:
    et = (t.get("entry_time") or "")[:2]
    return f"{et}:00-{et}:59" if et.isdigit() else "unknown"


def print_funnel(funnel_path: Path):
    """Aggregate the per-day funnel rows written by log_funnel_summary."""
    if not funnel_path.exists():
        return
    with open(funnel_path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        return

    days = len({(r["date"], r["instrument"]) for r in rows})
    totals = defaultdict(float)
    num_cols = ["scans", "setup_seen", "taken", "adx_low", "adx_high",
                "confirm_inconsistent", "oi_conflict", "max_pain_conflict",
                "same_dir_block", "entry_failed"]
    for r in rows:
        for c in num_cols:
            totals[c] += _f(r, c)

    seen, taken = totals["setup_seen"], totals["taken"]
    print(f"\n── SIGNAL FUNNEL ({days} instrument-days) " + "─" * 18)
    print(f"  {'Scans':<22}: {totals['scans']:.0f}")
    print(f"  {'Setups Seen':<22}: {seen:.0f}")
    print(f"  {'Trades Taken':<22}: {taken:.0f}"
          + (f"  ({taken / seen * 100:.0f}% of setups)" if seen else ""))
    labels = [
        ("adx_low", "ADX too low"), ("adx_high", "ADX too high"),
        ("confirm_inconsistent", "Confirm failed"),
        ("oi_conflict", "OI conflict (soft)"),
        ("max_pain_conflict", "Max pain conflict (soft)"),
        ("same_dir_block", "Same-dir after SL"),
        ("entry_failed", "Entry/quote failed"),
    ]
    for key, label in labels:
        if totals[key]:
            pct = f"  ({totals[key] / seen * 100:.0f}% of setups)" if seen and key not in ("adx_low", "adx_high") else ""
            print(f"  {label:<22}: {totals[key]:.0f}{pct}")


def main():
    ap = argparse.ArgumentParser(description="Per-filter expectancy audit")
    ap.add_argument("strategy", nargs="?", default="scalper")
    ap.add_argument("--csv", help="explicit path to a master CSV")
    args = ap.parse_args()

    csv_path = (Path(args.csv) if args.csv else
                Path(os.path.expanduser("~/openalgo/logs")) / f"{args.strategy}_master.csv")
    trades = load_trades(csv_path)

    sep = "=" * 64
    print(sep)
    print(f"FILTER AUDIT — {args.strategy} — {csv_path}")
    print(sep)
    print(f"\n  OVERALL              : {fmt_stats(cohort_stats(trades))}")
    overall = cohort_stats(trades)
    if overall["n"] < 50:
        print(f"\n  ⚠ Only {overall['n']} trades. Target 50-100 before acting on any split below.")

    print_split("OI BIAS vs direction", split_by(trades, lambda t: bias_label(t, "oi_bias")))
    print_split("MAX PAIN vs direction", split_by(trades, lambda t: bias_label(t, "max_pain_bias")))
    print_split("ADX AT ENTRY", split_by(trades, adx_bucket))
    print_split("MOMENTUM STATE", split_by(trades, lambda t: t.get("momentum") or "unknown"))
    print_split("EXIT REASON", split_by(trades, lambda t: t.get("exit_reason") or "unknown"))
    print_split("BREAK-EVEN TRIGGERED", split_by(trades, lambda t: t.get("be_triggered") or "unknown"))
    print_split("INSTRUMENT", split_by(trades, lambda t: t.get("instrument") or "unknown"))
    print_split("ENTRY HOUR", split_by(trades, entry_hour))
    print_funnel(csv_path.parent / f"{args.strategy}_funnel.csv")

    # Verdict hints: only for filters with enough sample on both sides
    print(f"\n{sep}\nVERDICT HINTS (need n>={MIN_SAMPLE} on both sides)\n{sep}")
    for col, fname in (("oi_bias", "OI bias"), ("max_pain_bias", "Max pain")):
        g = split_by(trades, lambda t, c=col: bias_label(t, c))
        agreed, conflicted = cohort_stats(g.get("agreed", [])), cohort_stats(g.get("CONFLICTED", []))
        if agreed["n"] >= MIN_SAMPLE and conflicted["n"] >= MIN_SAMPLE:
            delta = agreed["expectancy"] - conflicted["expectancy"]
            verdict = ("filter EARNS its place — conflicts lose money"
                       if delta > 0 and conflicted["expectancy"] < 0
                       else "filter does NOT earn its place yet")
            print(f"  {fname:<10}: agreed Exp Rs {agreed['expectancy']:+.0f} vs "
                  f"conflicted Rs {conflicted['expectancy']:+.0f} → {verdict}")
        else:
            print(f"  {fname:<10}: insufficient sample "
                  f"(agreed n={agreed.get('n', 0)}, conflicted n={conflicted.get('n', 0)})")
    print()


if __name__ == "__main__":
    main()
