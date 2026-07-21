#!/usr/bin/env bash
# Equity Dashboard — live NIFTY 100 display (read-only, own port 5051)
export OPENALGO_API_KEY=1c19c83e21ce74f33fc323b1c1af438c35d11ef669cea5215f07212af308e81e
cd "$(dirname "$0")"
exec uv run python3 dashboard_server.py
