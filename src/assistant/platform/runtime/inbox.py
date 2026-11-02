"""Durable channel inbox and reply ledger; ambiguous side effects never replay."""

from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import time

from assistant.platform.runtime.contracts import ChannelEvent
from assistant.platform.secrets import mask_secrets


class Inbox:
    """Persist before ACK, save replies before send, and recover conservatively."""

    def __init__(self, data_dir: Path):
        """Keep the receipt ledger separate from personal structured memory."""
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        self.path = Path(data_dir) / "runtime_inbox.sqlite3"
        if self.path.is_symlink():
            raise ValueError("inbox database must not be a symlink")
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS inbox ("
                       "id TEXT PRIMARY KEY, session TEXT NOT NULL, envelope TEXT NOT NULL, "
                       "state TEXT NOT NULL, reply TEXT NOT NULL DEFAULT '', "
                       "error TEXT NOT NULL DEFAULT '', receipt TEXT NOT NULL DEFAULT '{}', "
                       "created REAL NOT NULL, updated REAL NOT NULL)")
        self.path.chmod(0o600)

    def _db(self):
        """Short transactions let independent transports enqueue concurrently."""
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def put(self, event: ChannelEvent) -> bool:
        """Atomically deduplicate a full envelope before scheduling any model work."""
        envelope = asdict(event)
        envelope["text"] = mask_secrets(event.text)
        envelope["metadata"] = {}  # never persist signed attachment URLs or transport auth
        now = time.time()
        with self._db() as db:
            cur = db.execute("INSERT OR IGNORE INTO inbox "
                             "(id,session,envelope,state,created,updated) VALUES(?,?,?,'queued',?,?)",
                             (event.dedup_key, event.session_key, json.dumps(envelope), now, now))
            return cur.rowcount == 1

    def claim(self, *, delivery: bool = False) -> dict | None:
        """Claim one record while preventing concurrent turns in the same session."""
        source, target = ("ready", "sending") if delivery else ("queued", "running")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM inbox i WHERE state=? AND "
                             "NOT EXISTS (SELECT 1 FROM inbox b WHERE b.session=i.session "
                             "AND b.id<>i.id AND (b.state IN ('running','sending') OR "
                             "(b.state IN ('queued','ready') AND (b.created<i.created OR "
                             "(b.created=i.created AND b.rowid<i.rowid))))) ORDER BY created,i.rowid LIMIT 1",
                             (source,)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE inbox SET state=?,updated=? WHERE id=?",
                       (target, time.time(), row["id"]))
            result = dict(row)
            result["state"] = target
            return result

    def finish(self, ident: str, state: str, *, reply: str = "", error: str = "",
               receipt: dict | None = None) -> None:
        """Record each transition and keep replies available for delivery-only retry."""
        if state not in {"ready", "sent", "failed", "unknown", "interrupted"}:
            raise ValueError("invalid inbox terminal/ready state")
        with self._db() as db:
            db.execute("UPDATE inbox SET state=?, reply=CASE WHEN ?='' THEN reply ELSE ? END, "
                       "error=?,receipt=?,updated=? WHERE id=?",
                       (state, reply, mask_secrets(reply), mask_secrets(error),
                        json.dumps(receipt or {}), time.time(), ident))

    def recover(self) -> int:
        """Interrupted model work stays visible; a sending reply becomes uncertain."""
        with self._db() as db:
            a = db.execute("UPDATE inbox SET state='interrupted',error='runtime restarted during turn' "
                           "WHERE state='running'").rowcount
            b = db.execute("UPDATE inbox SET state='unknown',error='delivery may have been accepted' "
                           "WHERE state='sending'").rowcount
            return a + b

    def list(self, limit: int = 50) -> list[dict]:
        """Expose inspectable receipts without exposing model inputs by default."""
        with self._db() as db:
            return [dict(r) for r in db.execute(
                "SELECT id,session,state,error,receipt,created,updated FROM inbox "
                "ORDER BY created DESC LIMIT ?", (limit,))]

    def retry_delivery(self, ident: str) -> bool:
        """Explicit operator action resends a saved reply, never reruns the agent."""
        with self._db() as db:
            return db.execute("UPDATE inbox SET state='ready',updated=? "
                              "WHERE id=? AND state IN ('failed','unknown') AND reply<>''",
                              (time.time(), ident)).rowcount == 1

    def put_reply(self, session: str, ident: str, text: str) -> bool:
        """Queue a project result in its original conversation; restart-safe dedup."""
        with self._db() as db:
            original = db.execute("SELECT envelope FROM inbox WHERE session=? "
                                  "ORDER BY created DESC LIMIT 1", (session,)).fetchone()
            if original is None:
                return False
            envelope = json.loads(original["envelope"])
            envelope.update(event_id="completion:" + ident, text="")
            event = ChannelEvent(**envelope)
            now = time.time()
            db.execute("INSERT OR IGNORE INTO inbox "
                       "(id,session,envelope,state,reply,created,updated) "
                       "VALUES(?,?,?,'ready',?,?,?)", (event.dedup_key, session,
                       json.dumps(envelope), mask_secrets(text), now, now))
            return True
