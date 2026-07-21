#!/usr/bin/env bash
# PURSE — capital-aware single-position options trader (small real wallet)
export OPENALGO_API_KEY=1c19c83e21ce74f33fc323b1c1af438c35d11ef669cea5215f07212af308e81e
# export PURSE_CAPITAL=20000   # override starting wallet if you like
cd "$(dirname "$0")"
rm -f "$HOME/openalgo/control/squareoff.flag"  # ignore any stale square-off request on fresh start
exec caffeinate -i uv run python3 purse.py
