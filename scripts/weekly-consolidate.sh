#!/usr/bin/env bash
# Weekly profile consolidation for OpenClaw command-cron (profile-v2 P2).
# stdout must stay ONE short line (the WeChat announce); full output goes to
# ~/.personal-agent/consolidate.log. See doc/RESEARCH_AGENT_MEMORY_2026.md §4.
set -u
export TZ=${PERSONAL_AGENT_TZ:-Asia/Hong_Kong}
ASSISTANT=/rebase/.venv/bin/assistant
LOG="$HOME/.personal-agent/consolidate.log"
mkdir -p "$HOME/.personal-agent"

{
  echo "=== $(date -Is) weekly consolidation (cron) ==="
  "$ASSISTANT" consolidate
} >>"$LOG" 2>&1
rc=$?

if [ $rc -eq 0 ]; then
  summary=$(tail -5 "$LOG" | grep -Eo '[0-9]+ ops applied[^"]*' | tail -1)
  echo "Weekly profile consolidation done${summary:+ ($summary)}. Diff in your email."
else
  echo "Weekly consolidation failed (rc=$rc) — check ~/.personal-agent/consolidate.log"
fi
exit $rc
