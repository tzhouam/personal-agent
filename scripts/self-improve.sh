#!/usr/bin/env bash
# Daily self-improvement: Opus 4.8 reads the day's traces + chat/task history and
# makes small, test-backed improvements to the agent, delivered as a reviewable PR.
#
#   scripts/self-improve.sh              # live: branch → Opus → tests → push → PR
#   scripts/self-improve.sh report-only  # analyze + edit on a branch, NO push/PR
#   scripts/self-improve.sh dry-run      # analyze only, Opus makes NO edits
#
# Safety: works on a dated branch (never commits to main), refuses to touch
# secrets/.env/.git, gates every change on the full test suite, and opens a PR
# instead of pushing unreviewed model-written code to a public main. Opus 4.8 is
# reached by clearing the ANTHROPIC_* proxy env so the CLI uses the logged-in
# Anthropic account, not the DeepSeek/MiMo endpoint.
set -uo pipefail
export TZ="${PERSONAL_AGENT_TZ:-Asia/Hong_Kong}"

REPO=/rebase/personal-agent
DATA="${HOME}/.personal-agent"
PY=/rebase/.venv/bin/python
LOG="$DATA/self-improve.log"
BRIEF="$DATA/self-improve-brief.md"
DAY=$(date +%F)
BRANCH="auto/improve-$DAY"
MODE="${1:-live}"

cd "$REPO" || exit 1
mkdir -p "$DATA"
exec 9>"$DATA/self-improve.lock"
flock -n 9 || { echo "$(date -Is) another self-improve run holds the lock" >>"$LOG"; exit 0; }

announce() {  # best-effort WeChat ping (never fails the job)
  local msg="$1"
  "$PY" - "$msg" <<'PY' >>"$LOG" 2>&1 || true
import sys
from assistant.config import Settings
from assistant.notify import send_wechat
send_wechat(Settings(), sys.argv[1])
PY
}

{
echo "=== $(date -Is) self-improve ($MODE) ==="

# 1. sync main
git fetch -q origin || { echo "fetch failed"; exit 1; }
git checkout -q main && git reset -q --hard origin/main

# 2. gather evidence; skip the (costly) Opus call on a quiet day
"$PY" "$REPO/scripts/self_improve_evidence.py" 2 > "$BRIEF" 2>>"$LOG"
if [ ! -s "$BRIEF" ]; then
  echo "no friction/perf signals in the last 2 days — nothing to improve"
  exit 0
fi
echo "evidence: $(wc -l < "$BRIEF") lines"

# 3. branch + Opus 4.8 (env cleared so the CLI uses the Anthropic login, not the proxy)
git checkout -q -B "$BRANCH" main
PROMPT="$(cat "$REPO/scripts/self_improve_prompt.md")

--- EVIDENCE ---
$(cat "$BRIEF")"
[ "$MODE" = "dry-run" ] && PROMPT="REPORT ONLY — analyze and explain, but do NOT edit any files.

$PROMPT"

env -u ANTHROPIC_API_KEY -u ANTHROPIC_BASE_URL -u ANTHROPIC_MODEL -u ANTHROPIC_AUTH_TOKEN \
    -u ANTHROPIC_DEFAULT_HAIKU_MODEL -u ANTHROPIC_DEFAULT_SONNET_MODEL -u ANTHROPIC_DEFAULT_OPUS_MODEL \
    timeout 1800 claude -p "$PROMPT" \
      --model opus --max-turns 80 --permission-mode acceptEdits --add-dir "$REPO" 2>&1 | tail -60

# 4. evaluate the diff
git add -A
if git diff --cached --quiet; then
  echo "Opus proposed no code changes"
  git checkout -q main; git branch -qD "$BRANCH" 2>/dev/null; exit 0
fi

# 4a. refuse sensitive paths
if git diff --cached --name-only | grep -qE '(^|/)\.env|\.credentials|/secrets|(^|/)\.git/'; then
  echo "ABORT: a proposed change touched a sensitive path"; git diff --cached --name-only
  git reset -q --hard origin/main; git checkout -q main; git branch -qD "$BRANCH" 2>/dev/null
  exit 1
fi
echo "changed files:"; git diff --cached --name-only | sed 's/^/  /'

# 5. tests must pass
if ! "$PY" -m pytest test/ -q >/tmp/si-pytest.log 2>&1; then
  echo "TESTS FAILED — discarding branch"; tail -6 /tmp/si-pytest.log
  git reset -q --hard origin/main; git checkout -q main; git branch -qD "$BRANCH" 2>/dev/null
  announce "⚠️ 每日自我改进 $DAY：Opus 提了改动但测试未过，已丢弃。"
  exit 1
fi
echo "tests green"

# 6. report-only / dry-run stop before pushing
if [ "$MODE" != "live" ]; then
  echo "$MODE: branch $BRANCH kept locally for inspection (no push/PR)"
  exit 0
fi

# 7. commit, push branch, open PR (never merges — the owner reviews)
git commit -q -m "auto: self-improvement from $DAY traces + chat (Opus 4.8)

Distilled by the daily self-improve job from run traces, chat friction, and
task failures. Full test suite green. Review before merge — not auto-merged.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
git push -q -u origin "$BRANCH"
BODY="Automated daily self-improvement (Opus 4.8) from the last 2 days of run traces,
chat friction, and task failures. Tests pass. **Review before merging — this is not
auto-merged.** Evidence and Opus's own summary are in \`~/.personal-agent/self-improve.log\`."
PR=$(gh pr create --base main --head "$BRANCH" \
       --title "auto: self-improvement $DAY (Opus 4.8)" --body "$BODY" 2>&1 | grep -oE 'https://[^ ]+' | head -1)
echo "PR: ${PR:-<creation failed>}"
[ -n "${PR:-}" ] && announce "🛠️ 每日自我改进 $DAY：Opus 提了一个改动、测试已过，PR 待你审阅：$PR"
} >>"$LOG" 2>&1
