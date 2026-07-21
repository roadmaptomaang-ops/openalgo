#!/usr/bin/env bash
# Trend Rider V3 (cap fix) — PIT_v2
export OPENALGO_API_KEY=1c19c83e21ce74f33fc323b1c1af438c35d11ef669cea5215f07212af308e81e
cd "$(dirname "$0")"
exec caffeinate -i uv run python3 trend_rider_v3.py
