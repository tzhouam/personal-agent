"""Durable-delivery ledgers (Track D — doc/DESIGN_DURABLE_DELIVERY.md).

One `outbox.db` per user (NOT `delivery.db`, which belongs to the digest
DeliveryLedger) holding the email chat ledger (D2), the routine occurrence
ledger (D4), and system notes — plus the derived failure surface (D5): a read
API over the producers' own rows, so no cross-store handoff window exists.
The no-silent-loss guarantee: durable bounded delivery attempts, and terminal
failures stay ELIGIBLE for insertion into every future transport-accepted
interaction until acknowledged or expired (48h after first actually shown —
receipt-gated, so an unseen failure never starts its clock)."""

import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("assistant")

_SCHEMA_VERSION = 1
_MAX_ATTEMPTS = 3
_OUTPUT_CAP = 4096
_SURFACE_TTL_HOURS = 48
_RETENTION_DAYS = 30

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS email_ledger (
    uidvalidity INTEGER NOT NULL,
    uid         INTEGER NOT NULL,
    state       TEXT NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    reply       TEXT,
    surfaced_ids TEXT,
    summary     TEXT,
    claim_token TEXT,
    surfaced_at TEXT,
    acked_at    TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (uidvalidity, uid));
CREATE TABLE IF NOT EXISTS routine_ledger (
    routine_id  TEXT NOT NULL,
    occurrence  TEXT NOT NULL,
    state       TEXT NOT NULL,
    claim_token TEXT,
    output      TEXT,
    error       TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    claimed_at  TEXT,
    updated_at  TEXT NOT NULL,
    surfaced_at TEXT,
    acked_at    TEXT,
    PRIMARY KEY (routine_id, occurrence));
CREATE TABLE IF NOT EXISTS system_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summary TEXT NOT NULL,
    failed_at TEXT NOT NULL,
    surfaced_at TEXT,
    acked_at TEXT);
"""

_CORRUPT_MARKERS = ("file is not a database", "database disk image is malformed")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class OutboxDB:
    """Per-user transactional store for the delivery ledgers. Open→use→close
    per operation batch (the events.db pattern); WAL + busy_timeout cover the
    multi-threaded daemon, the per-user write lock serializes writers."""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "outbox.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.conn = self._open()
        except sqlite3.DatabaseError as exc:
            # Narrow by design: only REAL corruption moves the db aside (with
            # sidecars); contention/permission/schema errors propagate.
            if not any(m in str(exc).lower() for m in _CORRUPT_MARKERS):
                raise
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            for suffix in ("", "-wal", "-shm"):
                p = Path(str(self.path) + suffix)
                if p.exists():
                    p.rename(str(self.path) + f".corrupt-{stamp}{suffix}")
            log.error("outbox.db corrupt — moved aside, recreated")
            self.conn = self._open()
            self.add_system_note(
                "delivery state store was corrupt and had to be reset — "
                "in-flight retries may have been lost")

    def _open(self) -> sqlite3.Connection:
        created = not self.path.exists()
        conn = sqlite3.connect(self.path, timeout=5)
        if created:
            os.chmod(self.path, 0o600)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        # version gate BEFORE any schema mutation: a db written by newer code
        # is refused UNTOUCHED
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'").fetchone()
        except sqlite3.OperationalError:
            row = None   # no meta table: fresh (or pre-schema) db
        if row is not None and int(row[0]) > _SCHEMA_VERSION:
            conn.close()
            raise RuntimeError(
                f"outbox.db schema v{row[0]} is newer than this code "
                f"(v{_SCHEMA_VERSION}) — refusing to touch it")
        conn.executescript(_SCHEMA)   # additive CREATE IF NOT EXISTS only
        if row is None:
            conn.execute(
                "INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)",
                (str(_SCHEMA_VERSION),))
        elif int(row[0]) < _SCHEMA_VERSION:
            # forward migrations run here (none yet at v1)
            conn.execute("UPDATE meta SET value=? WHERE key='schema_version'",
                         (str(_SCHEMA_VERSION),))
        conn.commit()
        return conn

    def close(self) -> None:
        self.conn.close()

    # ── meta ─────────────────────────────────────────────────────────
    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.set_meta_many({key: value})

    def set_meta_many(self, pairs: dict) -> None:
        """Several meta keys in ONE transaction — an epoch change must never
        commit its UIDVALIDITY separately from its baseline (a crash between
        the two would pair a new epoch with an old baseline and skip mail)."""
        for key, value in pairs.items():
            self.conn.execute(
                "INSERT INTO meta VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                "value=excluded.value", (key, str(value)))
        self.conn.commit()

    # ── email ledger (D2) ────────────────────────────────────────────
    def email_discover(self, uidvalidity: int, uid: int, summary: str = "",
                       ignored: bool = False) -> bool:
        """Insert a newly discovered UID (idempotent). Returns True if new."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO email_ledger (uidvalidity, uid, state, "
            "summary, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (uidvalidity, uid, "ignored" if ignored else "pending",
             summary[:200], _now(), _now()))
        self.conn.commit()
        return cur.rowcount > 0

    def email_due(self, uidvalidity: int) -> list[dict]:
        """The work queue this cycle, ascending by UID with head-of-line
        discipline: recover stale `executing` rows first (a dead process's
        turn may have half-run — dead-lettered, never rerun), then return
        rows up to and including the FIRST nonterminal one ("contiguous"
        among discovered UIDs)."""
        stale = self.conn.execute(
            "SELECT uid, claim_token FROM email_ledger WHERE uidvalidity=? "
            "AND state='executing'", (uidvalidity,)).fetchall()
        for uid, token in stale:
            # any executing row seen at the START of a cycle is stale: the
            # single poll thread runs turns synchronously, so a live turn
            # can never coexist with its own cycle's due-scan
            self.conn.execute(
                "UPDATE email_ledger SET state='dead', last_error=?, "
                "updated_at=? WHERE uidvalidity=? AND uid=? AND state='executing' "
                "AND claim_token=?",
                ("interrupted mid-processing — may have partially run; not retried",
                 _now(), uidvalidity, uid, token))
        self.conn.commit()
        rows = self.conn.execute(
            "SELECT uidvalidity, uid, state, attempts, reply, surfaced_ids "
            "FROM email_ledger WHERE (uidvalidity=? AND state='pending') "
            "OR state='processed' "   # processed replies send over SMTP — an
            "ORDER BY uidvalidity ASC, uid ASC",  # epoch reset doesn't strand them
            (uidvalidity,)).fetchall()
        out = []
        for uv, uid, state, attempts, reply, surfaced in rows:
            out.append({"uidvalidity": uv, "uid": uid, "state": state,
                        "attempts": attempts, "reply": reply,
                        "surfaced_ids": json.loads(surfaced) if surfaced else []})
        return out

    def email_begin_turn(self, uidvalidity: int, uid: int) -> str | None:
        """pending → executing with a fresh claim token; None if not pending."""
        token = uuid.uuid4().hex
        cur = self.conn.execute(
            "UPDATE email_ledger SET state='executing', claim_token=?, "
            "updated_at=? WHERE uidvalidity=? AND uid=? AND state='pending'",
            (token, _now(), uidvalidity, uid))
        self.conn.commit()
        return token if cur.rowcount else None

    def email_finish_turn(self, uidvalidity: int, uid: int, token: str,
                          reply: str, surfaced_ids: list[str]) -> bool:
        """executing → processed (outbox), CAS on the claim token."""
        cur = self.conn.execute(
            "UPDATE email_ledger SET state='processed', reply=?, surfaced_ids=?, "
            "updated_at=? WHERE uidvalidity=? AND uid=? AND state='executing' "
            "AND claim_token=?",
            (reply, json.dumps(surfaced_ids), _now(), uidvalidity, uid, token))
        self.conn.commit()
        return cur.rowcount > 0

    def email_turn_failed(self, uidvalidity: int, uid: int, token: str,
                          error: str) -> None:
        """A turn that raised CLEANLY returns to pending (attempts+1) — dead
        past the bound. CAS on the token."""
        self.conn.execute(
            "UPDATE email_ledger SET state=CASE WHEN attempts+1>=? THEN 'dead' "
            "ELSE 'pending' END, attempts=attempts+1, last_error=?, updated_at=? "
            "WHERE uidvalidity=? AND uid=? AND state='executing' AND claim_token=?",
            (_MAX_ATTEMPTS, str(error)[:300], _now(), uidvalidity, uid, token))
        self.conn.commit()

    def email_ack(self, uidvalidity: int, uid: int) -> None:
        """processed → acked (reply transport-accepted)."""
        self.conn.execute(
            "UPDATE email_ledger SET state='acked', updated_at=? "
            "WHERE uidvalidity=? AND uid=? AND state='processed'",
            (_now(), uidvalidity, uid))
        self.conn.commit()

    def email_send_failed(self, uidvalidity: int, uid: int, error: str) -> None:
        """A processed row whose send failed: attempts+1, dead past the bound
        (the outbox reply is retried, never the turn)."""
        self.conn.execute(
            "UPDATE email_ledger SET state=CASE WHEN attempts+1>=? THEN 'dead' "
            "ELSE 'processed' END, attempts=attempts+1, last_error=?, updated_at=? "
            "WHERE uidvalidity=? AND uid=? AND state='processed'",
            (_MAX_ATTEMPTS, str(error)[:300], _now(), uidvalidity, uid))
        self.conn.commit()

    def email_settle_old_epoch_pending(self, old_uidvalidity: int) -> int:
        """A mailbox reset makes old-epoch PENDING rows unfetchable — settle
        them as dead (visible on the surface), never silently. processed rows
        keep retrying their sends."""
        cur = self.conn.execute(
            "UPDATE email_ledger SET state='dead', last_error=?, updated_at=? "
            "WHERE uidvalidity=? AND state IN ('pending','executing')",
            ("mailbox was reset before this mail could be processed",
             _now(), old_uidvalidity))
        self.conn.commit()
        return cur.rowcount

    def email_settled_frontier(self, uidvalidity: int, baseline: int) -> int:
        """Highest contiguous settled UID (among discovered rows) above the
        baseline — the rollback shadow old code resumes from."""
        rows = self.conn.execute(
            "SELECT uid, state FROM email_ledger WHERE uidvalidity=? AND uid>? "
            "ORDER BY uid ASC", (uidvalidity, baseline)).fetchall()
        frontier = baseline
        for uid, state in rows:
            if state in ("acked", "ignored", "dead"):
                frontier = uid
            else:
                break
        return frontier

    # ── routine ledger (D4) ──────────────────────────────────────────
    def routine_claim(self, routine_id: str, occurrence: str) -> str | None:
        """Mint/claim an occurrence. New rows insert as `claimed`; a stale
        `claimed` row (dead process, pre-side-effects by definition) is
        re-claimed with a fresh token. Returns the token, or None when the
        occurrence is already past `claimed`."""
        token = uuid.uuid4().hex
        cur = self.conn.execute(
            "INSERT INTO routine_ledger (routine_id, occurrence, state, "
            "claim_token, claimed_at, updated_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(routine_id, occurrence) DO UPDATE SET "
            "claim_token=excluded.claim_token, claimed_at=excluded.claimed_at, "
            "updated_at=excluded.updated_at WHERE routine_ledger.state='claimed'",
            (routine_id, occurrence, "claimed", token, _now(), _now()))
        self.conn.commit()
        return token if cur.rowcount else None

    def routine_transition(self, routine_id: str, occurrence: str, token: str,
                           to_state: str, output: str | None = None,
                           error: str | None = None,
                           from_states: tuple = ()) -> bool:
        """CAS transition on the claim token (a displaced claimant's writes
        are rejected). `output` is capped at 4KB with a visible marker."""
        if output and len(output.encode()) > _OUTPUT_CAP:
            clipped = output.encode()[:_OUTPUT_CAP - 24].decode("utf-8", "ignore")
            output = clipped + "…(输出过长已截断)"
        placeholders = ",".join("?" * len(from_states))
        cur = self.conn.execute(
            f"UPDATE routine_ledger SET state=?, output=COALESCE(?, output), "
            f"error=?, updated_at=? WHERE routine_id=? AND occurrence=? "
            f"AND claim_token=? AND state IN ({placeholders})",
            (to_state, output, error and str(error)[:300], _now(),
             routine_id, occurrence, token, *from_states))
        self.conn.commit()
        return cur.rowcount > 0

    def routine_recover(self) -> list[dict]:
        """Cycle-start recovery. Runs in the SAME single thread that executes
        tasks, so any `executing` row it sees is from a dead process — no
        timed lease, no clock comparison, no long-task mislabel: it becomes
        execution_unknown (side effects may have started — never retried,
        surfaced). Returns (a) stale `claimed` rows (pre-side-effects: the
        caller RESUMES them — a crash between claim and execution must not
        strand the occurrence) and (b) executed/execution_failed rows with
        undelivered output for delivery-only retry."""
        self.conn.execute(
            "UPDATE routine_ledger SET state='execution_unknown', updated_at=? "
            "WHERE state='executing'", (_now(),))
        self.conn.commit()
        rows = self.conn.execute(
            "SELECT routine_id, occurrence, claim_token, output, error, attempts, "
            "state FROM routine_ledger WHERE state IN "
            "('claimed','executed','execution_failed')").fetchall()
        return [{"routine_id": r, "occurrence": o, "claim_token": t,
                 "output": out, "error": e, "attempts": a, "state": st}
                for r, o, t, out, e, a, st in rows]

    def routine_delivered(self, routine_id: str, occurrence: str, token: str) -> bool:
        return self.routine_transition(routine_id, occurrence, token, "delivered",
                                       from_states=("executed", "execution_failed"))

    def routine_delivery_failed(self, routine_id: str, occurrence: str,
                                token: str, error: str) -> None:
        """Delivery attempt failed: attempts+1; terminal past the bound."""
        self.conn.execute(
            "UPDATE routine_ledger SET "
            "state=CASE WHEN attempts+1>=? THEN 'delivery_failed' ELSE state END, "
            "attempts=attempts+1, error=?, updated_at=? "
            "WHERE routine_id=? AND occurrence=? AND claim_token=? "
            "AND state IN ('executed','execution_failed')",
            (_MAX_ATTEMPTS, str(error)[:300], _now(),
             routine_id, occurrence, token))
        self.conn.commit()

    # ── system notes ─────────────────────────────────────────────────
    def add_system_note(self, summary: str) -> None:
        self.conn.execute(
            "INSERT INTO system_notes (summary, failed_at) VALUES (?, ?)",
            (summary[:300], _now()))
        self.conn.commit()

    # ── D5: surface bookkeeping ──────────────────────────────────────
    def mark_surfaced(self, ids: list[str]) -> None:
        """Receipt callback: the reply carrying these failure ids was
        transport-accepted — start (only once) their 48h expiry clocks."""
        now = _now()
        for fid in ids:
            kind, key = parse_failure_id(fid)
            if kind == "email":
                self.conn.execute(
                    "UPDATE email_ledger SET surfaced_at=COALESCE(surfaced_at,?) "
                    "WHERE uidvalidity=? AND uid=?", (now, key[0], key[1]))
            elif kind == "routine":
                self.conn.execute(
                    "UPDATE routine_ledger SET surfaced_at=COALESCE(surfaced_at,?) "
                    "WHERE routine_id=? AND occurrence=?", (now, key[0], key[1]))
            elif kind == "system":
                self.conn.execute(
                    "UPDATE system_notes SET surfaced_at=COALESCE(surfaced_at,?) "
                    "WHERE id=?", (now, key))
        self.conn.commit()

    def acknowledge(self, fid: str) -> bool:
        kind, key = parse_failure_id(fid)
        now = _now()
        if kind == "email":
            cur = self.conn.execute(
                "UPDATE email_ledger SET acked_at=? WHERE uidvalidity=? AND uid=? "
                "AND state='dead' AND acked_at IS NULL", (now, key[0], key[1]))
        elif kind == "routine":
            cur = self.conn.execute(
                "UPDATE routine_ledger SET acked_at=? WHERE routine_id=? AND "
                "occurrence=? AND acked_at IS NULL "
                "AND state IN ('delivery_failed','execution_unknown')",
                (now, key[0], key[1]))
        elif kind == "system":
            cur = self.conn.execute(
                "UPDATE system_notes SET acked_at=? WHERE id=? AND acked_at IS NULL",
                (now, key))
        else:
            return False
        self.conn.commit()
        return cur.rowcount > 0

    def open_failures(self) -> list[dict]:
        """This db's contribution to the failure surface (D5 predicate:
        unacked AND (never surfaced OR surfaced < 48h ago))."""
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(hours=_SURFACE_TTL_HOURS)).isoformat()
        out: list[dict] = []
        for uv, uid, err, surfaced in self.conn.execute(
                "SELECT uidvalidity, uid, last_error, surfaced_at FROM email_ledger "
                "WHERE state='dead' AND acked_at IS NULL"):
            if surfaced is None or surfaced > cutoff:
                out.append({"id": f"dfe{uv}-{uid}", "kind": "email",
                            "summary": f"一封邮件没有得到回复 ({err or 'failed'})"})
        for rid, occ, st, err, surfaced in self.conn.execute(
                "SELECT routine_id, occurrence, state, error, surfaced_at FROM "
                "routine_ledger WHERE state IN "
                "('delivery_failed','execution_unknown') AND acked_at IS NULL"):
            if surfaced is None or surfaced > cutoff:
                what = ("结果没送到" if st == "delivery_failed"
                        else "可能只执行了一半")
                out.append({"id": f"dfo{rid}@{occ.replace(' ', 'T')}",
                            "kind": "routine",
                            "summary": f"例行任务 {rid} ({occ}) {what}"})
        for nid, summary, surfaced in self.conn.execute(
                "SELECT id, summary, surfaced_at FROM system_notes "
                "WHERE acked_at IS NULL"):
            if surfaced is None or surfaced > cutoff:
                out.append({"id": f"dfs{nid}", "kind": "system",
                            "summary": summary})
        return out

    def prune(self) -> int:
        """Curate-phase retention: terminal rows older than 30 days — EXCEPT
        open failures (unacked and not yet expired), which are never pruned
        out from under the owner."""
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=_RETENTION_DAYS)).isoformat()
        expire = (datetime.now(timezone.utc)
                  - timedelta(hours=_SURFACE_TTL_HOURS)).isoformat()
        n = 0
        n += self.conn.execute(
            "DELETE FROM email_ledger WHERE updated_at < ? AND (state IN "
            "('acked','ignored') OR (state='dead' AND (acked_at IS NOT NULL "
            "OR surfaced_at < ?)))", (cutoff, expire)).rowcount
        n += self.conn.execute(
            "DELETE FROM routine_ledger WHERE updated_at < ? AND (state IN "
            "('delivered','condition_false','cancelled') OR (state IN "
            "('delivery_failed','execution_unknown') AND (acked_at IS NOT NULL "
            "OR surfaced_at < ?)))", (cutoff, expire)).rowcount
        self.conn.commit()
        return n


def parse_failure_id(fid: str):
    """Reverse a typed display id to (kind, key); ('', None) if unknown."""
    fid = str(fid or "").strip()
    if fid.startswith("dfe"):
        try:
            uv, uid = fid[3:].split("-", 1)
            return "email", (int(uv), int(uid))
        except ValueError:
            return "", None
    if fid.startswith("dfo"):
        try:
            rid, occ = fid[3:].split("@", 1)
            return "routine", (rid, occ.replace("T", " "))
        except ValueError:
            return "", None
    if fid.startswith("dfrem"):
        return "reminder", fid[5:]
    if fid.startswith("dfs"):
        try:
            return "system", int(fid[3:])
        except ValueError:
            return "", None
    return "", None


# ── the derived surface (all producers) ──────────────────────────────

def open_failures(settings) -> list[dict]:
    """The unified failure surface: outbox.db producers + reminder failed
    rows. Read-only; each entry: {id, kind, summary}."""
    out: list[dict] = []
    try:
        db = OutboxDB(settings.data_dir)
        try:
            out.extend(db.open_failures())
        finally:
            db.close()
    except Exception:
        log.exception("failure surface: outbox read failed")
    try:
        from assistant.platform.notify import ReminderStore

        cutoff = (datetime.now(timezone.utc)
                  - timedelta(hours=_SURFACE_TTL_HOURS)).isoformat()
        for r in ReminderStore(settings.data_dir).failed():
            if r.get("acked_at"):
                continue
            surfaced = r.get("surfaced_at")
            if surfaced is None or surfaced > cutoff:
                out.append({"id": f"dfrem{r['id']}", "kind": "reminder",
                            "summary": f"提醒没送出去: {r['message'][:60]} "
                                       f"(due {r['due_at']})"})
    except Exception:
        log.exception("failure surface: reminder read failed")
    out.sort(key=lambda f: (f["kind"], f["id"]))   # deterministic presentation
    return out


def _clip_bytes(text: str, limit: int) -> str:
    """UTF-8-safe byte clip with a visible marker."""
    if len(text.encode()) <= limit:
        return text
    return text.encode()[:max(limit - 3, 0)].decode("utf-8", "ignore") + "…"


def render_failure_block(failures: list[dict]) -> str:
    """The ≤512-byte, indivisible owner notice PREPENDED to chat replies in
    code (never model-dependent; F10 chunking always fits it in part 1).
    Provably terminating: every entry is byte-clipped up front, then whole
    entries drop until the block fits — each step strictly shrinks it."""
    if not failures:
        return ""
    header = "⚠ 有事项没送达（回复 知道了 <编号> 可清除）:"
    entries = [_clip_bytes(f"[{f['id']}] {f['summary']}", 130)
               for f in failures[:3]]
    hidden = len(failures) - len(entries)
    while True:
        lines = [header] + entries
        if hidden:
            lines.append(f"…还有 {hidden} 条")
        block = "\n".join(lines)
        if len(block.encode()) <= 512 or not entries:
            return block
        entries.pop()
        hidden += 1


def mark_surfaced(settings, ids: list[str]) -> None:
    """Receipt callback from a send site: these failure ids were inside a
    transport-accepted reply."""
    if not ids:
        return
    outbox_ids = [i for i in ids if not str(i).startswith("dfrem")]
    reminder_ids = [i for i in ids if str(i).startswith("dfrem")]
    try:
        if outbox_ids:
            db = OutboxDB(settings.data_dir)
            try:
                db.mark_surfaced(outbox_ids)
            finally:
                db.close()
        if reminder_ids:
            from assistant.platform.notify import ReminderStore

            store = ReminderStore(settings.data_dir)
            for fid in reminder_ids:
                store.mark_surfaced(parse_failure_id(fid)[1])
    except Exception:
        log.exception("mark_surfaced failed")


def acknowledge(settings, fid: str) -> bool:
    """The owner's 知道了 — clears one failure from the surface (audit trail
    kept: rows gain acked_at, they are not deleted)."""
    kind, key = parse_failure_id(fid)
    if kind == "reminder":
        from assistant.platform.notify import ReminderStore

        return ReminderStore(settings.data_dir).acknowledge_failed(key)
    if kind in ("email", "routine", "system"):
        db = OutboxDB(settings.data_dir)
        try:
            return db.acknowledge(fid)
        finally:
            db.close()
    return False
