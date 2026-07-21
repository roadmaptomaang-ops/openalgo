#!/usr/bin/env bash
# SWING — multi-day equity swing book. Run ONCE each morning after 09:20.
# It decides, places orders, and exits in ~1 minute. No daemon.
export OPENALGO_API_KEY=1c19c83e21ce74f33fc323b1c1af438c35d11ef669cea5215f07212af308e81e
cd "$(dirname "$0")"
exec uv run python3 swing.py
