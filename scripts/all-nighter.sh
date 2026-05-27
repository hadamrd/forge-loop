#!/usr/bin/env bash
# all-nighter.sh — start the forge-loop in a detached tmux session with
# a restart-on-crash shim so it actually survives the operator closing
# their terminal.
#
# Usage:
#   dev/sprint-loop/scripts/all-nighter.sh          # start (or re-attach)
#   dev/sprint-loop/scripts/all-nighter.sh status   # is it alive?
#   dev/sprint-loop/scripts/all-nighter.sh stop     # graceful stop + kill session
#   dev/sprint-loop/scripts/all-nighter.sh tail     # tail loop events
#
# Survival mechanics:
#   - Runs inside `tmux new-session -d` so it lives across SIGHUP / logout.
#   - The session's command is a `while true` loop that re-execs the loop
#     if it crashes (non-zero exit), with a 30s backoff. The HALT touchfile
#     (`docs/ops/loop-runner.HALT`) breaks out of the restart shim cleanly.
#   - Detects existing sessions and refuses to double-start.

set -euo pipefail

REPO="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
LOOP_DIR="${REPO}/dev/sprint-loop"
SESSION="forge-loop"
HALT_FILE="${REPO}/docs/ops/loop-runner.HALT"
STOP_FILE="${REPO}/docs/ops/loop-runner.stop"
PID_FILE="${REPO}/docs/ops/loop-runner.pid"
EVENTS_FILE="${REPO}/docs/ops/loop-runner-events.jsonl"

cmd="${1:-start}"

case "$cmd" in
  start)
    if ! command -v tmux >/dev/null 2>&1; then
      echo "ERROR: tmux not installed. apt install tmux." >&2
      exit 1
    fi
    if tmux has-session -t "$SESSION" 2>/dev/null; then
      echo "[all-nighter] session '$SESSION' already running. Use 'tmux attach -t $SESSION' to view, or 'stop' to kill." >&2
      exit 0
    fi
    # Clear any stale halt marker from a previous run.
    rm -f "$HALT_FILE" "$STOP_FILE"

    # Shim: re-exec the loop on non-zero exit with 30s backoff, unless the
    # halt marker has been written (drift detector or operator).
    inner_cmd="$(cat <<'INNER'
cd "REPLACE_LOOP_DIR"
while true; do
  if [ -f "REPLACE_HALT_FILE" ]; then
    echo "[all-nighter] HALT marker present, not (re)starting"
    sleep 60
    continue
  fi
  uv run forge-loop run
  rc=$?
  if [ -f "REPLACE_HALT_FILE" ]; then
    echo "[all-nighter] HALT marker created during run; will idle"
    sleep 60
    continue
  fi
  echo "[all-nighter] loop exited rc=$rc; restarting in 30s"
  sleep 30
done
INNER
)"
    inner_cmd="${inner_cmd//REPLACE_LOOP_DIR/$LOOP_DIR}"
    inner_cmd="${inner_cmd//REPLACE_HALT_FILE/$HALT_FILE}"

    tmux new-session -d -s "$SESSION" "bash -c '$inner_cmd' 2>&1 | tee -a $LOOP_DIR/all-nighter.log"
    sleep 1
    echo "[all-nighter] started session '$SESSION'"
    echo "  attach : tmux attach -t $SESSION"
    echo "  tail   : $0 tail"
    echo "  status : $0 status"
    echo "  stop   : $0 stop"
    ;;

  status)
    if tmux has-session -t "$SESSION" 2>/dev/null; then
      echo "[all-nighter] tmux session '$SESSION' RUNNING"
    else
      echo "[all-nighter] tmux session '$SESSION' NOT running"
    fi
    if [ -f "$HALT_FILE" ]; then
      echo "[all-nighter] HALT marker present:"
      cat "$HALT_FILE"
    fi
    if [ -f "$PID_FILE" ]; then
      pid=$(cat "$PID_FILE")
      if kill -0 "$pid" 2>/dev/null; then
        echo "[all-nighter] runner pid $pid: alive"
      else
        echo "[all-nighter] runner pid $pid: STALE"
      fi
    fi
    cd "$LOOP_DIR" && uv run forge-loop status 2>&1 || true
    ;;

  stop)
    if tmux has-session -t "$SESSION" 2>/dev/null; then
      touch "$STOP_FILE"
      echo "[all-nighter] touched $STOP_FILE — waiting up to 30s for graceful exit"
      for _ in $(seq 1 30); do
        if [ ! -f "$STOP_FILE" ]; then break; fi
        sleep 1
      done
      tmux kill-session -t "$SESSION" 2>/dev/null || true
      echo "[all-nighter] session killed"
    else
      echo "[all-nighter] no session to stop"
    fi
    ;;

  tail)
    if [ ! -f "$EVENTS_FILE" ]; then
      echo "[all-nighter] no events file at $EVENTS_FILE" >&2
      exit 1
    fi
    tail -F "$EVENTS_FILE"
    ;;

  *)
    echo "Usage: $0 {start|status|stop|tail}" >&2
    exit 2
    ;;
esac
