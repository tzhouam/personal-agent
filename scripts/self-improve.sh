#!/usr/bin/env bash
# Self-improvement: Opus 4.8 reads recent traces + chat/task history (ALL active
# users in multi_tenant — mutually authorized) and makes small, test-backed
# improvements to the agent, delivered as a reviewable PR. Scheduled weekly by
# the multi-tenant job queue (worker kind `self_improve`, §12b layer 3); can be
# run by hand any time.
#
#   scripts/self-improve.sh              # live: worktree → Opus → tests → push → PR
#   scripts/self-improve.sh report-only  # analyze + edit in the worktree, NO push/PR
#   scripts/self-improve.sh dry-run      # analyze only, Opus makes NO edits
#   SELF_IMPROVE_DAYS=7 …                # evidence window (default 2)
#
# Isolation: ALL work happens in a throwaway git worktree under the shared dir —
# never the live checkout — so it can't disturb the running daemon or wipe
# uncommitted edits. Safety: refuses any change touching .env/.git/credentials;
# gates every change on the full test suite (run against the worktree via
# PYTHONPATH); discards the branch if red; opens a PR (NEVER auto-merged — the
# human-review gate for self-modifying changes). Opus 4.8 is reached by clearing
# the ANTHROPIC_* proxy env so the CLI uses the logged-in Anthropic account, not
# the DeepSeek/MiMo endpoint.
set -uo pipefail
export TZ="${PERSONAL_AGENT_TZ:-Asia/Hong_Kong}"

REPO=/rebase/personal-agent
DATA="${HOME}/.personal-agent"
SI="$DATA/shared/self-improve"   # deployment-global artifacts (log/brief/worktree/lock)
PY=/rebase/.venv/bin/python
LOG="$SI/self-improve.log"
BRIEF="$SI/self-improve-brief.md"
DAY=$(date +%F)
DAYS="${SELF_IMPROVE_DAYS:-2}"
BRANCH="auto/improve-$DAY"
WT="$SI/self-improve-wt"
MODE="${1:-live}"

mkdir -p "$SI"
exec 9>"$SI/self-improve.lock"
flock -n 9 || { echo "$(date -Is) another self-improve run holds the lock" >>"$LOG"; exit 0; }

cleanup() { cd "$REPO" 2>/dev/null && git worktree remove --force "$WT" 2>/dev/null; }
trap cleanup EXIT

announce() {  # best-effort WeChat ping (never fails the job)
  "$PY" - "$1" <<'PY' >>"$LOG" 2>&1 || true
import sys
from assistant.config import Settings
from assistant.notify import send_wechat
send_wechat(Settings(), sys.argv[1])
PY
}

{
echo "=== $(date -Is) self-improve ($MODE) ==="

# 1. evidence first (per-user in multi_tenant); skip the (costly) Opus call on
#    a quiet window
"$PY" "$REPO/scripts/self_improve_evidence.py" "$DAYS" > "$BRIEF" 2>>"$LOG"
if [ ! -s "$BRIEF" ]; then
  echo "no friction/perf signals in the last $DAYS days — nothing to improve"; exit 0
fi
echo "evidence: $(wc -l < "$BRIEF") lines"

# 2. fresh isolated worktree off origin/main (never the live checkout)
git -C "$REPO" fetch -q origin || { echo "fetch failed"; exit 1; }
git -C "$REPO" worktree remove --force "$WT" 2>/dev/null || true
rm -rf "$WT"
git -C "$REPO" worktree add -q -B "$BRANCH" "$WT" origin/main || { echo "worktree add failed"; exit 1; }
cd "$WT"

# 3. Opus 4.8 (env cleared so the CLI uses the Anthropic login, not the proxy)
PROMPT="$(cat "$WT/scripts/self_improve_prompt.md")

ADDITIONAL HARD RULE (multi-user privacy): the evidence below may reference
several users (## user <uid> sections). Users authorized using it to improve
the agent — but NEVER quote personal content (names, message text, amounts,
events) into code, comments, tests, commit messages, or PR text. Only the
abstracted engineering lesson may appear in the change.

--- EVIDENCE ---
$(cat "$BRIEF")"
[ "$MODE" = "dry-run" ] && PROMPT="REPORT ONLY — analyze and explain, but do NOT edit any files.

$PROMPT"

env -u ANTHROPIC_API_KEY -u ANTHROPIC_BASE_URL -u ANTHROPIC_MODEL -u ANTHROPIC_AUTH_TOKEN \
    -u ANTHROPIC_DEFAULT_HAIKU_MODEL -u ANTHROPIC_DEFAULT_SONNET_MODEL -u ANTHROPIC_DEFAULT_OPUS_MODEL \
    timeout 1800 claude -p "$PROMPT" \
      --model opus --max-turns 80 --permission-mode acceptEdits --add-dir "$WT" 2>&1 | tail -60

# 4. evaluate the diff (inside the worktree)
git -C "$WT" add -A
if git -C "$WT" diff --cached --quiet; then
  echo "Opus proposed no code changes"; exit 0
fi
if git -C "$WT" diff --cached --name-only | grep -qE '(^|/)\.env|\.credentials|/secrets|(^|/)\.git/'; then
  echo "ABORT: a proposed change touched a sensitive path"; git -C "$WT" diff --cached --name-only; exit 1
fi
echo "changed files:"; git -C "$WT" diff --cached --name-only | sed 's/^/  /'

# 5. tests must pass — run against the worktree's code, not the -e install
if ! ( cd "$WT" && PYTHONPATH="$WT/src" "$PY" -m pytest test/ -q >/tmp/si-pytest.log 2>&1 ); then
  echo "TESTS FAILED — discarding"; tail -6 /tmp/si-pytest.log
  announce "⚠️ 自我改进 $DAY：Opus 提了改动但测试未过，已丢弃。"; exit 1
fi
echo "tests green"

# 6. report-only / dry-run stop before pushing
if [ "$MODE" != "live" ]; then
  echo "$MODE: worktree $WT kept (remove with: git -C $REPO worktree remove --force $WT)"
  trap - EXIT   # keep the worktree for inspection
  exit 0
fi

# 7. commit, push branch, open PR (never merges — the owner reviews)
git -C "$WT" commit -q -m "auto: self-improvement from $DAY traces + chat (Opus 4.8)

Distilled by the self-improve job from run traces, chat friction, and task
failures across all users. Full test suite green. Review before merge — not
auto-merged.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
git -C "$WT" push -q -u origin "$BRANCH"
BODY="Automated self-improvement (Opus 4.8) from the last $DAYS days of run traces,
chat friction, and task failures across all active users. Tests pass.
**Review before merging — not auto-merged.**
Evidence and Opus's own summary are in \`~/.personal-agent/shared/self-improve/self-improve.log\`."
PR=$(gh pr create --repo tzhouam/personal-agent --base main --head "$BRANCH" \
       --title "auto: self-improvement $DAY (Opus 4.8)" --body "$BODY" 2>&1 | grep -oE 'https://[^ ]+' | head -1)
echo "PR: ${PR:-<creation failed>}"
[ -n "${PR:-}" ] && announce "🛠️ 自我改进 $DAY：Opus 提了一个改动、测试已过，PR 待你审阅：$PR"
} >>"$LOG" 2>&1
