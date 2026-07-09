import json
import sqlite3
from datetime import datetime, timezone
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
"""


class EventsStore:
    """Raw observation log (evidence layer) + surfaced-item dedup store."""

    def __init__(self, db_path: Path):
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

    def mark_seen(self, item_ids: list[str], context: str = "") -> None:
        now = datetime.now(timezone.utc).isoformat()
        for item_id in item_ids:
            self.conn.execute(
                "INSERT INTO seen (item_id, first_seen, last_seen, context) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(item_id) DO UPDATE SET last_seen = excluded.last_seen",
                (item_id, now, now, context),
            )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
