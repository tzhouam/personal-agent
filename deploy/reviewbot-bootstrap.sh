#!/usr/bin/env bash
set -Eeuo pipefail

APP=/home/ubuntu/project/omni-reviewbot
ROOT=/home/ubuntu/project/.omni-reviewbot
CACHE=/home/ubuntu/project/.cache/omni-reviewbot
RUNNER_ROOT=$ROOT/runner
UNIT_ROOT=/home/ubuntu/.config/systemd/user
SSH_ROOT=/home/ubuntu/.ssh
SOURCE_ROOT=/home/ubuntu/project/personal-agent
LOCK=$CACHE/reviewbot-bootstrap.lock
EDGE_FINGERPRINT='SHA256:BkHeQYU0TxsroAo+CAXkPHBndK9Jr8MwKlAo6QAyE2U'

export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}

status() {
  echo "reviewbot cutover status v1"
  if [[ -L $APP ]]; then
    printf 'app_target=%s\n' "$(readlink -f "$APP")"
  else
    printf 'app_target=%s\n' missing
  fi
  printf 'deploy_commit='; cat "$APP/.deploy-commit" 2>/dev/null || echo missing
  for unit in \
    omni-reviewbot-vllm-omni-watch.service \
    omni-reviewbot-vllm-omni-dashboard.service \
    omni-reviewbot-vllm-gr-watch.service \
    omni-reviewbot-vllm-gr-dashboard.service \
    omni-reviewbot-dashboard-tunnel.service \
    omni-reviewbot-github-runner.service
  do
    printf '%s=' "$unit"
    systemctl --user is-active "$unit" 2>/dev/null || true
  done
  for endpoint in \
    http://127.0.0.1:8765/api/status \
    http://127.0.0.1:8766/api/status
  do
    printf '%s=' "$endpoint"
    curl -fsS --max-time 10 "$endpoint" |
      python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("repo"), d.get("maintenance"))' || true
  done
}

prepare_tunnel_key() {
  install -d -m 700 "$SSH_ROOT"
  if [[ ! -f $SSH_ROOT/reviewbot-dashboard-tunnel ]]; then
    ssh-keygen -q -t ed25519 -N '' -C reviewbot-dashboard-tunnel \
      -f "$SSH_ROOT/reviewbot-dashboard-tunnel"
  fi
  chmod 600 "$SSH_ROOT/reviewbot-dashboard-tunnel"
  chmod 644 "$SSH_ROOT/reviewbot-dashboard-tunnel.pub"
  keyscan=$SSH_ROOT/reviewbot-dashboard-known_hosts.next
  ssh-keyscan -T 10 -t ed25519 43.153.173.195 > "$keyscan" 2>/dev/null
  observed=$(ssh-keygen -lf "$keyscan" | awk 'NR == 1 {print $2}')
  [[ $observed == "$EDGE_FINGERPRINT" ]] || {
    echo "dashboard edge host-key fingerprint mismatch" >&2
    exit 1
  }
  chmod 600 "$keyscan"
  mv -f "$keyscan" "$SSH_ROOT/reviewbot-dashboard-known_hosts"
  printf 'public_key='
  cat "$SSH_ROOT/reviewbot-dashboard-tunnel.pub"
  printf 'public_key_fingerprint='
  ssh-keygen -lf "$SSH_ROOT/reviewbot-dashboard-tunnel.pub" | awk '{print $2}'
}

if [[ $# -eq 1 && $1 == status ]]; then
  status
  exit 0
fi
if [[ $# -eq 1 && $1 == tunnel-key ]]; then
  [[ $(id -un) == ubuntu ]] || { echo "tunnel key preparation must run as ubuntu" >&2; exit 1; }
  prepare_tunnel_key
  exit 0
fi
if [[ $# -ne 2 || $1 != bootstrap || ! $2 =~ ^[0-9a-f]{40}$ ]]; then
  echo "usage: reviewbot-bootstrap bootstrap <40-character ReviewBot SHA>" >&2
  exit 2
fi
if [[ $(id -un) != ubuntu ]]; then
  echo "ReviewBot bootstrap must run as ubuntu" >&2
  exit 1
fi
REVIEWBOT_SHA=$2

install -d -m 700 "$CACHE" "$ROOT" "$ROOT/releases" "$ROOT/shared" "$UNIT_ROOT" "$SSH_ROOT"
exec 9>"$LOCK"
flock -w 3600 9

STAGE=$(mktemp -d "$CACHE/reviewbot-bootstrap.XXXXXX")
cleanup() {
  rc=$?
  trap - EXIT
  case "$STAGE" in "$CACHE"/reviewbot-bootstrap.*) rm -rf -- "$STAGE" ;; esac
  exit "$rc"
}
trap cleanup EXIT

OUTER=$STAGE/cutover-input.tar.gz
cp /dev/stdin "$OUTER"
mkdir -p "$STAGE/input"
python3 - "$OUTER" "$STAGE/input" <<'PY'
import sys
import tarfile
from pathlib import PurePosixPath

archive_path, destination = sys.argv[1:]
required = {
    "migration.tar.gz",
    "release.tar.gz",
    "actions-runner.tar.gz",
    "actions-runner.sha256",
    "runner-token",
}
seen = set()
with tarfile.open(archive_path, "r:gz") as archive:
    for member in archive.getmembers():
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts:
            raise SystemExit(f"unsafe outer path: {member.name}")
        if member.isdir() and path in (PurePosixPath("."),):
            continue
        if not member.isfile() or len(path.parts) != 1 or path.name not in required:
            raise SystemExit(f"unsupported outer entry: {member.name}")
        if path.name in seen:
            raise SystemExit(f"duplicate outer entry: {member.name}")
        seen.add(path.name)
    if seen != required:
        raise SystemExit(f"incomplete cutover input: {sorted(required - seen)}")
    archive.extractall(destination)
PY

expected_runner_digest=$(tr -d '[:space:]' < "$STAGE/input/actions-runner.sha256")
[[ $expected_runner_digest =~ ^[0-9a-f]{64}$ ]] || {
  echo "invalid Actions runner digest" >&2
  exit 1
}
actual_runner_digest=$(sha256sum "$STAGE/input/actions-runner.tar.gz" | awk '{print $1}')
[[ $actual_runner_digest == "$expected_runner_digest" ]] || {
  echo "Actions runner digest mismatch" >&2
  exit 1
}
RUNNER_TOKEN=$(tr -d '\r\n' < "$STAGE/input/runner-token")
[[ $RUNNER_TOKEN =~ ^[A-Za-z0-9_-]{20,200}$ ]] || {
  echo "invalid repository runner registration token" >&2
  exit 1
}

mkdir -p "$STAGE/release" "$STAGE/migration"
python3 - "$STAGE/input/release.tar.gz" "$STAGE/release" "$REVIEWBOT_SHA" <<'PY'
import json
import sys
import tarfile
from pathlib import PurePosixPath

archive_path, destination, expected_sha = sys.argv[1:]
with tarfile.open(archive_path, "r:gz") as archive:
    for member in archive.getmembers():
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts:
            raise SystemExit(f"unsafe release path: {member.name}")
        if member.issym() or member.islnk() or member.isdev():
            raise SystemExit(f"unsupported release entry: {member.name}")
        if not (member.isdir() or member.isfile()):
            raise SystemExit(f"unsupported release entry type: {member.name}")
    archive.extractall(destination)
manifest = json.load(open(f"{destination}/manifest.json", encoding="utf-8"))
if manifest.get("reviewbot", {}).get("git_sha") != expected_sha:
    raise SystemExit("paired release SHA does not match bootstrap command")
PY
python3 - "$STAGE/input/migration.tar.gz" "$STAGE/migration" <<'PY'
import sys
import tarfile
from pathlib import PurePosixPath

archive_path, destination = sys.argv[1:]
allowed_roots = {"manifest.json", "app", "tunnel"}
with tarfile.open(archive_path, "r:gz") as archive:
    for member in archive.getmembers():
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts:
            raise SystemExit(f"unsafe migration path: {member.name}")
        if not path.parts or path.parts[0] not in allowed_roots:
            raise SystemExit(f"unsupported migration root: {member.name}")
        if member.issym() or member.islnk() or member.isdev():
            raise SystemExit(f"unsupported migration entry: {member.name}")
        if not (member.isdir() or member.isfile()):
            raise SystemExit(f"unsupported migration entry type: {member.name}")
    archive.extractall(destination)
PY

for required in \
  "$STAGE/release/app/deploy/personal-agent/bootstrap.sh" \
  "$STAGE/release/app/deploy/server-deploy.sh" \
  "$STAGE/migration/manifest.json" \
  "$STAGE/migration/app/.env" \
  "$STAGE/migration/app/.env.vllm-gr" \
  "$STAGE/migration/app/state/reviewbot.db" \
  "$STAGE/migration/app/state-vllm-gr/reviewbot.db"
do
  [[ -f $required ]] || { echo "missing cutover input: $required" >&2; exit 1; }
done

python3 - "$STAGE/migration/manifest.json" <<'PY'
import json
import sys

manifest = json.load(open(sys.argv[1], encoding="utf-8"))
required = {"schema_version", "migration_id", "source_commit", "created_at", "state"}
if set(manifest) != required or manifest.get("schema_version") != 1:
    raise SystemExit("unsupported migration manifest")
if not isinstance(manifest.get("state"), dict) or set(manifest["state"]) != {"state", "state-vllm-gr"}:
    raise SystemExit("incomplete migration state manifest")
PY

prepare_tunnel_key >/dev/null

DEPLOY_RELEASE=1
if [[ -L $APP ]]; then
  current_target=$(readlink -f "$APP")
  [[ $current_target == "$ROOT/releases/"* ]] || {
    echo "existing ReviewBot link points outside the managed release root" >&2
    exit 1
  }
  if [[ $(cat "$APP/.deploy-commit" 2>/dev/null || true) == "$REVIEWBOT_SHA" ]]; then
    DEPLOY_RELEASE=0
  elif [[ $(cat "$APP/.cutover-bootstrap" 2>/dev/null || true) != "$REVIEWBOT_SHA" ]]; then
    echo "existing ReviewBot release is not a resumable cutover" >&2
    exit 1
  fi
elif [[ -d $APP ]]; then
  [[ $(cat "$APP/.cutover-bootstrap" 2>/dev/null || true) == "$REVIEWBOT_SHA" ]] || {
    echo "existing ReviewBot directory is not a resumable cutover" >&2
    exit 1
  }
elif [[ -e $APP ]]; then
  echo "existing ReviewBot path is neither a directory nor a managed link" >&2
  exit 1
else
  INITIAL=$ROOT/bootstrap-$REVIEWBOT_SHA
  [[ ! -e $INITIAL ]] || { echo "bootstrap staging release already exists" >&2; exit 1; }
  mkdir -m 700 "$INITIAL"
  cp -a "$STAGE/release/app/." "$INITIAL/"
  cp -p "$STAGE/migration/app/.env" "$INITIAL/.env"
  cp -p "$STAGE/migration/app/.env.vllm-gr" "$INITIAL/.env.vllm-gr"
  cp -a "$STAGE/migration/app/state" "$INITIAL/state"
  cp -a "$STAGE/migration/app/state-vllm-gr" "$INITIAL/state-vllm-gr"
  chmod 600 "$INITIAL/.env" "$INITIAL/.env.vllm-gr"
  printf '%s\n' "$REVIEWBOT_SHA" > "$INITIAL/.cutover-bootstrap"
  mv "$INITIAL" "$APP"
fi

"$STAGE/release/app/deploy/personal-agent/bootstrap.sh"
if [[ $DEPLOY_RELEASE -eq 1 ]]; then
  "/home/ubuntu/.local/bin/omni-reviewbot-deploy" "$REVIEWBOT_SHA" \
    < "$STAGE/input/release.tar.gz"
fi

if [[ ! -e $RUNNER_ROOT ]]; then
  mkdir -m 700 "$RUNNER_ROOT"
  tar -xzf "$STAGE/input/actions-runner.tar.gz" -C "$RUNNER_ROOT"
  (
    cd "$RUNNER_ROOT"
    ./config.sh --unattended --replace \
      --url https://github.com/JiusiServe/omni-reviewbot \
      --token "$RUNNER_TOKEN" \
      --name Personal-Agent-ReviewBot \
      --labels personal-agent,reviewbot-deploy \
      --work _work
  )
elif [[ ! -f $RUNNER_ROOT/.runner ]]; then
  echo "existing ReviewBot runner directory is not configured" >&2
  exit 1
fi
RUNNER_TOKEN=
: > "$STAGE/input/runner-token"

install -m 644 "$SOURCE_ROOT/deploy/omni-reviewbot-github-runner.service" \
  "$UNIT_ROOT/omni-reviewbot-github-runner.service"
systemctl --user daemon-reload
systemctl --user enable --now omni-reviewbot-github-runner.service
systemctl --user restart omni-reviewbot-dashboard-tunnel.service

for unit in \
  omni-reviewbot-vllm-omni-watch.service \
  omni-reviewbot-vllm-omni-dashboard.service \
  omni-reviewbot-vllm-gr-watch.service \
  omni-reviewbot-vllm-gr-dashboard.service \
  omni-reviewbot-dashboard-tunnel.service \
  omni-reviewbot-github-runner.service
do
  systemctl --user is-active --quiet "$unit" || {
    systemctl --user status --no-pager "$unit" >&2 || true
    exit 1
  }
done
curl -fsS --max-time 15 http://127.0.0.1:8765/api/status >/dev/null
curl -fsS --max-time 15 http://127.0.0.1:8766/api/status >/dev/null
curl -fsS --max-time 20 https://review.43.153.173.195.nip.io/code_review/vllm_omni/api/status >/dev/null
curl -fsS --max-time 20 https://review.43.153.173.195.nip.io/code_review/vllm_gr/api/status >/dev/null
printf '%s\n' "$REVIEWBOT_SHA" > "$ROOT/bootstrap-complete"
status
