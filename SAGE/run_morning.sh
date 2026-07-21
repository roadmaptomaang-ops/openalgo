#!/usr/bin/env bash
# SAGE morning: the GO/NO-GO verdict first, then the full brief
cd "$(dirname "$0")"
uv run python3 sage_runner.py verdict
uv run python3 sage_runner.py morning
