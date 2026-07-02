"""Derive todos from the day's signals: red-priority GitHub notifications and
a pending resume approval. Manual todos come in via `assistant todo add`."""

from ..todo_store import TodoStore

_RESUME_KEY = "resume-approval"


def update_todos(store: TodoStore, digest: dict, resume: dict) -> dict:
    added = []
    for item in digest.get("sections", {}).get("red", []):
        key = item.get("url") or f"notif-{item.get('id')}"
        long_summary = item.get("summary") or item.get("title", "")
        todo = store.upsert(
            key,
            # short imperative label as the title; the full sentence stays as detail
            title=item.get("todo") or long_summary,
            detail=long_summary if item.get("todo") else "",
            url=item.get("url"),
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
    return {"added": added, "open": open_items, "open_count": len(open_items)}
