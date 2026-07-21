"""
sage_runner.py — SAGE CLI Orchestrator
=======================================
Single entry point for all SAGE operations.

Commands:
    uv run python3 sage_runner.py morning          # generate today's brief
    uv run python3 sage_runner.py evening          # evaluate today's hypothesis
    uv run python3 sage_runner.py both             # morning + evening (useful for testing)
    uv run python3 sage_runner.py enrich           # backfill story library + refresh patterns
    uv run python3 sage_runner.py patterns         # mine story library into pattern registry
    uv run python3 sage_runner.py accuracy         # SAGE accuracy summary
    uv run python3 sage_runner.py similar --adx 26 --time 10:30 --direction CE
    uv run python3 sage_runner.py status           # library + hypothesis counts

Options:
    --date YYYY-MM-DD     override today's date
    --strategy scalper | trend_rider
    --json                print raw JSON output
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def cmd_morning(args):
    from sage_morning_brief import generate_morning_brief
    brief = generate_morning_brief(date=args.date)
    if args.json:
        brief_out = {k: v for k, v in brief.items()
                     if k not in ("story_count", "patterns_matched")}
        print(json.dumps(brief_out, indent=2, default=str))


def cmd_evening(args):
    from sage_evening_verdict import generate_evening_verdict
    verdict = generate_evening_verdict(date=args.date)
    if args.json:
        print(json.dumps(verdict, indent=2, default=str))


def cmd_enrich(args):
    from sage_enricher import backfill_stories, story_count
    n = backfill_stories(verbose=True)
    sc = story_count()
    print(f"Story library: {sc['total']} total | "
          f"{sc['v2_enriched']} fully enriched | "
          f"{sc['wins']}W {sc['losses']}L")
    # Refresh the pattern registry from the (now updated) story library.
    from sage_patterns import mine_patterns
    mine_patterns(verbose=True)


def cmd_patterns(args):
    from sage_patterns import mine_patterns
    mine_patterns(verbose=True)


def cmd_scorecard(args):
    from sage_scorecard import scorecard, equity_curve
    scorecard(days=args.days)
    if args.curve:
        equity_curve(args.curve)


def cmd_verdict(args):
    from sage_verdict import verdict
    verdict(verbose=True)


def cmd_accuracy(args):
    from sage_evening_verdict import accuracy_summary
    accuracy_summary()


def cmd_similar(args):
    from sage_similarity import query_from_conditions, _safe_float
    conditions = {
        "adx":           args.adx,
        "entry_time":    args.time,
        "direction":     args.direction,
        "oi_bias":       args.oi_bias,
        "trade_num_today": args.trade_num,
    }
    if args.vix:
        conditions["vix"] = args.vix
    result = query_from_conditions(
        conditions, top_n=args.top, strategy_filter=args.strategy
    )
    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return
    if "error" in result:
        print(result["error"]); return
    matched = result.get("matched", 0)
    print(f"\n{'='*60}")
    print(f"  SAGE SIMILARITY  ·  {matched} matches found")
    if matched == 0:
        print(f"  Recommendation : {result.get('recommendation', 'INSUFFICIENT_DATA')}")
        print(f"  (Try without --strategy filter or lower --adx)")
        return
    print(f"  Win Rate : {round(result['win_rate']*100,1)}%  "
          f"Avg P&L: ₹{result['avg_pnl_rs']:,.0f}  "
          f"Avg MFE: {result['avg_mfe_pts']}pts")
    print(f"  Recommendation : {result['recommendation']}  "
          f"(confidence {result['confidence']:.2f})")
    print(f"{'='*60}")
    for s in result["stories"][:8]:
        print(f"  {s['date']}  {(s['entry_time'] or ''):<8}  "
              f"{(s['direction'] or ''):<4}  "
              f"₹{_safe_float(s.get('pnl_rs')):>7,.0f}  "
              f"{(s['exit_reason'] or ''):<20}  "
              f"sim={s['similarity_score']:.3f}")


def cmd_status(args):
    from sage_enricher import story_count
    from sage_db import get_sage_db
    sc = story_count()
    db = get_sage_db()
    hyp_count = db.query(
        "SELECT COUNT(*) as n FROM daily_hypotheses"
    )[0]["n"]
    scored = db.query(
        "SELECT COUNT(*) as n FROM daily_hypotheses WHERE hypothesis_correct IS NOT NULL"
    )[0]["n"]
    print(f"\n  SAGE STATUS")
    print(f"  Story library  : {sc['total']} trades  "
          f"({sc['v2_enriched']} fully enriched)")
    print(f"  Win/Loss       : {sc['wins']}W / {sc['losses']}L")
    print(f"  Hypotheses     : {hyp_count} total  ({scored} evaluated)")


def main():
    p = argparse.ArgumentParser(
        description="SAGE — Situational Awareness & Growth Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = p.add_subparsers(dest="command")

    # morning
    m = sub.add_parser("morning", help="Generate today's morning brief")
    m.add_argument("--date",   type=str, default=None)
    m.add_argument("--json",   action="store_true")

    # evening
    e = sub.add_parser("evening", help="Evaluate today's hypothesis")
    e.add_argument("--date",   type=str, default=None)
    e.add_argument("--json",   action="store_true")

    # both
    b = sub.add_parser("both", help="Run morning + evening (testing)")
    b.add_argument("--date",   type=str, default=None)
    b.add_argument("--json",   action="store_true")

    # enrich
    sub.add_parser("enrich", help="Backfill story library + refresh patterns")

    # patterns
    sub.add_parser("patterns", help="Mine the story library into pattern_registry")

    # scorecard
    sc = sub.add_parser("scorecard", help="Net-of-cost P&L per engine (the truth serum)")
    sc.add_argument("--days",  type=int, default=None)
    sc.add_argument("--curve", type=str, default=None, help="equity curve for a strategy")

    # verdict
    sub.add_parser("verdict", help="One-screen GO/NO-GO: should you trade today?")

    # accuracy
    sub.add_parser("accuracy", help="SAGE prediction accuracy summary")

    # similar
    s = sub.add_parser("similar", help="Find similar past trade setups")
    s.add_argument("--adx",       type=float, default=25.0)
    s.add_argument("--time",      type=str,   default="10:00")
    s.add_argument("--direction", type=str,   default="CE")
    s.add_argument("--oi-bias",   type=str,   default="neutral", dest="oi_bias")
    s.add_argument("--trade-num", type=int,   default=1, dest="trade_num")
    s.add_argument("--vix",       type=float, default=None)
    s.add_argument("--strategy",  type=str,   default=None)
    s.add_argument("--top",       type=int,   default=10)
    s.add_argument("--json",      action="store_true")

    # status
    sub.add_parser("status", help="Library and hypothesis counts")

    args = p.parse_args()

    if args.command == "morning":
        cmd_morning(args)
    elif args.command == "evening":
        cmd_evening(args)
    elif args.command == "both":
        cmd_morning(args)
        print()
        cmd_evening(args)
    elif args.command == "enrich":
        cmd_enrich(args)
    elif args.command == "patterns":
        cmd_patterns(args)
    elif args.command == "scorecard":
        cmd_scorecard(args)
    elif args.command == "verdict":
        cmd_verdict(args)
    elif args.command == "accuracy":
        cmd_accuracy(args)
    elif args.command == "similar":
        cmd_similar(args)
    elif args.command == "status":
        cmd_status(args)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
