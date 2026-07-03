#!/usr/bin/env bash
# Daily pipeline entry for OpenClaw command-cron (replaces the scheduler.sh loop).
# stdout is the announce message (delivered to WeChat when the cron job has
# --announce) so it must stay ONE short line; full pipeline output goes to
# ~/.personal-agent/daily-run.log. Exit code: 0 only when the run finished,
# so cron run history records error (and can alert) on a stuck pipeline.
set -u
# The container clock is UTC and OpenClaw cron fires at 23:00 UTC (= 07:00
# HKT). Pin the owner's zone so date.today()/run ids/digest dates match his
# morning, not the previous UTC day. Requires the tzdata package.
export TZ=${PERSONAL_AGENT_TZ:-Asia/Hong_Kong}
ASSISTANT=/rebase/.venv/bin/assistant
PY=/rebase/.venv/bin/python
DIR="$HOME/.personal-agent"
LOG="$DIR/daily-run.log"
mkdir -p "$DIR"

# Overlap guard: chat trigger_run checks state.json before spawning, but the
# reverse race (cron firing mid chat-triggered run) is caught here.
exec 9>"$DIR/daily-run.lock"
if ! flock -n 9; then
  echo "Daily run skipped: another run is already in progress."
  exit 0
fi

{
  echo "=== $(date -Is) daily run (cron) ==="
  "$ASSISTANT" run "$@" || "$ASSISTANT" run --resume "$@"
} >>"$LOG" 2>&1
rc=$?

"$PY" - "$rc" <<'EOF'
import json, pathlib, sys

rc = int(sys.argv[1])
d = pathlib.Path.home() / ".personal-agent"
try:
    st = json.loads((d / "state.json").read_text())
except Exception:
    st = {}
run_id, phase = st.get("run_id", "?"), st.get("phase", "?")
if rc == 0 and phase in (None, "done"):
    extra = ""
    try:
        dg = json.loads((d / "runs" / run_id / "digest.json").read_text())
        n = sum(len(v) for v in dg.get("sections", {}).values())
        extra = f", {n} digest items"
    except Exception:
        pass
    print(f"Daily digest done ({run_id}{extra}). Full digest in your email.")
    sys.exit(0)
print(f"Daily run {run_id} stuck at phase '{phase}' — check ~/.personal-agent/daily-run.log")
sys.exit(rc or 1)
EOF
