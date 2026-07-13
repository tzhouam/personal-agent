"""Derive todos from the day's signals (red-priority GitHub notifications, a
pending resume approval), enrich them with PR/issue context, and monitor open
todos — auto-closing ones whose underlying task is finished (PR merged/closed,
review already submitted, issue closed). Manual todos via `assistant todo add`.

The todo ``detail`` is a written summary of the task, never pasted raw text:
the PR/issue body feeds a cheap-model summarization pass, and only the
structured meta line (author · size · age) is shown verbatim. If the LLM is
unavailable the body is dropped, not pasted."""

import logging

from ..todo_store import TodoStore

log = logging.getLogger("assistant")

_RESUME_KEY = "resume-approval"

_DETAIL_SYSTEM = """You write todo descriptions for your owner's task list. For each item you
get a notification summary plus the raw PR/issue body. Write 1-2 plain sentences saying what
the change/task is about and what the owner needs to do. No markdown, no headings, no bullet
lists, no quoting raw text.
Respond with ONLY a JSON array: [{"id": "<item id>", "detail": "<1-2 sentences>"}]"""


def _summarize_details(llm, items: list[dict]) -> dict[str, str]:
    """One batched cheap-model call → {id: written detail}. {} on any failure."""
    if llm is None or not items:
        return {}
    lines = "\n\n".join(
        f"id={i['id']}\nsummary: {i['summary']}\nbody: {i['body'] or '(empty)'}" for i in items
    )
    try:
        result = llm.complete_json(f"## Tasks\n\n{lines}", system=_DETAIL_SYSTEM,
                                   role="research", max_tokens=4000)
        return {str(r["id"]): str(r["detail"]).strip() for r in result
                if isinstance(r, dict) and r.get("id") and r.get("detail")}
    except Exception as exc:
        log.warning("todo detail summarization failed: %s", exc)
        return {}


def update_todos(store: TodoStore, digest: dict, resume: dict, github=None, llm=None) -> dict:
    """Reconcile the todo list against today's signals and return
    {added, closed, open (urgency-sorted), open_count}.

    Passes in order: expire todos untouched for 30 days; if `github` is given,
    auto-close ones whose underlying PR/issue is finished; add new todos from the
    digest's red-priority notifications (skipping keys already tracked), fetching
    context and writing LLM detail summaries only for genuinely new items; and
    add or clear the single resume-approval todo per `resume["status"]`. `github`
    and `llm` are optional — without them the monitor and summarization passes are
    skipped rather than failing."""
    # ── age-out pass: a todo untouched for a month is stale by definition ──
    closed = [{"id": t["id"], "title": t["title"], "reason": "outdated (open >30 days)"}
              for t in store.expire_stale(days=30)]

    # ── monitor pass: close finished work before adding new items ──
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
    open_keys = {i.get("key") for i in store.open_items()}
    candidates = []
    for item in digest.get("sections", {}).get("red", []):
        key = item.get("url") or f"notif-{item.get('id')}"
        if key in open_keys:
            continue  # already tracked — don't re-fetch or re-summarize
        long_summary = item.get("summary") or item.get("title", "")
        context = {}
        if github is not None and item.get("url"):
            try:  # author / size / age meta + raw body (for summarization only)
                context = github.fetch_item_context(item["url"]) or {}
            except Exception as exc:
                log.warning("todo context fetch failed for %s: %s", item["url"], exc)
        candidates.append({"key": key, "item": item, "summary": long_summary,
                           "meta": context.get("meta", ""), "body": context.get("body", "")})

    written = _summarize_details(
        llm, [{"id": c["key"], "summary": c["summary"], "body": c["body"]}
              for c in candidates if c["body"]],
    )

    added = []
    for c in candidates:
        item = c["item"]
        # written task summary first, notification sentence as fallback —
        # the raw body never reaches the detail field
        detail = written.get(c["key"]) or c["summary"]
        if c["meta"]:
            detail = f"{detail} — {c['meta']}"[:600]
        todo = store.upsert(
            c["key"],
            # short imperative label as the title; the summary as detail
            title=item.get("todo") or c["summary"],
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

    from ..urgency import urgency

    # most urgent first — the digest email and website consume this order
    open_items = sorted(store.open_items(), key=urgency, reverse=True)
    return {"added": added, "closed": closed, "open": open_items,
            "open_count": len(open_items)}
