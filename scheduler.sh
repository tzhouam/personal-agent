#!/usr/bin/env bash
# Fallback daily scheduler for environments without systemd/cron (e.g. this
# container, where PID 1 is tini). Prefer the systemd units on a real host.
#
#   nohup /rebase/personal-agent/scheduler.sh >/dev/null 2>&1 &
#
set -u

HK_TZ="Asia/Hong_Kong"
RUN_AT="07:00"
ASSISTANT="/rebase/.venv/bin/assistant"
LOG_DIR="${HOME}/.personal-agent"
PID_FILE="${LOG_DIR}/scheduler.pid"
LOG_FILE="${LOG_DIR}/scheduler.log"

mkdir -p "$LOG_DIR"
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "scheduler already running (pid $(cat "$PID_FILE"))" >&2
    exit 1
fi
echo $$ > "$PID_FILE"

next_run_epoch() {
    local today target
    today=$(TZ="$HK_TZ" date +%F)
    target=$(TZ="$HK_TZ" date -d "$today $RUN_AT" +%s)
    if [ "$(date +%s)" -ge "$target" ]; then
        target=$(TZ="$HK_TZ" date -d "$today $RUN_AT + 1 day" +%s)
    fi
    echo "$target"
}

echo "$(date -Is) scheduler started (daily at $RUN_AT $HK_TZ)" >> "$LOG_FILE"
while true; do
    target=$(next_run_epoch)
    sleep $(( target - $(date +%s) ))
    echo "$(date -Is) starting daily run" >> "$LOG_FILE"
    "$ASSISTANT" run >> "$LOG_FILE" 2>&1 || "$ASSISTANT" run --resume >> "$LOG_FILE" 2>&1
    echo "$(date -Is) daily run finished (exit $?)" >> "$LOG_FILE"
    sleep 120  # don't double-fire within the same minute
done
