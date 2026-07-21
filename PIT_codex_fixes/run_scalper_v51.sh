#!/usr/bin/env bash
# Scalper V5.1 (re-entry guard) — PIT_codex_fixes.  Run only ONE scalper at a time.
export OPENALGO_API_KEY=1c19c83e21ce74f33fc323b1c1af438c35d11ef669cea5215f07212af308e81e
cd "$(dirname "$0")"
rm -f "$HOME/openalgo/control/squareoff.flag"  # ignore any stale square-off request on fresh start
exec caffeinate -i uv run python3 scalper_v51_rider.py
