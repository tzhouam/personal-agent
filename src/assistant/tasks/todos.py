"""Derive todos from the day's signals (red-priority GitHub notifications, a
pending resume approval), enrich them with PR/issue context, and monitor open
todos — auto-closing ones whose underlying task is finished (PR merged/closed,
review already submitted, issue closed). Manual todos via `assistant todo add`."""

import logging

from ..todo_store import TodoStore

log = logging.getLogger("assistant")

_RESUME_KEY = "resume-approval"


def update_todos(store: TodoStore, digest: dict, resume: dict, github=None) -> dict:
    # ── monitor pass first: close finished work before adding new items ──
    closed = []
    if github is not None:
        for todo in store.open_items():
            if todo.get("source") != "github" or not todo.get("url"):
                continue
            try:
                finished, reason = github.check_finished(todo["url"])
            except Exception as exc:  # API hiccup — check again next run
                log.warning("todo monitor failed for %s: %s", todo["url"], exc)
                continue
            if finished:
                store.close_by_key(todo["key"])
                closed.append({"id": todo["id"], "title": todo["title"], "reason": reason})

    # ── add new todos from red-priority notifications ──
    added = []
    for item in digest.get("sections", {}).get("red", []):
        key = item.get("url") or f"notif-{item.get('id')}"
        long_summary = item.get("summary") or item.get("title", "")
        detail = long_summary
        if github is not None and item.get("url"):
            try:  # enrich with author / size / age / body snippet
                context = github.fetch_item_context(item["url"])
                if context:
                    detail = f"{long_summary} — {context}"[:600]
            except Exception as exc:
                log.warning("todo context fetch failed for %s: %s", item["url"], exc)
        todo = store.upsert(
            key,
            # short imperative label as the title; the full context as detail
            title=item.get("todo") or long_summary,
            detail=detail,
            url=item.get("url"),
            type=item.get("type", ""),
            source="github",
            priority="red",
            action=item.get("action"),
        )
        if todo:
            added.append(todo["id"])

    if resume.get("status") == "pending_approval":
        todo = store.upsert(
            _RESUME_KEY,
            title="Review & push the pending resume update (assistant approve-resume)",
            source="resume",
            priority="red",
        )
        if todo:
            added.append(todo["id"])
    else:
        store.close_by_key(_RESUME_KEY)

    open_items = store.open_items()
    return {"added": added, "closed": closed, "open": open_items,
            "open_count": len(open_items)}
