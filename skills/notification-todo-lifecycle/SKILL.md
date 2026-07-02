---
name: notification-todo-lifecycle
description: Turning GitHub notifications into todos that stay truthful — stable URL keys (notification ids churn), payload.size for push counts, API-monitored auto-close (merged/closed/already-reviewed)
trigger: todo/action items derived from notifications duplicate across days, stay open after the PR merged, or push events report "0 commit(s)"
modules: [todos, collectors]
status: active
created_at: 2026-07-02
last_used_at: 2026-07-02
run_count: 0
---

## Diagnose
- Duplicate todos: notification ids and `updated_at` change on every thread
  update, so id-based dedup creates a new todo per bump.
- Stale todos: nothing ever closes them — the list becomes noise within a week.
- "Pushed 0 commit(s)": the events-API PushEvent `commits` array is often empty
  (fine-grained PATs, private repos); the true count is `payload["size"]`.

## Fix
(`src/assistant/tasks/todos.py`, `collectors/github.py`, `todo_store.py`)
1. Dedup key = the item's **HTML URL** (stable per PR/issue), not notification
   id. `upsert` blocks only while an item with the same key is open — after
   close, a re-request legitimately creates a fresh todo.
2. Monitor pass runs BEFORE adding new todos each day: for each open github
   todo call `check_finished(url)` — PR merged / PR closed / owner already
   submitted a review (`GET /pulls/N/reviews`) / issue closed → auto-close with
   a reason, reported in the email as "☑️ Auto-completed".
3. Close = status flip with `closed: auto`, never deletion — history stays.
4. Enrich the todo description from the item API (author, files, +/−, age,
   body snippet) so the todo is actionable without clicking through.
5. Push counts: `payload.get("size") or len(commits)`.

## Verification
Unit: `test_todos_website.py:test_update_todos_from_digest_and_resume` (dedupe,
short titles, enrichment, merged→auto-close). Live: run the todos phase twice —
second run adds nothing and closes anything you reviewed in between.

## Anti-patterns
- Keying dedup on `notification id + updated_at` — that's a per-update value.
- A todo list without a completion monitor — it decays into noise and gets
  ignored.
- Deleting closed todos — you lose the audit trail and re-add loops become
  undebuggable.
