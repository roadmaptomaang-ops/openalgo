#!/usr/bin/env bash
# NEWPIT — the Capture Engine (offered vs captured vs regret)
cd "$(dirname "$0")"
uv run python3 newpit.py
echo ""
uv run python3 newpit.py --ceiling
