"""In-process worker pool that drains the durable job queue (§6).

Replaces the old detached-`Popen` model in `multi_tenant`: a small pool of daemon
threads `claim()` jobs and run them **in-process** as `Settings.for_user(uid)`,
so a job carries the authenticated uid of whoever enqueued it — no child process,
no `PERSONAL_AGENT_UID` env var, nothing forgeable.

Cancellation is **cooperative** (§6): a worker never force-kills a thread. Each
dispatch receives a `CancelToken`; long kinds call `token.check()` at checkpoints
(phase boundaries / per task step) and raise `Cancelled` to yield. Deletion (§14)
flags the job and `stop()` joins with a bounded timeout; a genuinely stuck worker
is escaped by a daemon restart (`recover()` requeues).
"""

import contextvars
import logging
import threading

from assistant.platform.config import Settings
from assistant.platform.jobs import GLOBAL_UID

log = logging.getLogger("assistant")


class Cancelled(Exception):
    """Raised by a dispatch when it observes its job was cancelled — the worker
    turns this into a `cancelled` terminal state (not a failure)."""


def _note_job_failure(settings, uid: str, kind: str, exc: BaseException) -> None:
    """Give a job that exhausted its retries an owner-visible trace.

    Until this existed, `jobs.db` was write-only from the owner's side: no
    D5 row, no push, no `admin jobs` surface — a failed daily run left nothing
    but a journal line, which is exactly what the durable-delivery work set out
    to eliminate for every *other* producer.

    Per-user jobs go through `OutboxDB.add_system_note`, so the failure joins
    the existing D5 surface and rides out on the next chat reply (ackable as
    `知道了 dfs<n>`) — no new schema, no new surface.

    A GLOBAL_UID job has no tenant to notify and runs under root `Settings`,
    where a system note would land in an outbox nobody reads. It gets a
    best-effort push on the operator's own ANNOUNCE_* channel instead. That
    push can legitimately fail (the WeChat context-token window is ~24h from
    the owner's last inbound message), so it is wrapped and the log line is
    unconditional — the journal stays the floor, never the only attempt.

    Never raises: this runs inside the worker's failure path, and a failure to
    *report* a failure must not take down the pool.
    """
    summary = f"后台任务失败: {kind} — {str(exc)[:120]}"
    try:
        if uid == GLOBAL_UID:
            from assistant.platform import notify

            # send_wechat never raises: it returns "sent" / "disabled …" /
            # "failed: …". Log the verdict so a shut push window or an
            # unconfigured ANNOUNCE_* is diagnosable rather than invisible.
            log.warning("global job %s failed → operator push: %s",
                        kind, notify.send_wechat(settings, f"⚠ {summary}"))
            return
        from assistant.platform.delivery import OutboxDB

        db = OutboxDB(settings.data_dir)
        try:
            db.add_system_note(summary)
        finally:
            db.close()
    except Exception:
        log.exception("job failure note could not be recorded (%s/%s)", uid, kind)


class CancelToken:
    """A job's cooperative-cancellation handle. `check()` at a checkpoint raises
    `Cancelled` if the queue has flagged this job."""

    __slots__ = ("_queue", "_job_id")

    def __init__(self, queue, job_id: int):
        self._queue = queue
        self._job_id = job_id

    def cancelled(self) -> bool:
        return self._queue.is_cancelled(self._job_id)

    def check(self) -> None:
        if self.cancelled():
            raise Cancelled()


# The kind → handler map lives in `agent.dispatch` (the per-owner work); the
# platform only defines the `Dispatch` type it consumes (see `.dispatch`) and
# receives a built map at the composition root. This keeps the runtime free of
# any agent import — dependency inversion at the job boundary.


def _default_settings_for(uid: str) -> Settings:
    """Per-user Settings for a job's uid — except the global sentinel: a
    deployment-global job (global_evolve / self_improve) runs under the ROOT
    Settings, cross-user by design, and must never materialize a users/<uid>
    dir (GLOBAL_UID can't even pass uid validation)."""
    if uid == GLOBAL_UID:
        return Settings()
    return Settings.for_user(uid)


class WorkerPool:
    """A fixed pool of daemon threads draining `queue`.

    `settings_for(uid)` builds the per-user `Settings` a job runs under (default:
    `Settings.for_user`, the isolation seam). `dispatch` is the required
    kind → handler map (`.dispatch.Dispatch`); the composition root passes
    `agent.dispatch.build_dispatch()`. An empty map is valid — every kind is then
    unknown and fails — but `None` is a wiring bug and is rejected."""

    def __init__(self, queue, settings_for=None, dispatch=None,
                 max_workers: int = 2, poll_interval: float = 1.0):
        if dispatch is None:
            raise ValueError("WorkerPool requires an explicit dispatch mapping "
                             "(agent.dispatch.build_dispatch())")
        self.queue = queue
        self.settings_for = settings_for or _default_settings_for
        self.dispatch = dispatch          # {} is valid (all kinds unknown → failed)
        self.max_workers = max_workers
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> "WorkerPool":
        """Requeue orphaned `running` jobs (crash recovery) and spawn the pool."""
        self.queue.recover()
        for i in range(self.max_workers):
            t = threading.Thread(target=self._loop, name=f"job-worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def _loop(self) -> None:
        """Claim → run → mark, until stopped. A claim miss backs off `poll_interval`
        so an idle pool doesn't spin."""
        while not self._stop.is_set():
            try:
                job = self.queue.claim()
            except Exception:
                log.exception("job claim failed")
                self._stop.wait(self.poll_interval)
                continue
            if job is None:
                self._stop.wait(self.poll_interval)
                continue
            self._run_one(job)

    def _run_one(self, job: dict) -> None:
        """Execute one claimed job under its own uid, mapping the outcome to a
        terminal state. A cancelled job yields cleanly; any other exception
        requeues (attempts remaining) or fails — a bad job never kills the pool.

        Each job runs inside a **fresh `contextvars` context** (a copy of the
        loop's clean one): worker threads are reused across jobs of *different*
        users, and the ContextVar-scoped tracer set by one job (`tracing.init`
        inside `run()`) would otherwise survive on the thread and write the NEXT
        job's LLM spans into the previous user's `runs/<id>/trace.jsonl` — a
        cross-tenant trace leak. Sets inside `ctx.run` never propagate back, so
        the loop context stays clean for every subsequent job."""
        jid, uid, kind = job["id"], job["uid"], job["kind"]
        if self.queue.is_cancelled(jid):
            return self.queue.mark(jid, "cancelled")
        fn = self.dispatch.get(kind)
        if fn is None:
            log.error("job %s: unknown kind %r", jid, kind)
            return self.queue.mark(jid, "failed")
        settings = None
        try:
            settings = self.settings_for(uid)
            ctx = contextvars.copy_context()
            ctx.run(fn, settings, job.get("args", {}), CancelToken(self.queue, jid))
            self.queue.mark(jid, "done")
        except Cancelled:
            log.info("job %s (%s/%s) cancelled", jid, uid, kind)
            self.queue.mark(jid, "cancelled")
        except Exception as exc:
            state = self.queue.fail_or_retry(jid)
            log.exception("job %s (%s/%s) errored → %s", jid, uid, kind, state)
            # Only a TERMINAL failure reaches the owner: a job with attempts
            # left will run again shortly, and one note per retry would train
            # the owner to ignore the surface. `settings` is None only when
            # settings_for itself raised — nothing to write it to, and the
            # log.exception above is already the record.
            if state == "failed" and settings is not None:
                _note_job_failure(settings, uid, kind, exc)

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the pool to stop and join with a bounded timeout (a worker mid-job
        finishes its current step; it is never force-killed)."""
        self._stop.set()
        for t in self._threads:
            t.join(timeout=timeout)
