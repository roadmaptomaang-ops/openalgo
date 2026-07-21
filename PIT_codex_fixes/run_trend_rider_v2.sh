#!/usr/bin/env bash
# Trend Rider V2 (the earner) — PIT_codex_fixes
#
# 2026-07-15: SAGE flagged edge decay (lifetime +Rs47/trade is a fossil from
# before 07-08; since then -Rs49/trade, regime gauge reads CHOP). Briefly
# paused same day, then UNPAUSED at Danny's call — MINT-E is A/B testing the
# same entry signal with MINT's exits and needs Trend Rider's own arm to keep
# running as the comparison baseline. Watch the regime gauge; if it stays
# CHOP and per-trade P&L stays negative, revisit standing it down for real.

export OPENALGO_API_KEY=1c19c83e21ce74f33fc323b1c1af438c35d11ef669cea5215f07212af308e81e
cd "$(dirname "$0")"
rm -f "$HOME/openalgo/control/squareoff.flag"  # ignore any stale square-off request on fresh start
exec caffeinate -i uv run python3 trend_rider_v2.py
