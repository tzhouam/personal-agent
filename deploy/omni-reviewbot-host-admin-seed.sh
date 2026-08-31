#!/usr/bin/env bash
set -Eeuo pipefail

CACHE=/home/ubuntu/.cache/omni-reviewbot-admin-seed
SELF=/home/ubuntu/.local/bin/omni-reviewbot-host-admin

if [[ ! ${SSH_ORIGINAL_COMMAND:-} =~ ^seed[[:space:]]+([0-9a-f]{64})$ ]]; then
  echo "ReviewBot host administration is not initialized" >&2
  exit 2
fi
EXPECTED_SHA=${BASH_REMATCH[1]}

install -d -m 700 "$CACHE"
STAGE=$(mktemp -d "$CACHE/seed.XXXXXX")
cleanup() {
  rc=$?
  trap - EXIT
  case "$STAGE" in "$CACHE"/seed.*) rm -rf -- "$STAGE" ;; esac
  exit "$rc"
}
trap cleanup EXIT

ARCHIVE=$STAGE/host-admin.tar.gz
cp /dev/stdin "$ARCHIVE"
ACTUAL_SHA=$(sha256sum "$ARCHIVE" | awk '{print $1}')
[[ $ACTUAL_SHA == "$EXPECTED_SHA" ]] || {
  echo "ReviewBot host-admin seed digest mismatch" >&2
  exit 1
}

python3 - "$ARCHIVE" "$STAGE" <<'PY'
import sys
import tarfile

archive_path, destination = sys.argv[1:]
with tarfile.open(archive_path, "r:gz") as archive:
    members = archive.getmembers()
    if len(members) != 1 or members[0].name != "omni-reviewbot-host-admin":
        raise SystemExit("seed archive must contain only omni-reviewbot-host-admin")
    member = members[0]
    if not member.isfile() or member.issym() or member.islnk() or member.isdev():
        raise SystemExit("seed host-admin entry must be a regular file")
    archive.extract(member, destination)
PY

bash -n "$STAGE/omni-reviewbot-host-admin"
install -m 700 "$STAGE/omni-reviewbot-host-admin" "$SELF.next"
mv -f "$SELF.next" "$SELF"
echo "ReviewBot host administration initialized"
