#!/usr/bin/env bash
# Tell every running engine to close its open position(s) via its OWN exit
# logic (which records the trade card + DB row) and stop — the clean
# alternative to flattening from outside the engine.
#
#   ./squareoff.sh          → request square-off for today
#   ./squareoff.sh --clear  → cancel a pending request
#
# Engines check the flag every few seconds, so they wind down within ~5-10s.
# The flag carries today's date; run_*.sh clears it on startup so a mid-day
# restart doesn't immediately re-square-off.
set -euo pipefail
FLAG="$HOME/openalgo/control/squareoff.flag"
mkdir -p "$(dirname "$FLAG")"

if [[ "${1:-}" == "--clear" ]]; then
  rm -f "$FLAG"
  echo "Square-off request cleared."
  exit 0
fi

date +%F > "$FLAG"
echo "Square-off requested for $(date +%F)."
echo "Running engines will close open positions (self-recorded) and stop within ~10s."
echo "Clear with: ./squareoff.sh --clear   (or it auto-expires tomorrow)"
