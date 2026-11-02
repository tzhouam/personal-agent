"""Owner-scoped append-only facts alongside the existing authoritative stores.

Structured personal records stay in their current formats. Retrieval reads them
without migrating them, and health/finance records remain explicit skill queries
rather than ambient context for an unrelated project.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid

from assistant.agent.lessons_store import LessonsStore
from assistant.agent.personal_search import _score, _terms, search_personal_data
from assistant.agent.profile_store import ProfileStore
from assistant.agent.todo_store import ReadingList
from assistant.agent.workflow_store import WorkflowStore
from assistant.platform.config import Settings
from assistant.platform.secrets import mask_secrets


_PRIVATE_KINDS = frozenset({"health", "finance", "credential", "secret"})
_CREDENTIAL = re.compile(r"(?:sk-(?:proj-)?[A-Za-z0-9_-]{12,}|xox[baprs]-[A-Za-z0-9-]{12,})")


def _redact(value: str) -> str:
    """Keep common API/chat credentials out of a durable fact or excerpt."""
    return _CREDENTIAL.sub("[credential redacted]", mask_secrets(value))


class Memory:
    """Read legacy evidence and retain new facts under one owner's data dir."""

    def __init__(self, settings: Settings):
        """Create only the new fact store; never rewrite a legacy data file."""
        self.settings = settings
        self.path = Path(settings.data_dir) / "runtime_memory.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("memory database must not be a symlink")
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        with self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS facts ("
                         "id TEXT PRIMARY KEY, text TEXT NOT NULL, source TEXT NOT NULL, "
                         "project TEXT NOT NULL, kind TEXT NOT NULL, ts TEXT NOT NULL)")
            conn.execute("CREATE INDEX IF NOT EXISTS facts_project ON facts(project, ts)")

    @contextmanager
    def _connect(self):
        """Use a short-lived connection so concurrent worker threads stay safe."""
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def remember(self, text: str, source: str, project: str = "", kind: str = "fact") -> dict:
        """Append a sourced fact; existing memories are never silently replaced."""
        if not isinstance(text, str) or not text.strip() or len(text) > 20_000:
            raise ValueError("memory text must contain 1–20000 characters")
        if not isinstance(source, str) or not source.strip() or len(source) > 1000:
            raise ValueError("memory requires a nonempty provenance source")
        if not isinstance(project, str) or len(project) > 200:
            raise ValueError("invalid project scope")
        if not isinstance(kind, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", kind):
            raise ValueError("invalid memory kind")
        row = {"id": "M-" + uuid.uuid4().hex, "text": _redact(text.strip()),
               "source": _redact(source.strip()), "project": project.strip(),
               "kind": kind, "ts": datetime.now(timezone.utc).isoformat()}
        with self._connect() as conn:
            conn.execute("INSERT INTO facts(id,text,source,project,kind,ts) "
                         "VALUES (:id,:text,:source,:project,:kind,:ts)", row)
        return row

    def _legacy(self, query: str, limit: int) -> list[dict]:
        """Read existing APIs that do not trigger finance/health migrations."""
        rows = search_personal_data(self.settings, query, limit=limit)
        store = ProfileStore(self.settings.profile_dir)
        profile = store.load() if store.exists() else {}
        for section in ("identity", "education", "experience", "preferences", "skills",
                        "projects", "interests"):
            value = profile.get(section)
            if value:
                entries = value if isinstance(value, list) else [value]
                for entry in entries:
                    text = json.dumps(entry, ensure_ascii=False, sort_keys=True)
                    rows.append({"source": "profile", "title": section,
                                 "text": text, "ts": "", "kind": section})
        for row in LessonsStore(self.settings.profile_dir).active():
            rows.append({"source": "lessons", "title": row.get("rule", ""),
                         "text": json.dumps(row, ensure_ascii=False, sort_keys=True),
                         "ts": row.get("created", ""), "kind": "preference"})
        for row in ReadingList(self.settings.profile_dir).load().get("items", []):
            rows.append({"source": "reading", "title": row.get("title", ""),
                         "text": json.dumps(row, ensure_ascii=False, sort_keys=True),
                         "ts": row.get("created", ""), "kind": "reading"})
        for row in WorkflowStore(self.settings.profile_dir).load().get("workflows", []):
            rows.append({"source": "workflows", "title": row.get("name", ""),
                         "text": json.dumps(row, ensure_ascii=False, sort_keys=True),
                         "ts": row.get("created", ""), "kind": "procedure"})
        return rows

    def recall(self, query: str, project: str = "", limit: int = 8) -> list[dict]:
        """Find sourced facts and legacy records, bounded and project-filtered.

        An empty query returns recent explicit facts only. A project query sees
        that project's facts plus owner-global facts; legacy hits must mention
        the requested project because old records carry no reliable project key.
        No finance/health files are read or copied to the ambient memory store.
        """
        if not isinstance(query, str) or not isinstance(project, str):
            raise ValueError("query and project must be strings")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValueError("memory limit must be between 1 and 50")
        terms = _terms(query)
        with self._connect() as conn:
            if project:
                rows = conn.execute("SELECT * FROM facts WHERE project IN ('', ?) "
                                    "ORDER BY ts DESC", (project.strip(),)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM facts ORDER BY ts DESC").fetchall()
        candidates = [dict(row) for row in rows if row["kind"] not in _PRIVATE_KINDS]
        if terms:
            for row in self._legacy(query, limit=30):
                if project and project.casefold() not in json.dumps(row, ensure_ascii=False).casefold():
                    continue
                row = dict(row)
                row.setdefault("project", "")
                row.setdefault("kind", row.get("source", "legacy"))
                row.setdefault("id", "L-" + hashlib.sha256(
                    json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16])
                candidates.append(row)
        matched = []
        for row in candidates:
            score = _score(str(row.get("title", "")) + "\n" + row["text"], terms) if terms else 1
            if score:
                row = {**row, "text": _redact(row["text"])[:4000], "score": score}
                matched.append(row)
        matched.sort(key=lambda row: (row["score"], row.get("ts", "")), reverse=True)
        return matched[:limit]
