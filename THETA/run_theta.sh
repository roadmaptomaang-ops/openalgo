#!/usr/bin/env bash
# THETA — chop-day defined-risk premium seller (NIFTY iron condor)
export OPENALGO_API_KEY=1c19c83e21ce74f33fc323b1c1af438c35d11ef669cea5215f07212af308e81e
cd "$(dirname "$0")"
rm -f "$HOME/openalgo/control/squareoff.flag"  # ignore any stale square-off request on fresh start
exec caffeinate -i uv run python3 theta.py
