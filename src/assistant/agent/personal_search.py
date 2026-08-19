"""Tenant-scoped retrieval across the owner's durable personal data.

The normal chat prompt intentionally sees only a small recent window.  This
module is the explicit, auditable escape hatch for requests such as "find the
interview details from an older conversation": it searches only paths derived
from the current user's ``Settings`` and returns bounded excerpts with stable
source ids.  No cross-user/global path is consulted.
"""

from __future__ import annotations

import json
import hashlib
import re
import sqlite3
from pathlib import Path

import yaml

from assistant.platform.config import Settings


SOURCES = frozenset({"sessions", "todos", "reminders", "tasks", "observations"})
_LATIN = re.compile(r"[a-z0-9][a-z0-9_.:/@-]{1,}", re.IGNORECASE)
_CJK = re.compile(r"[\u3400-\u9fff]{2,}")


def _terms(query: str) -> set[str]:
    """Useful literal terms plus CJK bi/tri-grams for phrase-tolerant matching."""
    low = str(query).casefold()
    terms = {m.group(0) for m in _LATIN.finditer(low)}
    for match in _CJK.finditer(low):
        value = match.group(0)
        if len(value) <= 4:
            terms.add(value)
        for n in (2, 3):
            terms.update(value[i:i + n] for i in range(len(value) - n + 1))
    return {t for t in terms if len(t) >= 2}


def _score(text: str, terms: set[str]) -> int:
    low = str(text).casefold()
    return sum(min(3, low.count(term)) * (3 if len(term) >= 5 else 1)
               for term in terms)


def _hit(source: str, ts: str, title: str, text: str, **meta) -> dict:
    return {"source": source, "ts": str(ts or ""), "title": str(title or "")[:240],
            "text": str(text or "")[:1600], "meta": meta}


def _session_hits(data_dir: Path) -> list[dict]:
    out: list[dict] = []
    root = data_dir / "sessions"
    paths = list(root.glob("*/*.json")) + list(root.glob("*.json"))
    for path in sorted(paths):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        for turn in data.get("turns", []):
            owner = str(turn.get("owner", ""))
            assistant = str(turn.get("assistant", ""))
            out.append(_hit("sessions", turn.get("ts", ""), owner[:160],
                            f"Owner: {owner}\nAssistant: {assistant}",
                            outcome=turn.get("outcome", "")))
    return out


def _todo_hits(settings: Settings) -> list[dict]:
    path = settings.profile_dir / "todos.yaml"
    if not path.exists():
        return []
    try:
        items = (yaml.safe_load(path.read_text()) or {}).get("items", [])
    except (OSError, ValueError):
        return []
    return [_hit("todos", item.get("created", ""), item.get("title", ""),
                 " · ".join(str(v) for v in (
                     item.get("title", ""), item.get("detail", ""), item.get("url", ""),
                     f"status={item.get('status', '')}", f"due={item.get('due', '')}") if v),
                 id=item.get("id", ""), status=item.get("status", ""))
            for item in items]


def _reminder_hits(data_dir: Path) -> list[dict]:
    path = data_dir / "reminders.yaml"
    if not path.exists():
        return []
    try:
        rows = (yaml.safe_load(path.read_text()) or {}).get("reminders", [])
    except (OSError, ValueError):
        return []
    return [_hit("reminders", row.get("due_at", ""), row.get("message", "")[:160],
                 f"{row.get('message', '')} · due={row.get('due_at', '')} "
                 f"· sent={row.get('sent_at', '')}", id=row.get("id", ""),
                 sent_at=row.get("sent_at")) for row in rows]


def _task_hits(data_dir: Path) -> list[dict]:
    out: list[dict] = []
    for path in sorted((data_dir / "tasks").glob("task-*.json")):
        try:
            row = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        outcomes = "\n".join(str(step.get("outcome", ""))[:500]
                             for step in row.get("steps", [])[-6:])
        out.append(_hit("tasks", row.get("started", ""), row.get("request", "")[:160],
                        f"Request: {row.get('request', '')}\nReport: {row.get('report', '')}"
                        + (f"\nOutcomes:\n{outcomes}" if outcomes else ""),
                        id=row.get("id", ""), status=row.get("status", "")))
    return out


def _observation_hits(db_path: Path) -> list[dict]:
    if not db_path.exists():
        return []
    out: list[dict] = []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT id, source, ts, kind, title, ifnull(url,''), "
                "ifnull(entities,''), ifnull(raw,'') FROM observations "
                "ORDER BY id DESC LIMIT 5000").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    for row in rows:
        out.append(_hit("observations", row[2], row[4],
                        " · ".join(v for v in (row[4], row[5], row[6], row[7][:500]) if v),
                        id=row[0], origin=row[1], kind=row[3], url=row[5]))
    return out


def search_personal_data(settings: Settings, query: str, *, sources=None,
                         limit: int = 12) -> list[dict]:
    """Return the best literal matches, each assigned a stable ``P<n>`` id."""
    terms = _terms(query)
    if not terms:
        return []
    if isinstance(sources, str):
        selected = {s.strip().lower() for s in sources.split(",") if s.strip()}
    elif isinstance(sources, (list, tuple, set)):
        selected = {str(s).strip().lower() for s in sources if str(s).strip()}
    else:
        selected = set(SOURCES)
    selected &= SOURCES
    if not selected:
        selected = set(SOURCES)

    hits: list[dict] = []
    if "sessions" in selected:
        hits += _session_hits(settings.data_dir)
    if "todos" in selected:
        hits += _todo_hits(settings)
    if "reminders" in selected:
        hits += _reminder_hits(settings.data_dir)
    if "tasks" in selected:
        hits += _task_hits(settings.data_dir)
    if "observations" in selected:
        hits += _observation_hits(settings.events_db)

    scored = [(_score(f"{h['title']}\n{h['text']}", terms), h) for h in hits]
    matched = [pair for pair in scored if pair[0] > 0]
    matched.sort(key=lambda pair: (pair[0], pair[1].get("ts", "")), reverse=True)
    out = []
    for score, hit in matched[:max(1, min(int(limit), 30))]:
        identity = json.dumps(
            {k: hit.get(k) for k in ("source", "ts", "title", "text", "meta")},
            ensure_ascii=False, sort_keys=True, default=str)
        evidence_id = "P-" + hashlib.sha1(identity.encode()).hexdigest()[:8]
        out.append({**hit, "id": evidence_id, "score": score})
    return out


def render_hits(query: str, hits: list[dict]) -> str:
    if not hits:
        return f"no personal records matched {query!r}"
    lines = [f"personal records matching {query!r} ({len(hits)}):"]
    for hit in hits:
        stamp = f" · {hit['ts']}" if hit.get("ts") else ""
        lines.append(f"[{hit['id']}] {hit['source']}{stamp} · {hit['title']}\n"
                     f"{hit['text']}")
    return "\n\n".join(lines)
