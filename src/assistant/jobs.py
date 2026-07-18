"""Durable background-job queue — the multi-tenant replacement for detached
CLI `Popen`s (doc/DESIGN_MULTI_USER.md §6).

One **deployment-global** SQLite queue at `shared/jobs.db` (a single cross-user
view is what fairness/scheduling need — it is *not* per-user `events.db`). Jobs
carry the **authenticated** uid of whoever enqueued them; a worker later runs
them in-process as `Settings.for_user(uid)`, so there is no forgeable
`PERSONAL_AGENT_UID` env var and no subprocess trust boundary.

Guarantees:
- **Durable & crash-recoverable.** `recover()` on startup requeues jobs left
  `running` by a dead worker (idempotent kinds) — the queue survives a restart.
- **At-least-once, deduped *enqueue*.** A `dedupe_key` (`uid+kind+date`) blocks a
  duplicate *enqueue*, but a crash after a side effect but before `done` can still
  replay — so **side effects** are guarded separately by `DeliveryLedger`, not the
  queue. Duplicate delivery remains *possible* (ledger-write-after-send race); we
  do not claim exactly-once.
- **Per-user fairness.** `claim()` hands out at most one *running* pipeline job per
  uid (the per-user `run.lock` is the hard backstop) and rotates across users.
- **Cooperative cancellation.** `request_cancel` flags jobs; a worker checks
  `is_cancelled` at checkpoints and yields — Python threads are never force-killed.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# kinds where at most one may run per uid at a time (they take the per-user
# run.lock anyway; enforcing it here avoids pointless lock-contention churn).
_SINGLETON_KINDS = ("run", "run_phase")

# Sentinel uid for deployment-global jobs (weekly global evolve / self-improve,
# §12b). Deliberately FAILS `uidsafe.validate_uid` (underscores) so it can never
# be registered, resolved to a user, or turned into a users/<uid> path — the
# worker's default settings_for special-cases it to the deployment-ROOT Settings.
GLOBAL_UID = "__global__"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    uid         TEXT NOT NULL,
    kind        TEXT NOT NULL,
    args_json   TEXT NOT NULL DEFAULT '{}',
    state       TEXT NOT NULL DEFAULT 'queued',   -- queued|running|done|failed|cancelled
    attempts    INTEGER NOT NULL DEFAULT 0,
    cancelled   INTEGER NOT NULL DEFAULT 0,        -- cooperative-cancel flag
    dedupe_key  TEXT,
    created_ts  TEXT NOT NULL,
    updated_ts  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, id);
"""

_ACTIVE = ("queued", "running")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobQueue:
    """SQLite-backed durable job queue at `shared_dir/jobs.db`.

    Thread-safe by opening a short-lived connection per operation (WAL +
    `busy_timeout`), so many worker threads and the enqueuing HTTP handlers never
    share a cursor. Low-volume by design — one household's background work."""

    def __init__(self, shared_dir: Path, max_attempts: int = 3):
        self.dir = Path(shared_dir)
        self.path = self.dir / "jobs.db"
        self.max_attempts = max_attempts
        self.dir.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    @contextmanager
    def _conn(self):
        """A committing connection for one operation. `BEGIN IMMEDIATE` is used
        explicitly by `claim` for the atomic pick; everything else autocommits on
        the context exit."""
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ── enqueue ──────────────────────────────────────────────────────
    def enqueue(self, uid: str, kind: str, args: dict | None = None,
                dedupe_key: str | None = None,
                dedupe_scope: str = "all") -> int | None:
        """Add a `queued` job for `uid`. If `dedupe_key` matches an existing
        job, do nothing and return None. Two scopes:

        - ``"all"`` (default): matches ANY existing job — including a
          completed/failed/cancelled one. Keys are date/week-scoped
          (`uid:run:<day>`), so this means "at most once per period": a
          finished daily run must NOT be re-enqueued by the next poll tick
          (that was a live runaway — the pipeline looped all day once the
          first run completed), and a failed/cancelled one stays down for its
          period (retries happen at the attempt level, not by re-enqueue; a
          manual re-trigger passes no dedupe key).
        - ``"active"``: matches only `queued`/`running` jobs — "at most one
          in flight": a task-record resume (`uid:task_resume:<task-id>`)
          must be re-enqueueable after a dead/failed attempt, while an alive
          one still dedupes, so at most one worker ever holds a record.

        Returns the new job id otherwise."""
        payload = json.dumps(args or {}, ensure_ascii=False)
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            if dedupe_key:
                query = "SELECT id FROM jobs WHERE dedupe_key=?"
                if dedupe_scope == "active":
                    query += " AND state IN ('queued','running')"
                dup = c.execute(query, (dedupe_key,)).fetchone()
                if dup:
                    return None
            now = _now()
            cur = c.execute(
                "INSERT INTO jobs (uid, kind, args_json, state, dedupe_key, "
                "created_ts, updated_ts) VALUES (?,?,?, 'queued', ?, ?, ?)",
                (uid, kind, payload, dedupe_key, now, now))
            return cur.lastrowid

    # ── claim / complete (worker side) ───────────────────────────────
    def claim(self) -> dict | None:
        """Atomically take the fairest runnable `queued` job → `running`, or None.

        Fairness (§6): candidates are ordered by **how many jobs the uid already
        has running** (fewest first), then age — so a user with an idle slot is
        served before a user monopolizing workers with a deep queue, for *every*
        kind, not just pipelines. Additionally a `_SINGLETON_KINDS` job whose uid
        already has one running is skipped outright (≤1 pipeline per user). The
        pick + state flip run in one `BEGIN IMMEDIATE` transaction so two workers
        never grab the same row."""
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            running = {r["uid"]: r["n"] for r in c.execute(
                "SELECT uid, COUNT(*) n FROM jobs WHERE state='running' "
                "GROUP BY uid").fetchall()}
            pipeline_busy = {r["uid"] for r in c.execute(
                "SELECT DISTINCT uid FROM jobs WHERE state='running' "
                "AND kind IN (%s)" % ",".join("?" * len(_SINGLETON_KINDS)),
                _SINGLETON_KINDS).fetchall()}
            queued = c.execute(
                "SELECT * FROM jobs WHERE state='queued' ORDER BY id").fetchall()
            for row in sorted(queued, key=lambda r: (running.get(r["uid"], 0), r["id"])):
                if row["kind"] in _SINGLETON_KINDS and row["uid"] in pipeline_busy:
                    continue                      # this user already runs a pipeline
                c.execute("UPDATE jobs SET state='running', attempts=attempts+1, "
                          "updated_ts=? WHERE id=?", (_now(), row["id"]))
                job = dict(row)
                job.update(state="running", attempts=row["attempts"] + 1,
                           args=json.loads(row["args_json"]))
                return job
            return None

    def mark(self, job_id: int, state: str) -> None:
        """Set a job's terminal (or intermediate) state with a fresh timestamp."""
        with self._conn() as c:
            c.execute("UPDATE jobs SET state=?, updated_ts=? WHERE id=?",
                      (state, _now(), job_id))

    def fail_or_retry(self, job_id: int) -> str:
        """A worker errored on `job_id`: requeue it if attempts remain, else mark
        `failed`. Returns the resulting state."""
        with self._conn() as c:
            row = c.execute("SELECT attempts FROM jobs WHERE id=?", (job_id,)).fetchone()
            state = "queued" if row and row["attempts"] < self.max_attempts else "failed"
            c.execute("UPDATE jobs SET state=?, updated_ts=? WHERE id=?",
                      (state, _now(), job_id))
            return state

    # ── cancellation (cooperative) ───────────────────────────────────
    def request_cancel(self, job_id: int) -> None:
        """Flag one job for cooperative cancellation (the worker yields at its next
        checkpoint). A still-`queued` job is cancelled immediately."""
        with self._conn() as c:
            c.execute("UPDATE jobs SET cancelled=1, updated_ts=? WHERE id=?",
                      (_now(), job_id))
            c.execute("UPDATE jobs SET state='cancelled', updated_ts=? "
                      "WHERE id=? AND state='queued'", (_now(), job_id))

    def cancel_user(self, uid: str) -> int:
        """Flag *all* of a user's active jobs for cancellation (used by the
        deletion protocol, §14). Returns how many were flagged."""
        with self._conn() as c:
            cur = c.execute(
                "UPDATE jobs SET cancelled=1, updated_ts=? WHERE uid=? "
                "AND state IN ('queued','running')", (_now(), uid))
            c.execute("UPDATE jobs SET state='cancelled', updated_ts=? "
                      "WHERE uid=? AND state='queued'", (_now(), uid))
            return cur.rowcount

    def is_cancelled(self, job_id: int) -> bool:
        """Whether `job_id` has been flagged — a worker calls this at checkpoints."""
        with self._conn() as c:
            row = c.execute("SELECT cancelled FROM jobs WHERE id=?", (job_id,)).fetchone()
            return bool(row and row["cancelled"])

    # ── recovery / introspection ─────────────────────────────────────
    def recover(self) -> int:
        """Startup recovery: jobs left `running` by a worker that died (no live
        thread can exist right after process start) go back to `queued`. Returns
        how many were requeued. Idempotent kinds re-run cleanly; a `run` re-enters
        via its own resume checkpoint."""
        with self._conn() as c:
            cur = c.execute("UPDATE jobs SET state='queued', updated_ts=? "
                            "WHERE state='running'", (_now(),))
            return cur.rowcount

    def counts(self) -> dict:
        """`{state: n}` across the queue — for tests, `/status`, and metrics."""
        with self._conn() as c:
            return {r["state"]: r["n"] for r in c.execute(
                "SELECT state, COUNT(*) n FROM jobs GROUP BY state").fetchall()}

    def get(self, job_id: int) -> dict | None:
        """Full job row as a dict (with decoded `args`), or None."""
        with self._conn() as c:
            row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return None
            job = dict(row)
            job["args"] = json.loads(row["args_json"])
            return job


class DeliveryLedger:
    """Per-user outbox ledger guarding **side effects** against at-least-once
    replay (§6). Usage is **claim-before-send**: `mark_delivered` atomically
    claims a `(kind, day)` (False = already claimed → skip the send), the send
    runs, and a send *failure* calls `unmark` so a retry/resume can re-claim.
    Lives in the user's own `data_dir` (`delivery.db`) — a delivery record is
    per-user, not global.

    Residual risk (documented, not hidden): claim-before-send trades the
    duplicate window for a loss window — if the process dies between a
    *successful* send-failure and the `unmark`, that day's delivery stays
    claimed and is skipped until an operator clears it. Delivery is
    at-least-once minus replays, not exactly-once."""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "delivery.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as c:
            c.execute("CREATE TABLE IF NOT EXISTS delivered "
                      "(kind TEXT, day TEXT, ts TEXT, PRIMARY KEY (kind, day))")

    def was_delivered(self, kind: str, day: str) -> bool:
        """Whether `(kind, day)` was already sent."""
        with sqlite3.connect(self.path) as c:
            return c.execute("SELECT 1 FROM delivered WHERE kind=? AND day=?",
                             (kind, day)).fetchone() is not None

    def mark_delivered(self, kind: str, day: str) -> bool:
        """Record `(kind, day)` as delivered. Returns True if this call claimed it,
        False if it was already recorded (the caller should then **not** re-send).
        The `PRIMARY KEY` makes the claim atomic across concurrent workers."""
        with sqlite3.connect(self.path) as c:
            cur = c.execute("INSERT OR IGNORE INTO delivered (kind, day, ts) "
                            "VALUES (?,?,?)", (kind, day, _now()))
            return cur.rowcount == 1

    def unmark(self, kind: str, day: str) -> None:
        """Release a claim after a **failed** send so a retry/resume can re-claim
        the `(kind, day)` and actually deliver."""
        with sqlite3.connect(self.path) as c:
            c.execute("DELETE FROM delivered WHERE kind=? AND day=?", (kind, day))
