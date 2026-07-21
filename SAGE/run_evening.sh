#!/usr/bin/env bash
# SAGE end-of-day: grade + ingest + refresh patterns + NET scorecard
cd "$(dirname "$0")"
uv run python3 sage_runner.py evening
uv run python3 sage_runner.py enrich
uv run python3 sage_runner.py scorecard
