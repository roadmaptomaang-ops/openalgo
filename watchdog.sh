#!/usr/bin/env bash
# Engine watchdog — restarts any of today's live engines that have died.
#
# Why this exists: on 2026-07-16, mint/mint_e/scalper_v51/trend_rider all
# stopped logging within the same minute (~13:34) and PURSE never scanned
# past its startup banner — something killed the terminals mid-session and
# the rest of the day's opportunity was silently lost. This checks every
# few minutes during market hours and restarts anything missing.
#
# Usage:
#   ./watchdog.sh          → run one check pass
#   ./watchdog.sh --loop    → loop forever, checking every 5 min (foreground)
set -uo pipefail
cd "$(dirname "$0")"

LOGDIR="$HOME/openalgo/logs/watchdog"
mkdir -p "$LOGDIR"
LOGFILE="$LOGDIR/$(date +%F).log"

export OPENALGO_API_KEY="${OPENALGO_API_KEY:-1c19c83e21ce74f33fc323b1c1af438c35d11ef669cea5215f07212af308e81e}"

# name|process-match-pattern|start-command
# Plain array, not `declare -A` — this Mac has no Homebrew bash, and
# /usr/bin/env bash resolves to Apple's frozen bash 3.2, which has no
# associative arrays. (2026-07-20: silently broke every restart with
# "unbound variable" until traced to this.)
ENGINES=(
  "mint|mint\.py|cd $HOME/openalgo/MINT && ./run_mint.sh"
  "mint_e|mint_e\.py|cd $HOME/openalgo/MINT_E && ./run_mint_e.sh"
  "purse|purse\.py|cd $HOME/openalgo/PURSE && ./run_purse.sh"
  "theta|theta\.py|cd $HOME/openalgo/THETA && ./run_theta.sh"
  "trend_rider|trend_rider_v2\.py|cd $HOME/openalgo/PIT_codex_fixes && caffeinate -i uv run python3 trend_rider_v2.py"
  "scalper_v51|scalper_v51_rider\.py|cd $HOME/openalgo/PIT_codex_fixes && caffeinate -i uv run python3 scalper_v51_rider.py"
)

log() { echo "[$(date +%T)] $*" | tee -a "$LOGFILE"; }

check_market_hours() {
  # 10#$hm forces base-10 — a bare leading-zero number like 0915 is
  # otherwise parsed as octal by bash arithmetic, and 9 isn't a valid
  # octal digit, so this silently errored and always reported "outside
  # market hours" no matter the actual time. (2026-07-20)
  local hm; hm=$(date +%H%M)
  [[ $((10#$hm)) -ge 915 && $((10#$hm)) -le 1530 ]]
}

pass() {
  if ! check_market_hours; then
    log "outside market hours (09:15-15:30) — skipping check"
    return
  fi
  for entry in "${ENGINES[@]}"; do
    IFS='|' read -r name pattern startcmd <<< "$entry"
    if pgrep -f "$pattern" > /dev/null 2>&1; then
      : # alive, nothing to do
    else
      log "MISSING: $name (pattern: $pattern) — restarting"
      # shellcheck disable=SC2086
      nohup bash -c "export OPENALGO_API_KEY=$OPENALGO_API_KEY; $startcmd" \
        >> "$LOGDIR/${name}_restart.log" 2>&1 &
      disown
      log "restarted $name (pid $!)"
    fi
  done
}

if [[ "${1:-}" == "--loop" ]]; then
  log "watchdog loop started (checking every 300s during market hours)"
  while true; do
    pass
    sleep 300
  done
else
  pass
fi
