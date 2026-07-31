"""SQLite-backed evidence layer for the pipeline.

Owns three tables: an append-only ``observations`` log (with an FTS5 mirror for
search), a ``seen`` dedup store so an item is surfaced once, and a ``metrics``
table feeding the digest's Health section. Exports the ``EventsStore`` wrapper.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    source TEXT NOT NULL,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT,
    entities TEXT,
    raw TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS observations_fts
    USING fts5(title, entities, content=observations, content_rowid=id);
CREATE TRIGGER IF NOT EXISTS obs_ai AFTER INSERT ON observations BEGIN
    INSERT INTO observations_fts(rowid, title, entities)
    VALUES (new.id, new.title, new.entities);
END;
CREATE TABLE IF NOT EXISTS seen (
    item_id TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    context TEXT
);
CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    step TEXT NOT NULL,
    name TEXT NOT NULL,
    value REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS metrics_name_ts ON metrics (name, ts);
"""


class EventsStore:
    """Raw observation log (evidence layer) + surfaced-item dedup store."""

    def __init__(self, db_path: Path):
        """Open (creating parents) the SQLite db at ``db_path`` and apply the
        idempotent schema so the store is usable on a fresh checkout."""
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def add_observations(self, run_id: str, observations: list[dict],
                         dedupe: bool = False) -> list[int]:
        """``dedupe=True`` (backfill/enrich re-runs) skips rows identical in
        (source, kind, title, url); the daily path keeps appending as-is —
        a state change (e.g. [open]→[merged]) alters the title, so it still
        inserts, which is new information, not a duplicate."""
        ids = []
        for obs in observations:
            if dedupe and self.conn.execute(
                "SELECT 1 FROM observations WHERE source=? AND kind=? AND title=?"
                " AND ifnull(url,'')=? LIMIT 1",
                (obs.get("source", ""), obs.get("kind", ""), obs.get("title", ""),
                 obs.get("url") or ""),
            ).fetchone():
                continue
            cur = self.conn.execute(
                "INSERT INTO observations (run_id, source, ts, kind, title, url, entities, raw)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    obs.get("source", ""),
                    obs.get("ts", ""),
                    obs.get("kind", ""),
                    obs.get("title", ""),
                    obs.get("url"),
                    " ".join(obs.get("entities", [])),
                    json.dumps(obs.get("raw", {}), ensure_ascii=False)[:4000],
                ),
            )
            ids.append(cur.lastrowid)
        self.conn.commit()
        return ids

    def filter_unseen(self, item_ids: list[str]) -> list[str]:
        """Keep only ids absent from ``seen`` — the dedup gate so an item is
        surfaced once. Order-preserving; empty input short-circuits."""
        if not item_ids:
            return []
        placeholders = ",".join("?" * len(item_ids))
        seen = {
            row[0]
            for row in self.conn.execute(
                f"SELECT item_id FROM seen WHERE item_id IN ({placeholders})", item_ids
            )
        }
        return [i for i in item_ids if i not in seen]

    def filter_unseen_versioned(self, pairs: list[tuple[str, str]],
                                cooldown_days: int = 7) -> list[str]:
        """Fingerprint-aware dedup gate (audit F21): keep ids that should
        SURFACE. An id passes when it is absent; a known id passes only when
        its stored fingerprint differs from the current one (genuinely new
        activity) AND its `last_seen` is older than `cooldown_days` — new
        activity resurfaces a thread at most once per cooldown (the old
        `updated_at`-keyed scheme re-surfaced busy threads every day; a bare
        existence check suppressed them forever). Activity WITHIN the
        cooldown is deliberately folded into the already-shown thread (the
        fingerprint is adopted): the owner saw it days ago, and only
        activity AFTER the cooldown resurfaces it. (Deferring un-adopted
        fingerprints instead would promise a resurface the pipeline cannot
        deliver — collection only fetches recently-updated items, so a
        change suppressed today may never be fetched again.) Legacy rows
        whose context predates
        fingerprinting (e.g. "digest 2026-07-30") adopt the current
        fingerprint in place — no deploy-time resurface storm, converging to
        fingerprint tracking after one observation (`last_seen` preserved so
        adoption adds no extra delay)."""
        if not pairs:
            return []
        placeholders = ",".join("?" * len(pairs))
        rows = {row[0]: (row[1], row[2]) for row in self.conn.execute(
            f"SELECT item_id, last_seen, context FROM seen "
            f"WHERE item_id IN ({placeholders})", [i for i, _ in pairs])}
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=cooldown_days)).isoformat()
        out: list[str] = []
        adopt: list[tuple[str, str]] = []
        for item_id, fp in pairs:
            if item_id not in rows:
                out.append(item_id)
                continue
            last_seen, context = rows[item_id]
            stored_fp = context[3:] if str(context or "").startswith("fp:") else None
            if stored_fp is None:
                adopt.append((item_id, fp))       # legacy row: adopt, suppress
            elif stored_fp != str(fp):
                if last_seen < cutoff:
                    out.append(item_id)
                else:                             # in-cooldown activity folds
                    adopt.append((item_id, fp))   # into the shown thread
        for item_id, fp in adopt:
            self.conn.execute("UPDATE seen SET context = ? WHERE item_id = ?",
                              (f"fp:{fp}", item_id))
        if adopt:
            self.conn.commit()
        return out

    def mark_seen_versioned(self, pairs: list[tuple[str, str]]) -> None:
        """Record surfaced ids WITH their activity fingerprint (stored as
        `fp:<fingerprint>` in context; this upsert updates the fingerprint,
        unlike `mark_seen`'s, which deliberately leaves context alone)."""
        now = datetime.now(timezone.utc).isoformat()
        for item_id, fp in pairs:
            self.conn.execute(
                "INSERT INTO seen (item_id, first_seen, last_seen, context)"
                " VALUES (?, ?, ?, ?) ON CONFLICT(item_id) DO UPDATE SET"
                " last_seen = excluded.last_seen, context = excluded.context",
                (item_id, now, now, f"fp:{fp}"))
        self.conn.commit()

    def mark_seen(self, item_ids: list[str], context: str = "") -> None:
        """Record ``item_ids`` as surfaced. Upsert: a repeat only advances
        ``last_seen``, preserving the original ``first_seen``. ``context``
        notes where it was surfaced."""
        now = datetime.now(timezone.utc).isoformat()
        for item_id in item_ids:
            self.conn.execute(
                "INSERT INTO seen (item_id, first_seen, last_seen, context) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(item_id) DO UPDATE SET last_seen = excluded.last_seen",
                (item_id, now, now, context),
            )
        self.conn.commit()

    # ── pipeline metrics (doc/PIPELINE_METRICS.md) ───────────────────
    def record_metrics(self, run_id: str, step: str, values: dict) -> None:
        """One row per (run, step, metric). Values must be numeric."""
        now = datetime.now(timezone.utc).isoformat()
        for name, value in values.items():
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue  # a non-numeric value must never kill a phase
            self.conn.execute(
                "INSERT INTO metrics (run_id, ts, step, name, value) VALUES (?, ?, ?, ?, ?)",
                (run_id, now, step, name, value),
            )
        self.conn.commit()

    def metrics_window(self, days: int = 7) -> list[dict]:
        """All metric rows from the last ``days``, oldest first — the raw
        input build_health() rolls into the digest's Health section."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = self.conn.execute(
            "SELECT run_id, ts, step, name, value FROM metrics WHERE ts >= ?"
            " ORDER BY ts", (cutoff,))
        return [{"run_id": r[0], "ts": r[1], "step": r[2], "name": r[3], "value": r[4]}
                for r in rows]

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self.conn.close()
