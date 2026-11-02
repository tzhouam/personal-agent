"""Durable runtime facts with atomic claims instead of in-memory queue ownership.

The database is scoped to one owner's data directory. Connections are short-lived
so the same store is safe across worker threads and independently started CLIs.
Transactions never span model calls or subprocesses.
"""

from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import sqlite3
import time
import uuid

from assistant.platform.runtime.contracts import ChannelEvent
from assistant.platform.secrets import mask_secrets


RUN_STATES = frozenset({"queued", "running", "awaiting_approval", "done", "failed",
                        "cancelled", "interrupted"})
_TERMINAL = frozenset({"done", "failed", "cancelled", "interrupted"})
_RUN_FIELDS = frozenset({"status", "report", "error", "backend_session_id", "evidence",
                         "artifacts", "verified", "evidence_path", "started_at",
                         "finished_at", "cancel_requested"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
    metadata TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_session ON messages(session, id);
CREATE TABLE IF NOT EXISTS ingress (
    event_key TEXT PRIMARY KEY, envelope TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'accepted', reply TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
    kind TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_run ON run_events(run_id, id);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY, session TEXT NOT NULL, tool TEXT NOT NULL,
    args TEXT NOT NULL, fingerprint TEXT NOT NULL,
    expires_at REAL NOT NULL, consumed_at REAL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    name TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE,
    verification TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, project TEXT NOT NULL, workspace TEXT NOT NULL,
    goal TEXT NOT NULL, session TEXT NOT NULL, kind TEXT NOT NULL,
    status TEXT NOT NULL, report TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '', backend_session_id TEXT,
    evidence TEXT NOT NULL DEFAULT '[]', artifacts TEXT NOT NULL DEFAULT '[]',
    verification TEXT NOT NULL DEFAULT '[]', verified INTEGER NOT NULL DEFAULT 0,
    evidence_path TEXT NOT NULL DEFAULT '', cancel_requested INTEGER NOT NULL DEFAULT 0,
    attempt INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, started_at TEXT, finished_at TEXT
);
CREATE INDEX IF NOT EXISTS runs_queue ON runs(status, created_at);
"""


def _now() -> str:
    """Use sortable UTC timestamps in durable audit rows."""
    return datetime.now(timezone.utc).isoformat()


def _json(value) -> str:
    """Canonical JSON also binds approvals to values independent of key order."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _fingerprint(session: str, tool: str, args: dict) -> str:
    """Bind a grant to this conversation and exact tool arguments."""
    return hashlib.sha256(_json([session, tool, args]).encode()).hexdigest()


class RuntimeStore:
    """One owner's conversations, ingress receipts, approvals and project runs."""

    def __init__(self, data_dir: Path):
        """Create only new runtime storage; legacy stores are never migrated."""
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "runtime.sqlite3"
        if self.path.is_symlink():
            raise ValueError("runtime database must not be a symlink")
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
        self.path.chmod(0o600)

    @contextmanager
    def _connection(self, *, write: bool = False):
        """Keep every write transaction short and serialized by SQLite."""
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=15000")
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if write:
                conn.commit()
        except BaseException:
            if write:
                conn.rollback()
            raise
        finally:
            conn.close()

    def add_message(self, session: str, role: str, content: str, **metadata) -> dict:
        """Append an exchange without retaining pasted credential-shaped values."""
        stamp = _now()
        content = mask_secrets(content)
        encoded = mask_secrets(_json(metadata))
        with self._connection(write=True) as conn:
            row = conn.execute(
                "INSERT INTO messages(session,role,content,metadata,created_at) "
                "VALUES (?,?,?,?,?)", (session, role, content, encoded, stamp))
            return {"id": row.lastrowid, "session": session, "role": role,
                    "content": content, "metadata": json.loads(encoded), "created_at": stamp}

    def messages(self, session: str, limit: int = 20) -> list[dict]:
        """Return the most recent messages in chronological prompt order."""
        limit = min(max(int(limit), 0), 1000)
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM messages WHERE session=? "
                                "ORDER BY id DESC LIMIT ?", (session, limit)).fetchall()
        return [{**dict(row), "metadata": json.loads(row["metadata"])}
                for row in reversed(rows)]

    def accept_event(self, event: ChannelEvent) -> bool:
        """Persist the entire accepted envelope before declaring it deduplicated."""
        stamp = _now()
        with self._connection(write=True) as conn:
            inserted = conn.execute(
                "INSERT OR IGNORE INTO ingress(event_key,envelope,created_at,updated_at) "
                "VALUES (?,?,?,?)", (event.dedup_key, mask_secrets(_json(asdict(event))),
                                     stamp, stamp))
            return inserted.rowcount == 1

    def pending_events(self, limit: int = 100) -> list[dict]:
        """Expose accepted work after a restart rather than losing a claimed event."""
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM ingress WHERE status='accepted' "
                                "ORDER BY created_at LIMIT ?", (max(0, limit),)).fetchall()
        return [{**dict(row), "envelope": json.loads(row["envelope"])} for row in rows]

    def mark_event(self, event_key: str, status: str, reply: str = "") -> None:
        """Keep send ambiguity separate from execution; it must not rerun a model."""
        if status not in {"accepted", "running", "sent", "failed", "unknown"}:
            raise ValueError("invalid ingress status")
        with self._connection(write=True) as conn:
            changed = conn.execute("UPDATE ingress SET status=?,reply=?,updated_at=? "
                                   "WHERE event_key=?",
                                   (status, mask_secrets(reply), _now(), event_key))
            if changed.rowcount != 1:
                raise KeyError(event_key)

    def record_event(self, run_id: str, kind: str, data) -> dict:
        """Append an audit event; previous events have no mutation API."""
        stamp = _now()
        encoded = mask_secrets(_json(data))
        with self._connection(write=True) as conn:
            row = conn.execute("INSERT INTO run_events(run_id,kind,data,created_at) "
                               "VALUES (?,?,?,?)", (run_id, kind, encoded, stamp))
        return {"id": row.lastrowid, "run_id": run_id, "kind": kind,
                "data": json.loads(encoded), "created_at": stamp}

    def events(self, run_id: str) -> list[dict]:
        """Return a run's evidence and lifecycle events in insertion order."""
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM run_events WHERE run_id=? ORDER BY id",
                                (run_id,)).fetchall()
        return [{**dict(row), "data": json.loads(row["data"])} for row in rows]

    def create_approval(self, session: str, tool: str, args: dict,
                        ttl: float = 3600) -> dict:
        """Issue a one-use, expiring grant for one exact action in this owner scope."""
        if not session or not tool or not isinstance(args, dict):
            raise ValueError("approval requires session, tool and argument object")
        if not 0 < ttl <= 604800:
            raise ValueError("approval lifetime must be between zero and seven days")
        ident = "approval-" + uuid.uuid4().hex
        expires = time.time() + ttl
        with self._connection(write=True) as conn:
            conn.execute("INSERT INTO approvals(id,session,tool,args,fingerprint,"
                         "expires_at,created_at) VALUES (?,?,?,?,?,?,?)",
                         (ident, session, tool, _json(args), _fingerprint(session, tool, args),
                          expires, _now()))
        return self.get_approval(ident)

    def get_approval(self, ident: str) -> dict | None:
        """Inspect the exact pending action before a human approves its identifier."""
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE id=?", (ident,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["args"] = json.loads(value["args"])
        value["consumed"] = value["consumed_at"] is not None
        value["status"] = ("consumed" if value["consumed_at"] is not None else
                           "expired" if value["expires_at"] < time.time() else "pending")
        return value

    def consume_approval(self, ident: str, session: str, tool: str, args: dict) -> bool:
        """Atomically consume once; mismatches never broaden or spend a grant."""
        stamp = time.time()
        with self._connection(write=True) as conn:
            row = conn.execute("UPDATE approvals SET consumed_at=? WHERE id=? "
                               "AND fingerprint=? AND expires_at>=? AND consumed_at IS NULL",
                               (stamp, ident, _fingerprint(session, tool, args), stamp))
            return row.rowcount == 1

    def register_project(self, name: str, path: Path,
                         verification: list[list[str]] | None = None) -> dict:
        """Register a trusted workspace; queued tasks keep an immutable snapshot."""
        stamp = _now()
        workspace = str(Path(path).resolve())
        commands = verification or []
        try:
            with self._connection(write=True) as conn:
                conn.execute("INSERT INTO projects(name,path,verification,created_at,updated_at) "
                             "VALUES (?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
                             "path=excluded.path,verification=excluded.verification,"
                             "updated_at=excluded.updated_at",
                             (name, workspace, _json(commands), stamp, stamp))
        except sqlite3.IntegrityError as exc:
            raise ValueError("workspace is already registered under another project name") from exc
        return self.project(name)

    def project(self, name: str) -> dict | None:
        """Look up only previously owner-registered projects."""
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM projects WHERE name=?", (name,)).fetchone()
        if row is None:
            return None
        return {**dict(row), "verification": json.loads(row["verification"])}

    def projects(self) -> list[dict]:
        """List the trusted project registry without discovering arbitrary paths."""
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM projects ORDER BY name").fetchall()
        return [{**dict(row), "verification": json.loads(row["verification"])} for row in rows]

    def create_run(self, project: str, goal: str, session: str = "cli",
                   kind: str = "project") -> dict:
        """Enqueue work and freeze its workspace and owner-configured verification."""
        ident, stamp = "project-" + uuid.uuid4().hex, _now()
        with self._connection(write=True) as conn:
            target = conn.execute("SELECT * FROM projects WHERE name=?", (project,)).fetchone()
            if target is None:
                raise KeyError(f"project {project!r} is not registered")
            conn.execute("INSERT INTO runs(id,project,workspace,goal,session,kind,status,"
                         "verification,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (ident, project, target["path"], mask_secrets(goal), session, kind,
                          "queued", target["verification"], stamp, stamp))
        self.record_event(ident, "queued", {"project": project, "workspace": target["path"]})
        return self.run(ident)

    def _run_row(self, row) -> dict:
        """Decode machine fields while keeping user-visible states explicit."""
        value = dict(row)
        for field in ("evidence", "artifacts", "verification"):
            value[field] = json.loads(value[field])
        value["verified"] = bool(value["verified"])
        value["cancel_requested"] = bool(value["cancel_requested"])
        return value

    def run(self, ident: str) -> dict | None:
        """Retrieve one project job after process restart."""
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id=?", (ident,)).fetchone()
        return self._run_row(row) if row is not None else None

    def runs(self, project: str | None = None, status: str | None = None,
             limit: int = 50) -> list[dict]:
        """Query queue/history with bound arguments and bounded result count."""
        clauses, args = [], []
        if project is not None:
            clauses.append("project=?")
            args.append(project)
        if status is not None:
            if status not in RUN_STATES:
                raise ValueError("invalid run status")
            clauses.append("status=?")
            args.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        args.append(min(max(int(limit), 0), 1000))
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM runs" + where + " ORDER BY created_at LIMIT ?",
                                args).fetchall()
        return [self._run_row(row) for row in rows]

    def claim_run(self, ident: str) -> bool:
        """One worker wins; two tasks never mutate the same workspace concurrently."""
        stamp = _now()
        with self._connection(write=True) as conn:
            row = conn.execute("UPDATE runs SET status='running',attempt=attempt+1,"
                               "started_at=?,updated_at=? WHERE id=? AND status='queued' "
                               "AND cancel_requested=0 AND NOT EXISTS "
                               "(SELECT 1 FROM runs other WHERE other.status='running' "
                               "AND other.workspace=runs.workspace)", (stamp, stamp, ident))
            return row.rowcount == 1

    def update_run(self, ident: str, **fields) -> dict:
        """Persist bounded lifecycle fields; a cancellation cannot be overwritten by success."""
        if not fields or set(fields) - _RUN_FIELDS:
            raise ValueError("unsupported run update")
        if "status" in fields and fields["status"] not in RUN_STATES:
            raise ValueError("invalid run status")
        values = {}
        for key, value in fields.items():
            if key in {"evidence", "artifacts"}:
                values[key] = mask_secrets(_json(value))
            elif key in {"verified", "cancel_requested"}:
                values[key] = int(bool(value))
            else:
                values[key] = mask_secrets(value)
        values["updated_at"] = _now()
        with self._connection(write=True) as conn:
            current = conn.execute("SELECT * FROM runs WHERE id=?", (ident,)).fetchone()
            if current is None:
                raise KeyError(ident)
            if current["cancel_requested"] and fields.get("status") in {"done", "failed"}:
                values.update(status="cancelled", verified=0)
            columns = ",".join(f"{key}=?" for key in values)
            conn.execute("UPDATE runs SET " + columns + " WHERE id=?", [*values.values(), ident])
        return self.run(ident)

    def cancel_run(self, ident: str) -> dict:
        """Mark cancellation durably; running backends observe it cooperatively."""
        with self._connection(write=True) as conn:
            row = conn.execute("SELECT * FROM runs WHERE id=?", (ident,)).fetchone()
            if row is None:
                raise KeyError(ident)
            if row["status"] not in _TERMINAL:
                status = "cancelled" if row["status"] != "running" else "running"
                conn.execute("UPDATE runs SET cancel_requested=1,status=?,updated_at=? "
                             "WHERE id=?", (status, _now(), ident))
        return self.run(ident)

    def recover_runs(self) -> int:
        """Expose interrupted work after an exclusive service restart; never replay effects."""
        with self._connection(write=True) as conn:
            rows = conn.execute("SELECT id FROM runs WHERE status='running'").fetchall()
            stamp = _now()
            conn.execute("UPDATE runs SET status=CASE WHEN cancel_requested=1 THEN 'cancelled' "
                         "ELSE 'interrupted' END,error='worker interrupted; inspect before retry',"
                         "finished_at=?,updated_at=? WHERE status='running'", (stamp, stamp))
        for row in rows:
            self.record_event(row["id"], "interrupted", {"retry": "explicit owner resubmission required"})
        return len(rows)
