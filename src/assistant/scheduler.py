"""Daily-pipeline fan-out scheduler for N users (doc/DESIGN_MULTI_USER.md §12).

Replaces the single 07:00 cron that ran one owner's `assistant run`. In
`multi_tenant` it iterates `registry.active()` and **enqueues one `run` job per
user** on the durable queue; the worker pool (§6) drains them with per-user
`run.lock`/`state.json` isolation and a bounded concurrency cap, so the pool size
is the natural stagger and one user's broken collector can't stall another's.

Enqueue is **idempotent per day** (dedupe key `uid:run:<date>`), so a cron that
double-fires — or a manual re-trigger — never double-runs a user. `single_user`
keeps the legacy direct cron (`assistant run`); this fan-out is a multi-tenant
concern (§6.1).
"""

import logging
from datetime import datetime

from .config import Settings
from .jobs import JobQueue
from .registry import UserRegistry

log = logging.getLogger("assistant")


def enqueue_daily_runs(settings: Settings, day: str | None = None) -> list[str]:
    """Enqueue the daily `run` for every active user; return the uids enqueued.

    `settings` is the **deployment-root** Settings (its `data_dir` is the root, so
    the registry and shared queue resolve correctly). A uid already queued/running
    for `day` is skipped by the queue's dedupe. Non-multi_tenant is a no-op (the
    legacy cron handles the single owner)."""
    if settings.deployment_mode != "multi_tenant":
        return []
    day = day or datetime.now().strftime("%Y-%m-%d")
    reg = UserRegistry(settings.data_dir)
    queue = JobQueue(settings.shared_dir)
    enqueued: list[str] = []
    for uid in reg.active():
        if queue.enqueue(uid, "run", {}, dedupe_key=f"{uid}:run:{day}") is not None:
            enqueued.append(uid)
    log.info("scheduler: enqueued daily run for %d/%d active users on %s",
             len(enqueued), len(reg.active()), day)
    return enqueued
