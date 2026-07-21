"""
sage_similarity.py — Cosine Similarity Engine
==============================================
Given a query feature vector (current market conditions),
finds the N most similar past Trade Stories and returns
their aggregated outcome stats.

No ML libraries required — pure numpy cosine similarity.

Usage:
    from sage_similarity import find_similar, query_from_conditions
    matches = find_similar(query_vector, top_n=10)
    result  = query_from_conditions(conditions_dict, top_n=10)
"""

import json
from pathlib import Path
import sys
from typing import Optional

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from sage_db import get_sage_db
from sage_enricher import (
    _build_feature_v1, _build_feature_v2,
    _direction_num, _oi_bias_num, _parse_time, _safe_float
)

MIN_STORIES_FOR_SEARCH = 10   # don't bother if library is too small
MIN_SCORE_THRESHOLD   = 0.70  # cosine similarity floor


def _load_story_matrix() -> tuple[list, np.ndarray]:
    """Load all stories with valid feature vectors. Returns (stories, matrix)."""
    db = get_sage_db()
    rows = db.query(
        "SELECT * FROM trade_stories WHERE feature_vector IS NOT NULL ORDER BY date"
    )
    stories = []
    vectors = []
    for r in rows:
        try:
            fv = json.loads(r["feature_vector"])
            if len(fv) >= 9:
                stories.append(r)
                vectors.append(fv[:9])   # use v1 slice for universal comparison
        except Exception:
            continue
    if not stories:
        return [], np.array([])
    return stories, np.array(vectors, dtype=np.float32)


def _cosine_similarity(query: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """Returns cosine similarity of query against every row in matrix."""
    q_norm = np.linalg.norm(query)
    if q_norm == 0:
        return np.zeros(len(matrix))
    m_norms = np.linalg.norm(matrix, axis=1)
    m_norms[m_norms == 0] = 1e-9
    return (matrix @ query) / (m_norms * q_norm)


def find_similar(
    query_vector: list,
    top_n: int = 10,
    strategy_filter: Optional[str] = None,
    instrument_filter: Optional[str] = None,
    min_score: float = MIN_SCORE_THRESHOLD,
    log_query: bool = True,
) -> dict:
    """
    Find the top_n most similar Trade Stories to the query vector.

    Returns:
        {
          "matched": int,
          "win_rate": float,
          "avg_pnl_rs": float,
          "avg_mfe_pts": float,
          "avg_hold_sec": int,
          "top_exit_reason": str,
          "recommendation": "ENTER" | "CAUTION" | "SKIP",
          "confidence": float,   # 0–1
          "stories": [list of matched story dicts with score]
        }
    """
    stories, matrix = _load_story_matrix()
    if len(stories) < MIN_STORIES_FOR_SEARCH:
        return {"error": f"Library too small ({len(stories)} stories). "
                         f"Need {MIN_STORIES_FOR_SEARCH}+."}

    # apply strategy/instrument filter
    if strategy_filter or instrument_filter:
        filtered_stories = []
        filtered_matrix  = []
        for i, s in enumerate(stories):
            if strategy_filter and s["strategy"] != strategy_filter:
                continue
            if instrument_filter and s["instrument"] != instrument_filter:
                continue
            filtered_stories.append(s)
            filtered_matrix.append(matrix[i])
        if not filtered_stories:
            return {"error": "No stories match the filter criteria."}
        stories = filtered_stories
        matrix  = np.array(filtered_matrix, dtype=np.float32)

    q = np.array(query_vector[:9], dtype=np.float32)
    scores = _cosine_similarity(q, matrix)

    # rank and filter by threshold
    ranked_idx = np.argsort(scores)[::-1]
    selected   = [(stories[i], float(scores[i]))
                  for i in ranked_idx
                  if scores[i] >= min_score][:top_n]

    if not selected:
        return {
            "matched": 0,
            "recommendation": "INSUFFICIENT_DATA",
            "confidence": 0.0,
            "stories": [],
        }

    wins   = sum(1 for s, _ in selected if s["outcome"] == "WIN")
    losses = sum(1 for s, _ in selected if s["outcome"] == "LOSS")
    n      = len(selected)
    wr     = wins / n

    pnl_vals  = [_safe_float(s.get("pnl_rs")) for s, _ in selected]
    mfe_vals  = [_safe_float(s.get("mfe_pts")) for s, _ in selected]
    hold_vals = [int(s.get("hold_seconds") or 0) for s, _ in selected]

    avg_pnl   = sum(pnl_vals) / n
    avg_mfe   = sum(mfe_vals) / n
    avg_hold  = int(sum(hold_vals) / n)

    exit_reasons = [s.get("exit_reason", "") for s, _ in selected]
    top_exit = max(set(exit_reasons), key=exit_reasons.count) if exit_reasons else "—"

    avg_score = sum(sc for _, sc in selected) / n

    # recommendation
    if wr >= 0.65 and avg_pnl > 0:
        rec = "ENTER"
    elif wr >= 0.50 or avg_pnl > 0:
        rec = "CAUTION"
    else:
        rec = "SKIP"

    # persist the query for audit/history (best-effort; never break the search)
    if log_query:
        try:
            from datetime import datetime as _dt
            now = _dt.now()
            get_sage_db().log_similarity({
                "query_date":  now.strftime("%Y-%m-%d"),
                "query_time":  now.strftime("%H:%M:%S"),
                "strategy":    strategy_filter or "",
                "instrument":  instrument_filter or "",
                "query_features":   json.dumps([round(float(x), 4) for x in q.tolist()]),
                "matched_story_ids": json.dumps([s["id"] for s, _ in selected]),
                "match_scores":      json.dumps([round(sc, 3) for _, sc in selected]),
                "matched_win_rate":  round(wr, 3),
                "matched_avg_pnl":   round(avg_pnl, 0),
                "recommendation":    rec,
                "reason":            f"{n} matches, {wins}W/{losses}L",
            })
        except Exception as e:
            print(f"[SAGE similarity_log skipped] {e}")

    return {
        "matched":        n,
        "win_rate":       round(wr, 3),
        "wins":           wins,
        "losses":         losses,
        "avg_pnl_rs":     round(avg_pnl, 0),
        "avg_mfe_pts":    round(avg_mfe, 2),
        "avg_hold_sec":   avg_hold,
        "top_exit_reason": top_exit,
        "avg_similarity": round(avg_score, 3),
        "recommendation": rec,
        "confidence":     round(avg_score * wr, 3),
        "stories": [
            {**s, "similarity_score": round(sc, 3)}
            for s, sc in selected
        ],
    }


def query_from_conditions(
    conditions: dict,
    top_n: int = 10,
    strategy_filter: Optional[str] = None,
    instrument_filter: Optional[str] = None,
) -> dict:
    """
    Build a feature vector from a plain conditions dict and run similarity.

    conditions keys (all optional, sensible defaults used):
        adx, ema9, ema21, entry_price, vwap, orb_high, orb_low,
        entry_time (HH:MM), direction (CE/PE/BUY), oi_bias (str),
        trade_num_today (int), running_pnl_before (float),
        vix (float), nifty_change_pct (float),
        breadth_pct (float), session_age_min (int)

    When price/orb/ema are not supplied the vector uses neutral midpoint
    values so similarity is driven by the features you DO provide.
    """
    # Use neutral midpoints for unspecified price-relative features
    # so the query doesn't get skewed by placeholder values
    adx  = conditions.get("adx", 25.0)
    row = {
        "adx_entry":   adx,
        "ema9_entry":  conditions.get("ema9", adx),       # neutral: ema spread ≈ 0
        "ema21_entry": conditions.get("ema21", adx),
        "entry_price": conditions.get("entry_price", 100.0),
        "vwap_entry":  conditions.get("vwap", 100.0),     # neutral: vwap_dist = 0
        "orb_high":    conditions.get("orb_high", 101.0),
        "orb_low":     conditions.get("orb_low", 99.0),   # neutral: orb_pos = 0.5
        "entry_time":  conditions.get("entry_time", "10:00"),
        "direction":   conditions.get("direction", "CE"),
        "oi_bias":     conditions.get("oi_bias", "neutral"),
    }
    # override entry_price to sit at midpoint of orb when not specified
    if "entry_price" not in conditions and "orb_high" not in conditions:
        row["entry_price"] = 100.0   # stays at midpoint

    trade_num   = conditions.get("trade_num_today", 1)
    running_pnl = conditions.get("running_pnl_before", 0.0)

    fv = _build_feature_v1(row, trade_num, running_pnl)

    # upgrade to v2 if live context available
    vix = conditions.get("vix")
    if vix is not None:
        fv = _build_feature_v2(
            fv,
            vix,
            conditions.get("nifty_change_pct", 0.0),
            conditions.get("breadth_pct", 0.5),
            conditions.get("session_age_min", 60),
        )

    result = find_similar(
        fv, top_n=top_n,
        strategy_filter=strategy_filter,
        instrument_filter=instrument_filter,
    )
    result["query_features"] = fv
    result["query_conditions"] = conditions
    return result


def _cli():
    import argparse, json as _j
    p = argparse.ArgumentParser(description="SAGE Similarity Search")
    p.add_argument("--adx",      type=float, default=25.0)
    p.add_argument("--ema9",     type=float, default=0.0)
    p.add_argument("--ema21",    type=float, default=1.0)
    p.add_argument("--price",    type=float, default=1.0, dest="entry_price")
    p.add_argument("--vwap",     type=float, default=1.0)
    p.add_argument("--orb-high", type=float, default=1.0, dest="orb_high")
    p.add_argument("--orb-low",  type=float, default=0.0, dest="orb_low")
    p.add_argument("--time",     type=str,   default="10:00", dest="entry_time")
    p.add_argument("--direction",type=str,   default="CE")
    p.add_argument("--oi-bias",  type=str,   default="neutral", dest="oi_bias")
    p.add_argument("--strategy", type=str,   default=None)
    p.add_argument("--top",      type=int,   default=10)
    p.add_argument("--json",     action="store_true")
    a = p.parse_args()

    result = query_from_conditions(vars(a), top_n=a.top, strategy_filter=a.strategy)

    if a.json:
        print(_j.dumps(result, indent=2, default=str))
        return

    if "error" in result:
        print(result["error"]); return

    print(f"\n{'='*60}")
    print(f"  SAGE SIMILARITY  ·  matched {result['matched']} stories")
    print(f"  Win Rate: {result['win_rate']*100:.1f}%  "
          f"Avg P&L: ₹{result['avg_pnl_rs']:,.0f}  "
          f"Avg MFE: {result['avg_mfe_pts']:.1f}pts")
    print(f"  Recommendation: {result['recommendation']}  "
          f"(confidence {result['confidence']:.2f})")
    print(f"{'='*60}")
    print(f"  {'Date':<12} {'Time':<8} {'Dir':<4} {'PnL':>8}  {'Score'}")
    for s in result["stories"][:5]:
        print(f"  {s['date']:<12} {(s['entry_time'] or ''):<8} "
              f"{(s['direction'] or ''):<4} "
              f"₹{_safe_float(s.get('pnl_rs')):>7,.0f}  "
              f"{s['similarity_score']:.3f}")


if __name__ == "__main__":
    _cli()
