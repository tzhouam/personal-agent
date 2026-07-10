"""Collect website marks: the browser queues done/unrelated clicks locally
and pushes them as small JSON files into the private marks repo; this module
pulls and applies them each run (owner decision 2026-07-10 — clicks act
locally, the agent reads instead of the page emailing).

Idempotency without write access: processed file paths are remembered in the
events seen-store (``marks:<path>``), and the underlying mark_done /
mark_unrelated calls are no-ops on repeat, so files never need deleting."""

import base64
import json
import logging

import httpx

from .config import Settings
from .events_store import EventsStore
from .todo_store import ReadingList, TodoStore

log = logging.getLogger("assistant")

_API = "https://api.github.com"


def collect_marks(settings: Settings, events: EventsStore) -> dict:
    """Apply unseen website marks. Returns {"applied": N, "files": M}."""
    if not (settings.marks_repo and settings.github_token):
        return {"applied": 0, "files": 0}
    client = httpx.Client(
        headers={"Authorization": f"Bearer {settings.github_token}",
                 "Accept": "application/vnd.github+json"}, timeout=30)
    listing = client.get(f"{_API}/repos/{settings.marks_repo}/contents/marks")
    if listing.status_code == 404:  # no marks pushed yet
        return {"applied": 0, "files": 0}
    listing.raise_for_status()
    files = [f for f in listing.json() if f.get("type") == "file"]

    unseen = set(events.filter_unseen([f"marks:{f['path']}" for f in files]))
    todos = TodoStore(settings.profile_dir)
    reading = ReadingList(settings.profile_dir)
    applied, processed = 0, []
    for entry in files:
        key = f"marks:{entry['path']}"
        if key not in unseen:
            continue
        try:
            blob = client.get(entry["url"]).json()
            marks = json.loads(base64.b64decode(blob.get("content", "") or b""))
        except Exception as exc:  # one corrupt file must not block the rest
            log.warning("marks file %s unreadable: %s", entry["path"], exc)
            processed.append(key)  # never retry garbage
            continue
        for mark in marks if isinstance(marks, list) else []:
            item_id = str(mark.get("id", ""))
            action = mark.get("action")
            ok = False
            if item_id.startswith("r") and action == "done":
                ok = reading.mark_done(item_id)
            elif item_id.startswith("r") and action == "unrelated":
                ok = reading.mark_unrelated(item_id)
            elif item_id.startswith("t") and action == "done":
                ok = todos.mark_done(item_id)
            applied += 1 if ok else 0
        processed.append(key)
    if processed:
        events.mark_seen(processed, context="website marks")
        log.info("website marks: %d applied from %d new file(s)", applied, len(processed))
    return {"applied": applied, "files": len(processed)}
