"""Admin operations — operator tools, **not** a tenant surface (§10, §14).

These run from the host CLI (`assistant admin …`); possessing CLI/filesystem
access *is* the admin capability in the self-host trust model (§A.6), so the gate
is the shell, not a per-request role. They manage the roster and the deletion /
migration protocols that a tenant can never invoke.

Deletion is an **ordered protocol, not `rm -rf`** (§14): deactivate → cancel &
drain → revoke credentials → acquire locks → delete + remove entry last, so a
running job can't recreate files after deletion and nothing can re-authenticate
mid-delete.
"""

import fcntl
import logging
import shutil
import time
from pathlib import Path

from .config import DEFAULT_UID, Settings
from .jobs import JobQueue
from .registry import UserRegistry
from .uidsafe import user_data_dir, validate_uid

log = logging.getLogger("assistant")

# infra that lives at the deployment root, never moved into a user dir
_ROOT_INFRA = {"users", "shared", "users.yaml", "users.yaml.tmp"}


def _require_multi_tenant(settings: Settings) -> None:
    if settings.deployment_mode != "multi_tenant":
        raise ValueError("admin user commands require DEPLOYMENT_MODE=multi_tenant")


def add_user(settings: Settings, uid: str, display: str = "") -> str:
    """Register a new active user and create their (empty) data dir."""
    _require_multi_tenant(settings)
    reg = UserRegistry(settings.data_dir)
    reg.add_user(uid, display)
    user_data_dir(settings.data_dir / "users", uid).mkdir(parents=True, exist_ok=True)
    return f"registered {uid!r}"


def bind_channel(settings: Settings, uid: str, channel: str, external_id: str) -> str:
    """Bind a channel identity (`weixin` accountId / `email` mailbox) to a user."""
    _require_multi_tenant(settings)
    UserRegistry(settings.data_dir).bind_channel(uid, channel, external_id)
    return f"bound {channel}:{external_id} → {uid}"


def set_bridge_token(settings: Settings, token: str) -> str:
    """Store the hash of the (single) bridge↔daemon token; the bridge keeps the
    plaintext (§A.6). Refuses an empty token — an empty token is never valid."""
    _require_multi_tenant(settings)
    if not token:
        raise ValueError("refusing to set an empty bridge token")
    UserRegistry(settings.data_dir).set_bridge_token(token)
    return "bridge token set (hash stored; keep the plaintext in the bridge secret store)"


def list_users(settings: Settings) -> str:
    """Human-readable roster: uid, status, and bound channels."""
    _require_multi_tenant(settings)
    rows = UserRegistry(settings.data_dir).users()
    if not rows:
        return "(no users registered)"
    out = []
    for u in rows:
        chans = ", ".join(f"{c['channel']}:{c['id']}" for c in u.get("channels", [])) or "—"
        out.append(f"{u['uid']:<20} {u.get('status','?'):<9} {chans}")
    return "\n".join(out)


def list_shared_lessons(settings: Settings) -> str:
    """The shared (cross-user) lessons roster: id · created · rule — why
    (provenance). These render into EVERY user's prompts; retire what misfires."""
    _require_multi_tenant(settings)
    from .lessons_store import shared_store

    rows = shared_store(settings).active()
    if not rows:
        return "(no active shared lessons)"
    return "\n".join(f"[{l['id']}] {l.get('created', '?')}  {l['rule']}"
                     + (f"\n      why: {l['why']}" if l.get("why") else "")
                     for l in rows)


def retire_shared_lesson(settings: Settings, lesson_id: str) -> str:
    """Retire (never delete) one shared lesson — it leaves every user's prompts
    on their next turn."""
    _require_multi_tenant(settings)
    from .lessons_store import shared_store

    ok = shared_store(settings).retire(str(lesson_id))
    return (f"retired shared lesson {lesson_id}" if ok
            else f"no active shared lesson {lesson_id!r}")


def _flock_bounded(path: Path, timeout: float) -> "int | None":
    """Try to take an exclusive flock on `path` within `timeout` seconds; return
    the held fd, or None if it stayed contended (a stuck run — escalate to a
    daemon restart, §14)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = path.open("w")
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            if time.monotonic() >= deadline:
                fd.close()
                return None
            time.sleep(0.1)


def delete_user(settings: Settings, uid: str, export_to: "Path | None" = None,
                drain_timeout: float = 10.0) -> str:
    """Delete a user via the ordered §14 protocol (never a bare `rm -rf`).

    1. **Deactivate** (`status→deleting`) — no new request/job resolves here.
    2. **Cancel & drain** the user's jobs, then wait (bounded) for a running
       worker to yield at a checkpoint (threads aren't force-killed, §6).
    3. **Revoke credentials** — unbind channel ids so nothing re-authenticates.
    4. **Acquire** the per-user `write.lock` + `run.lock` (now uncontended).
    5. **Export?** then **delete** the (containment-checked) data dir and remove
       the registry entry **atomically last**.
    """
    _require_multi_tenant(settings)
    uid = validate_uid(uid)
    reg = UserRegistry(settings.data_dir)
    if reg.get(uid) is None:
        raise KeyError(uid)
    udir = user_data_dir(settings.data_dir / "users", uid)   # containment-checked

    # 1 — deactivate: routing/scheduling stop resolving to this uid immediately
    reg.set_status(uid, "deleting")

    # 2 — cancel & drain active work; bounded wait for a running worker to yield
    queue = JobQueue(settings.shared_dir)
    queue.cancel_user(uid)
    deadline = time.monotonic() + drain_timeout
    while time.monotonic() < deadline:
        running = [j for j in _user_jobs(queue, uid) if j["state"] == "running"]
        if not running:
            break
        time.sleep(0.2)

    # 3 — revoke credentials so nothing can re-authenticate mid-delete
    reg.clear_channels(uid)

    # 4 — acquire the per-user locks (uncontended once work has drained)
    wl = rl = None
    if udir.exists():
        wl = _flock_bounded(udir / "write.lock", drain_timeout)
        rl = _flock_bounded(udir / "run.lock", drain_timeout) if wl else None
        if wl is None or rl is None:
            if wl is not None:
                wl.close()
            # the user stays status=deleting (step 1) so a retry is safe
            raise TimeoutError(
                f"{uid}: a worker/run would not release its lock — restart the "
                "daemon (recovery requeues; cancelled jobs won't re-run) and retry")

    # 5 — export?, delete the data dir, then drop the registry entry LAST
    try:
        exported = ""
        if export_to is not None and udir.exists():
            shutil.make_archive(str(Path(export_to)), "gztar", root_dir=udir)
            exported = f" (exported to {export_to}.tar.gz)"
        if udir.exists():
            shutil.rmtree(udir)
        reg.remove_user(uid)                   # atomic, last
    finally:
        for held in (wl, rl):                  # release+close (files already gone)
            if held is not None:
                held.close()
    return f"deleted {uid!r}{exported}"


def _user_jobs(queue: JobQueue, uid: str) -> list[dict]:
    """All of a uid's jobs (helper for the drain wait)."""
    with queue._conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM jobs WHERE uid=?", (uid,)).fetchall()]


def migrate_single_user(settings: Settings, uid: str, dry_run: bool = False) -> str:
    """Reversibly fold a single-user `DATA_DIR` into `users/<uid>/` (§14).

    Moves every existing data entry (profile, events.db, sessions, …) under
    `DATA_DIR` into `users/<uid>/`, skipping the multi-user infra (`users/`,
    `shared/`, `users.yaml`), then registers the user active. Reversible: the move
    is a rename within the same DATA_DIR, so the inverse is moving the entries back
    and dropping the registry entry. `--dry-run` reports the plan only. The `.env`
    split (shared vs per-user secrets) is left to the operator — this touches only
    data, never secrets."""
    root = settings.data_dir
    validate_uid(uid)
    reg = UserRegistry(root)
    if reg.get(uid) is not None:
        raise ValueError(f"{uid!r} already registered — refusing to overwrite")
    movable = [p for p in root.iterdir() if p.name not in _ROOT_INFRA] if root.exists() else []
    dest = user_data_dir(root / "users", uid)
    plan = f"move {len(movable)} entries from {root} → {dest}: " \
           + ", ".join(sorted(p.name for p in movable))
    if dry_run:
        return "[dry-run] " + plan
    dest.mkdir(parents=True, exist_ok=True)
    for p in movable:
        shutil.move(str(p), str(dest / p.name))
    reg.add_user(uid, display=uid)
    return plan + f"\nregistered {uid!r} active — set channels with `admin bind-channel`"
