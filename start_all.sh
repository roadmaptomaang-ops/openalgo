#!/usr/bin/env bash
# Starts today's full engine lineup in one shot, each backgrounded with its
# own log file, instead of the RUN.md ritual of pasting into 5+ terminal
# tabs by hand every morning.
#
# Built 2026-07-17 after a late (10:00 vs 09:20) manual start on 07-16 cost
# the first 40 minutes of the session — usually the highest-momentum window
# for the breakout engines.
#
# Usage:
#   export OPENALGO_API_KEY=...
#   ./start_all.sh
#
# Pair with watchdog.sh (or schedule both via launchd/cron) to also recover
# from mid-day crashes automatically.
set -uo pipefail
cd "$(dirname "$0")"

export OPENALGO_API_KEY="${OPENALGO_API_KEY:-1c19c83e21ce74f33fc323b1c1af438c35d11ef669cea5215f07212af308e81e}"

LOGDIR="$HOME/openalgo/logs/startup"
mkdir -p "$LOGDIR"
STAMP=$(date +%F_%H%M%S)

# ---- app.py first — everything else needs it up. Flask-SocketIO's dev
#      server refuses to run headless without a TTY on stdin, so fake one
#      with `script`. (2026-07-16: a plain background launch crashes with
#      "Werkzeug web server is not designed to run in production" every time.) ----
if ! curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:5000/ 2>/dev/null | grep -qE "200|302"; then
  echo "[start_all] starting app.py..."
  rm -f /tmp/openalgo_app.log
  script -q /tmp/openalgo_app.log caffeinate -dimsu uv run app.py &
  disown
  for i in $(seq 1 30); do
    code=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:5000/ 2>/dev/null)
    [[ "$code" == "200" || "$code" == "302" ]] && { echo "[start_all] app up after ${i}s"; break; }
    sleep 1
  done
else
  echo "[start_all] app.py already running — skipping."
fi

# NOTE: Upstox login (OAuth, browser) cannot be automated — engines below
# will start on schedule but sit safely idle (proven fail-safe: they refuse
# to trade on unresolved symbols/empty candles) until a human logs in at
# http://127.0.0.1:5000. That step stays manual by design.

start() {
  local name="$1" dir="$2" cmd="$3"
  echo "[start_all] launching $name..."
  ( cd "$dir" && eval "$cmd" ) >> "$LOGDIR/${name}_${STAMP}.log" 2>&1 &
  disown
  echo "[start_all] $name pid=$!"
}

start mint         "$HOME/openalgo/MINT"              "./run_mint.sh"
start mint_e       "$HOME/openalgo/MINT_E"             "./run_mint_e.sh"
start theta        "$HOME/openalgo/THETA"              "./run_theta.sh"
start trend_rider  "$HOME/openalgo/PIT_codex_fixes"    "caffeinate -i uv run python3 trend_rider_v2.py"
start scalper_v51  "$HOME/openalgo/PIT_codex_fixes"    "caffeinate -i uv run python3 scalper_v51_rider.py"
start purse        "$HOME/openalgo/PURSE"              "./run_purse.sh"

# SWING is a one-shot daily-close script, not a daemon — run it inline so
# it's done before this script exits, no backgrounding needed.
echo "[start_all] running swing (one-shot)..."
( cd "$HOME/openalgo/SWING" && ./run_swing.sh ) >> "$LOGDIR/swing_${STAMP}.log" 2>&1

echo "[start_all] app + 6 daemons launched (mint, mint_e, theta, trend_rider,"
echo "[start_all] scalper_v51, purse) + swing run. logs: $LOGDIR"
echo "[start_all] REMINDER: log into Upstox at http://127.0.0.1:5000 — nothing"
echo "[start_all] will trade on real data until that happens."

# Keep the watchdog running all day so a mid-session crash (the 07-16
# incident that motivated building it) auto-recovers unattended.
if ! pgrep -f "watchdog.sh --loop" > /dev/null 2>&1; then
  echo "[start_all] starting watchdog --loop..."
  nohup "$HOME/openalgo/watchdog.sh" --loop >> "$LOGDIR/watchdog_${STAMP}.log" 2>&1 &
  disown
else
  echo "[start_all] watchdog --loop already running — skipping."
fi
