#!/usr/bin/env bash
# forge-loop forward-progress watchdog.
#
# WHY: event watchers (PR_OPEN / STALL / DEAD) are blind to "busy but not
# progressing" — a loop can spin for hours re-reviewing one approved-but-blocked
# PR while every liveness check stays green (this cost ~9h once). The invariant
# that matters is FORWARD PROGRESS (merges landing), not liveness. This script
# watches that invariant and exits non-zero on a tripwire so a supervisor (cron,
# systemd, an operator, or an agent re-invoked on exit) can react.
#
# USAGE:
#   REPO_PATH=/path/to/checkout GH_REPO=owner/name scripts/progress-watchdog.sh
#
# ENV (all optional except the two above):
#   REPO_PATH    local checkout the loop runs in        (default: cwd)
#   GH_REPO      GitHub slug owner/name for merge polling (required for stall check)
#   STALL_SECS   no-merge window before a stall          (default: 5400 = 90min)
#   QUIET_SECS   event-log silence to confirm "stuck"    (default: 1800 = 30min)
#   POLL_SECS    seconds between checks                  (default: 600 = 10min)
#   COMMENT_CAP  open-PR comment count = comment-storm   (default: 18)
#   EVENTLOG     path to loop-runner-events.jsonl        (default: $REPO_PATH/docs/ops/loop-runner-events.jsonl)
#   STOPFILE     path to the loop stop-file              (default: $REPO_PATH/docs/ops/loop-runner.stop)
#
# EXIT CODES (tripwires):
#   10  loop entrypoint absent (DEAD)
#   11  stop-file present
#   12  no merge in STALL_SECS AND event log quiet QUIET_SECS (STUCK)
#   13  an open PR is piling up comments past COMMENT_CAP (busy-but-stuck)
#    2  misconfiguration
#
# NOTE on liveness detection: match the real installed entrypoint "bin/forge-loop"
# (NOT the string "forge-loop run", which self-matches an interactive shell that
# happens to contain that literal). Retry briefly to tolerate a tick boundary.
set -u

REPO_PATH="${REPO_PATH:-$PWD}"
GH_REPO="${GH_REPO:-}"
STALL_SECS="${STALL_SECS:-5400}"
QUIET_SECS="${QUIET_SECS:-1800}"
POLL_SECS="${POLL_SECS:-600}"
COMMENT_CAP="${COMMENT_CAP:-18}"
EVENTLOG="${EVENTLOG:-$REPO_PATH/docs/ops/loop-runner-events.jsonl}"
STOPFILE="${STOPFILE:-$REPO_PATH/docs/ops/loop-runner.stop}"

cd "$REPO_PATH" || { echo "WATCHDOG: cannot cd $REPO_PATH"; exit 2; }
command -v gh >/dev/null 2>&1 || { echo "WATCHDOG: gh CLI not found"; exit 2; }

# gh needs an empty GH_TOKEN to use its own stored auth (a loop-exported GH_TOKEN
# breaks the gh CLI). Poll merges via ls-remote so no token is needed at all.
remote_main_sha() { git ls-remote "https://github.com/$GH_REPO.git" main 2>/dev/null | awk '{print $1}'; }

base_sha=""
[ -n "$GH_REPO" ] && base_sha="$(remote_main_sha)"
last_advance="$(date +%s)"

echo "WATCHDOG: watching $REPO_PATH (repo=${GH_REPO:-<none>}) stall=${STALL_SECS}s quiet=${QUIET_SECS}s poll=${POLL_SECS}s"

while true; do
  now="$(date +%s)"

  # 1. loop alive? (real entrypoint, brief retry window for tick boundaries)
  alive=0
  for _ in 1 2 3; do
    if pgrep -af "bin/forge-loop" 2>/dev/null | grep -qv -e pgrep -e watchdog; then alive=1; break; fi
    sleep 10
  done
  [ "$alive" = "0" ] && { echo "TRIPWIRE(10): forge-loop entrypoint absent (DEAD)"; exit 10; }

  # 2. stop-file
  [ -f "$STOPFILE" ] && { echo "TRIPWIRE(11): stop-file present ($STOPFILE)"; exit 11; }

  # 3. forward progress: merge advanced OR loop still actively ticking
  if [ -n "$GH_REPO" ]; then
    cur_sha="$(remote_main_sha)"
    [ -n "$cur_sha" ] && [ "$cur_sha" != "$base_sha" ] && { base_sha="$cur_sha"; last_advance="$now"; }
    ev_age=999999
    [ -f "$EVENTLOG" ] && ev_age=$(( now - $(stat -c %Y "$EVENTLOG" 2>/dev/null || echo 0) ))
    # Two-factor: a fresh event log means the loop is working (building/repairing a
    # risk-gated PR that parks for human review IS progress) — only a frozen log
    # plus no merge is a genuinely stuck loop.
    if [ $(( now - last_advance )) -ge "$STALL_SECS" ] && [ "$ev_age" -ge "$QUIET_SECS" ]; then
      echo "TRIPWIRE(12): no merge in $((STALL_SECS/60))min AND event log quiet ${ev_age}s (STUCK)"; exit 12
    fi
  fi

  # 4. a PR piling up comments without merging (busy-but-stuck signature)
  if [ -n "$GH_REPO" ]; then
    stuck="$(GH_TOKEN= gh pr list --repo "$GH_REPO" --state open \
              --json number,comments --jq ".[]|select((.comments|length)>$COMMENT_CAP)|.number" 2>/dev/null | head -1)"
    [ -n "$stuck" ] && { echo "TRIPWIRE(13): PR #$stuck piling up >$COMMENT_CAP comments without merging"; exit 13; }
  fi

  sleep "$POLL_SECS"
done
