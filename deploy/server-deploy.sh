#!/usr/bin/env bash
set -Eeuo pipefail

APP=/home/ubuntu/project/personal-agent
CACHE=/home/ubuntu/.cache
LOCK="$CACHE/personal-agent-deploy.lock"
DEPLOY_RUNNER=/home/ubuntu/.local/bin/personal-agent-deploy

# One-time ReviewBot cutover commands share this repository's existing forced
# SSH key, but they never expose a shell.  The bootstrap command validates an
# exact paired release and migration payload; status is read-only.
if [[ ${SSH_ORIGINAL_COMMAND:-} =~ ^reviewbot-bootstrap[[:space:]]+([0-9a-f]{40})$ ]]; then
  exec "$APP/deploy/reviewbot-bootstrap.sh" bootstrap "${BASH_REMATCH[1]}"
elif [[ ${SSH_ORIGINAL_COMMAND:-} == "reviewbot-tunnel-key" ]]; then
  exec "$APP/deploy/reviewbot-bootstrap.sh" tunnel-key
elif [[ ${SSH_ORIGINAL_COMMAND:-} == "reviewbot-status" ]]; then
  exec "$APP/deploy/reviewbot-bootstrap.sh" status
elif [[ ${SSH_ORIGINAL_COMMAND:-} == "reviewbot-host-audit" ]]; then
  exec "$APP/deploy/reviewbot-bootstrap.sh" host-audit
fi

if [[ $# -eq 1 ]]; then
  DEPLOY_SHA=$1
elif [[ ${SSH_ORIGINAL_COMMAND:-} =~ ^deploy[[:space:]]+([0-9a-f]{40})$ ]]; then
  DEPLOY_SHA=${BASH_REMATCH[1]}
else
  echo "usage: deploy <40-character commit SHA>" >&2
  exit 2
fi
if [[ ! $DEPLOY_SHA =~ ^[0-9a-f]{40}$ ]]; then
  echo "invalid deploy commit SHA" >&2
  exit 2
fi

mkdir -p "$CACHE"
chmod 700 "$CACHE"
exec 9>"$LOCK"
flock -w 3600 9

cd "$APP"
if [[ -n $(git status --porcelain --untracked-files=no) ]]; then
  echo "refusing deployment: the server checkout has tracked changes" >&2
  git status --short --untracked-files=no >&2
  exit 1
fi

OLD_SHA=$(git rev-parse HEAD)
git fetch --quiet --prune origin main
FETCHED_SHA=$(git rev-parse FETCH_HEAD)
if [[ $FETCHED_SHA != "$DEPLOY_SHA" ]]; then
  echo "refusing deployment: origin/main is $FETCHED_SHA, not $DEPLOY_SHA" >&2
  exit 1
fi
if ! git merge-base --is-ancestor "$OLD_SHA" "$DEPLOY_SHA"; then
  echo "refusing deployment: server HEAD $OLD_SHA is not an ancestor of $DEPLOY_SHA" >&2
  echo "publish or reconcile the server-only commits before deploying" >&2
  exit 1
fi

ROLLBACK=0
cleanup() {
  rc=$?
  trap - EXIT
  if [[ $rc -ne 0 && $ROLLBACK -eq 1 ]]; then
    echo "deployment failed; restoring $OLD_SHA" >&2
    git reset --hard "$OLD_SHA" || true
    "$APP/.venv/bin/python" -m pip install -e "$APP" || true
    "$APP/.venv/bin/assistant" reboot || true
  fi
  exit "$rc"
}
trap cleanup EXIT

ROLLBACK=1
git reset --hard "$DEPLOY_SHA"
"$APP/.venv/bin/python" -m compileall -q "$APP/src"
"$APP/.venv/bin/python" -m pip install -e "$APP"
"$APP/.venv/bin/assistant" reboot
printf '%s\n' "$DEPLOY_SHA" > "$APP/.deployed-commit"

# Upgrade the forced-command runner only after the new release is healthy.
install -m 700 "$APP/deploy/server-deploy.sh" "$DEPLOY_RUNNER.next"
mv -f "$DEPLOY_RUNNER.next" "$DEPLOY_RUNNER"

ROLLBACK=0
echo "deployed $DEPLOY_SHA"
