"""Action handler implementations, grouped by domain: todos, reading list, run
control, profile, web search, reminders, routines, and task planning.

Each `_handler(settings, params) -> str` performs the effect against the live
stores and returns one human-readable line describing what actually happened —
replies are built from these outcomes, never from LLM claims. Store/LLM imports
are lazy so importing the registry stays cheap.
"""

import subprocess
import sys

from assistant.platform.config import Settings
from assistant.agent.state import load_state
from assistant.agent.todo_store import ReadingList, TodoStore


# ── todos ────────────────────────────────────────────────────────────

def _add_todo(settings: Settings, p: dict) -> str:
    """Upsert an open todo from `title` (+ optional `due`, `source`). Returns the
    new id, or notes it was already tracked."""
    source = p.get("source", "chat")
    extra = {"due": p["due"]} if p.get("due") else {}
    item = TodoStore(settings.profile_dir).upsert(
        f"{source}:{p['title']}", title=p["title"], source=source,
        priority="yellow", **extra)
    return (f"added todo {item['id']}: {item['title']}" if item
            else "todo already tracked")


def _done_todo(settings: Settings, p: dict) -> str:
    """Close the open todo with `id`; reports whether one was found."""
    item_id = str(p.get("id", ""))
    ok = TodoStore(settings.profile_dir).mark_done(item_id)
    return f"todo {item_id} marked done" if ok else f"no open todo {item_id!r}"


def _list_todos(settings: Settings, p: dict) -> str:
    """List open todos (id, title, source, since, due) — one per line."""
    lines = []
    for t in TodoStore(settings.profile_dir).open_items():
        due = f" due:{t['due']}" if t.get("due") else ""
        lines.append(f"[{t['id']}] {t['title']} ({t.get('source', '')}, "
                     f"since {t.get('created', '')}{due})")
    return "\n".join(lines) or "(no open todos)"


# ── reading list ─────────────────────────────────────────────────────

def _done_reading(settings: Settings, p: dict) -> str:
    """Mark the reading item `id` read; reports whether one was found."""
    item_id = str(p.get("id", ""))
    ok = ReadingList(settings.profile_dir).mark_done(item_id)
    return (f"reading item {item_id} marked read" if ok
            else f"no unread item {item_id!r}")


def _list_reading(settings: Settings, p: dict) -> str:
    """List unread reading-list items (id, title, url) — one per line."""
    lines = [f"[{r['id']}] {r['title']}  {r.get('url', '')}".rstrip()
             for r in ReadingList(settings.profile_dir).open_items()]
    return "\n".join(lines) or "(reading list empty)"


def _unrelated_reading(settings: Settings, p: dict) -> str:
    """Negative feedback: mark reading item `id` unrelated so future research
    digests avoid similar topics. Reports whether one was found."""
    item_id = str(p.get("id", ""))
    ok = ReadingList(settings.profile_dir).mark_unrelated(item_id)
    return (f"reading item {item_id} marked unrelated — the research digest will "
            "avoid similar topics" if ok else f"no reading item {item_id!r}")


# ── run control ──────────────────────────────────────────────────────

def _enqueue(settings: Settings, kind: str, args: dict, dedupe_key: str | None = None):
    """Enqueue a background job on the durable queue for **this** user (the
    handler already holds the authenticated `settings.uid` — a job never names
    another user). Returns the job id, or None if a duplicate was deduped. Only
    used in `multi_tenant`; single_user keeps the legacy detached-CLI path (§6)."""
    from assistant.platform.jobs import JobQueue
    return JobQueue(settings.shared_dir).enqueue(settings.uid, kind, args, dedupe_key)


def _trigger_run(settings: Settings, p: dict) -> str:
    """Start a full daily run in the background (optionally `resume` the last
    one), unless a run is already in progress.

    `multi_tenant`: enqueue on the durable per-uid queue (an in-process worker
    runs it — no CLI, no forgeable identity). `single_user` (legacy): detach via
    Popen exactly as before."""
    state = load_state(settings.state_file) or {}
    if state.get("phase") not in (None, "done"):
        return f"a run is already in progress ({state.get('run_id')})"
    if settings.deployment_mode == "multi_tenant":
        self_ = _enqueue(settings, "run", {"resume": bool(p.get("resume"))})
        return ("daily run queued" if self_ else
                "a daily run is already queued for you")
    cmd = [sys.executable, "-m", "assistant.cli", "run"]
    if p.get("resume"):
        cmd.append("--resume")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    log_file = (settings.data_dir / "chat_run.log").open("a")
    subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT,
                     start_new_session=True)
    return "daily run started in the background"


def _reboot(settings: Settings, p: dict) -> str:
    """Restart the serve daemon so it reloads code — spawned detached with a
    short delay so THIS reply is delivered before the daemon goes down."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    log_file = (settings.data_dir / "serve.log").open("a")
    subprocess.Popen([sys.executable, "-m", "assistant.cli", "reboot", "--delay", "3"],
                     stdout=log_file, stderr=subprocess.STDOUT, start_new_session=True)
    return "重启中，几秒后恢复 ♻️ (restarting the assistant, back in a few seconds)"


def _run_status(settings: Settings, p: dict) -> str:
    """Report the last run's id/phase (flagging an incomplete/resumable run) plus
    open todo and unread-reading counts."""
    state = load_state(settings.state_file) or {}
    if not state.get("run_id"):
        return "no runs yet"
    phase = state.get("phase", "?")
    line = f"last run {state['run_id']} — phase: {phase}"
    if phase not in (None, "done"):
        line += " (incomplete — `assistant run --resume` continues it)"
    open_todos = len(TodoStore(settings.profile_dir).open_items())
    backlog = len(ReadingList(settings.profile_dir).open_items())
    return f"{line}\n{open_todos} todos open, {backlog} reading items unread"


# phases the owner may run standalone; the rest need the full pipeline's
# upstream state (collect→profile→digest), so they map to trigger_run
RUNNABLE_PHASES = ("research", "website", "todos", "resume", "curate", "consolidate")


def _run_phase(settings: Settings, p: dict) -> str:
    """Run one standalone pipeline `phase`. Phases needing upstream state fall
    back to a full run; `website` runs inline (fast) and reports the real sync
    result; the rest launch in the background. Rejects unknown phases."""
    phase = str(p.get("phase", "")).strip().lower()
    if phase in ("run", "all", "daily", "digest", "collect", "profile", "deliver"):
        return _trigger_run(settings, {}) + " (that phase needs the full pipeline)"
    if phase not in RUNNABLE_PHASES:
        return (f"unknown phase {phase!r} — runnable: {', '.join(RUNNABLE_PHASES)}, "
                "or 'all' for the full daily run")
    if phase == "website":  # fast — run it inline and report the real result
        from assistant.agent.profile_store import ProfileStore
        from assistant.agent.todo_store import ReadingList, TodoStore
        from assistant.agent.urgency import urgency
        from assistant.agent.website import sync_website

        todos = sorted(TodoStore(settings.profile_dir).open_items(),
                       key=urgency, reverse=True)
        result = sync_website(settings, ProfileStore(settings.profile_dir).load(),
                              todos, reading=ReadingList(settings.profile_dir).open_items())
        return f"website sync: {result.get('status')} {result.get('url', '')}".strip()
    if settings.deployment_mode == "multi_tenant":
        _enqueue(settings, "run_phase", {"phase": phase})
        return (f"phase '{phase}' queued — I'll have the results on the "
                "website/next digest, or ask me in a few minutes")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    log_file = (settings.data_dir / "phase_run.log").open("a")
    subprocess.Popen([sys.executable, "-m", "assistant.cli", "run-phase", phase],
                     stdout=log_file, stderr=subprocess.STDOUT, start_new_session=True)
    return (f"phase '{phase}' started in the background — I'll have the results "
            "on the website/next digest, or ask me in a few minutes")


# ── profile ──────────────────────────────────────────────────────────

def _show_profile(settings: Settings, p: dict) -> str:
    """Return a compact summary of the owner profile (≤12 items/section), or a
    bootstrap hint when no profile exists yet."""
    from assistant.agent.profile_store import ProfileStore, render_summary

    store = ProfileStore(settings.profile_dir)
    if not store.exists():
        return "(no profile yet — run `assistant bootstrap`)"
    return render_summary(store.load(), max_items=12)


# ── web search ───────────────────────────────────────────────────────

_SEARCH_ANSWER_SYSTEM = """You answer the owner's question from the web search results below.
Be concise and concrete; cite the source URL in parentheses after each claim you take from a
result. If the results don't answer the question, say so and suggest a better query.
Answer in the language the owner used. Plain text, no markdown headings."""


def _web_search(settings: Settings, p: dict) -> str:
    """Search the web for `query` and answer from the results. Uses a grounded
    backend's own answer when available, else synthesizes one via the LLM over
    the formatted results; falls back to raw results if synthesis fails."""
    from assistant.platform.llm import LLM
    from assistant.platform.search import format_results, web_search_answer

    query = str(p.get("query", "")).strip()
    out = web_search_answer(query, max_results=8, settings=settings)
    results = out["results"]
    if out["answer"]:  # grounded backend (Gemini) already searched + answered
        sources = "\n".join(f"- {r['title']} {r['url']}" for r in results[:5])
        return out["answer"] + (f"\n\nsources:\n{sources}" if sources else "")
    if not results:
        return f"web search for {query!r} returned nothing (backend may be rate-limited — try again)"
    try:
        answer = LLM(settings).complete(
            f"## Question\n{query}\n\n## Search results\n{format_results(results, limit=8)}",
            system=_SEARCH_ANSWER_SYSTEM, max_tokens=8000).strip()
    except Exception:  # search worked, synthesis didn't — raw results still help
        answer = ""
    return answer or "top results:\n" + format_results(results)


# ── reminders ────────────────────────────────────────────────────────

def _set_reminder(settings: Settings, p: dict) -> str:
    """Schedule a one-shot WeChat reminder: parse `when` (+30m/+2h/HH:MM/
    'YYYY-MM-DD HH:MM') and store `message`. Returns the reminder id + fire time,
    or a format hint on an unparseable `when`."""
    from assistant.platform.notify import ReminderStore, parse_when

    due = parse_when(p.get("when", ""))
    if due is None:
        return (f"couldn't parse when={p.get('when')!r} — use '+30m', '+2h', "
                "'HH:MM', or 'YYYY-MM-DD HH:MM'")
    reminder = ReminderStore(settings.data_dir).add(str(p.get("message", "")), due)
    return (f"reminder {reminder['id']} set for {reminder['due_at']} — "
            "I'll ping you on WeChat")


def _list_reminders(settings: Settings, p: dict) -> str:
    """List pending reminders (id, due time, message) — one per line."""
    from assistant.platform.notify import ReminderStore

    pending = ReminderStore(settings.data_dir).pending()
    return "\n".join(f"[{r['id']}] {r['due_at']} — {r['message']}"
                     for r in pending) or "(no pending reminders)"


def _cancel_reminder(settings: Settings, p: dict) -> str:
    """Cancel the pending reminder with `id`; reports whether one was found."""
    from assistant.platform.notify import ReminderStore

    reminder_id = str(p.get("id", ""))
    ok = ReminderStore(settings.data_dir).cancel(reminder_id)
    return (f"reminder {reminder_id} cancelled" if ok
            else f"no pending reminder {reminder_id!r}")


# ── routines ─────────────────────────────────────────────────────────

def _create_routine(settings: Settings, p: dict) -> str:
    """Create a recurring routine: `task` at `time` on `days`, optionally gated
    on a free-text `condition` checked at fire time, optionally bound to a
    saved `workflow` (fired deterministically, no text interpretation).
    Returns the routine id + schedule, or a format hint on invalid input."""
    from assistant.agent.routines import RoutineStore

    if not str(p.get("task", "")).strip() and not str(p.get("workflow", "")).strip():
        return "create_routine needs a task (or a workflow id to bind)"
    routine = RoutineStore(settings.data_dir).add(
        task=str(p.get("task", "")), time=str(p.get("time", "")),
        days=str(p.get("days") or "daily"), condition=str(p.get("condition") or ""),
        workflow=str(p.get("workflow") or ""))
    if routine is None:
        return ("couldn't create routine — time must be HH:MM, days one of "
                "daily/workdays/weekends, 'mon,wed,fri', 'monthly:<1-31>', "
                "or 'yearly:<MM-DD>', and workflow (if given) a wf-id like wf3")
    when = f"{routine['days']} at {routine['time']}"
    gate = f", only when: {routine['condition']}" if routine["condition"] else ""
    return f"routine {routine['id']} created — {when}{gate}: {routine['task']}"


def _list_routines(settings: Settings, p: dict) -> str:
    """List active routines (id, schedule, optional condition, task)."""
    from assistant.agent.routines import RoutineStore

    lines = [f"[{r['id']}] {r['days']} {r['time']}"
             + (f" (if: {r['condition']})" if r.get("condition") else "")
             + f" — {r['task']}"
             + (f" (last checked {r['last_checked']})" if r.get("last_checked")
                else " (never checked yet)")
             for r in RoutineStore(settings.data_dir).active()]
    return "\n".join(lines) or "(no routines)"


def _cancel_routine(settings: Settings, p: dict) -> str:
    """Cancel the active routine with `id`; reports whether one was found."""
    from assistant.agent.routines import RoutineStore

    routine_id = str(p.get("id", ""))
    ok = RoutineStore(settings.data_dir).cancel(routine_id)
    return (f"routine {routine_id} cancelled" if ok
            else f"no active routine {routine_id!r}")


# ── task planning ────────────────────────────────────────────────────

_PLAN_SYSTEM = """You are the owner's personal task planner. You get a novel task request (booking,
arranging, researching, errands). Produce a concrete, realistic plan.

Be honest about capabilities: the agent has NO calendar or payment access — steps needing
those are the owner's, but make them trivially easy (draft the message to send, name the
criteria). Steps the agent CAN do: web search (results may be provided below — use them to
name real candidates), track the task as a todo, remind via the daily digest, draft text,
reason over the owner's profile/todos.

Respond with ONLY JSON:
{"title": "<short imperative todo title>",
 "due": "YYYY-MM-DD or null",
 "steps": [{"who": "agent|owner", "step": "<one concrete action>"}],
 "next": "<the single next action to take>"}
3-6 steps. Never invent facts — name candidates only when the search results support them."""


def _plan_task(settings: Settings, p: dict) -> str:
    """Plan a novel multi-step `request`: web-search for candidates, ask the LLM
    for a JSON plan grounded in the owner profile + results, and track it as a
    todo. Returns the plan (title, numbered steps, next action), or a retry hint
    when no plan is produced."""
    from assistant.platform.llm import LLM
    from assistant.agent.profile_store import ProfileStore, render_summary
    from assistant.platform.search import format_results, web_search

    request = str(p.get("request", "")).strip()
    profile_store = ProfileStore(settings.profile_dir)
    context = (render_summary(profile_store.load()) if profile_store.exists() else "")
    findings = web_search(request, max_results=6, settings=settings)  # [] on failure
    plan = LLM(settings).complete_json(
        f"## Owner profile\n{context}\n\n"
        + (f"## Web search results for the request\n{format_results(findings)}\n\n"
           if findings else "")
        + f"## Task request\n{request}",
        system=_PLAN_SYSTEM, max_tokens=8000)
    if not isinstance(plan, dict) or not plan.get("steps"):
        return f"couldn't produce a plan for {request!r} — try rephrasing"

    title = str(plan.get("title") or request)[:120]
    step_lines = [f"{i}. [{s.get('who', '?')}] {s.get('step', '')}"
                  for i, s in enumerate(plan.get("steps", [])[:6], 1)]
    detail = (" / ".join(step_lines))[:580]
    extra = {"due": plan["due"]} if plan.get("due") else {}
    todo = TodoStore(settings.profile_dir).upsert(
        f"plan:{title}", title=title, detail=detail, source="chat",
        priority="yellow", **extra)
    lines = [f"planned: {title}" + (f" (todo {todo['id']})" if todo
                                    else " (already tracked)")]
    lines += step_lines
    if plan.get("next"):
        lines.append(f"→ next: {plan['next']}")
    return "\n".join(lines)


# ── finance ──────────────────────────────────────────────────────────

def _log_date(p: dict) -> tuple[str, str | None]:
    """Resolve a log action's `date` param to an absolute event date.

    Returns `(when, error)`. `when == ""` means the day was unstated → the store
    defaults to today (the legitimate case). A non-None `error` means the model
    gave an explicit date we could not parse — the caller must **reject** the log
    (never silently record today). Already-absolute and relative days
    (今天/昨天/前天/N天前/…) both resolve via `resolve_day`."""
    from datetime import date as _date

    from assistant.platform.timeutil import resolve_day

    raw = str(p.get("date", "")).strip()
    if not raw:
        return "", None
    resolved = resolve_day(raw, _date.today())
    if resolved is None:
        return "", f'没听懂日期"{raw}"，请用「昨天／前天／2026-07-20」这样的说法重说'
    return resolved, None


def _log_transaction(settings: Settings, p: dict) -> str:
    """Append an income/expense record to the finance ledger. Reports the new
    id, a rejection reason, or the existing record when this would be a
    duplicate (same kind/amount/currency/date/time/note)."""
    from assistant.agent.finance_store import FinanceStore

    when, date_err = _log_date(p)
    if date_err:
        return f"transaction rejected — {date_err}"
    status, record = FinanceStore(settings.profile_dir).add(
        p.get("kind", ""), p.get("amount"), category=p.get("category", "other"),
        note=p.get("note", ""), when=when, time=p.get("time", ""),
        currency=p.get("currency") or settings.finance_currency,
        source=p.get("source", "chat"))
    if status == "invalid":
        return ("transaction rejected — need kind=income|expense, amount>0, "
                "date as YYYY-MM-DD and time as HH:MM if given")
    if status == "duplicate":
        from assistant.agent.finance_store import timestamp_of

        return (f"NOT logged — duplicate of {record['id']} "
                f"({timestamp_of(record)} {record['type']} {record['amount']} "
                f"{record['currency']}"
                + (f" · {record['note']}" if record.get("note") else "")
                + "); add a differing time/note if it really is a second transaction")
    from assistant.agent.finance_store import timestamp_of

    line = (f"logged {record['id']}: {record['type']} {record['amount']} "
            f"{record['currency']} · {record['category']}"
            + (f" · {record['note']}" if record["note"] else "")
            + f" · {timestamp_of(record)}")
    lookalikes = FinanceStore(settings.profile_dir).similar(record)
    if lookalikes:
        ids = ", ".join(f"{r['id']} ({timestamp_of(r)}"
                        + (f" · {r['note']}" if r.get("note") else "") + ")"
                        for r in lookalikes[:3])
        line += (f"\n⚠ same amount already recorded that day: {ids} — if this "
                 f"is the same bill, void one ({record['id']} or the older id)")
    return line


def _void_transaction(settings: Settings, p: dict) -> str:
    """Void (never delete) the ledger record with `id`."""
    from assistant.agent.finance_store import FinanceStore

    record_id = str(p.get("id", ""))
    ok = FinanceStore(settings.profile_dir).void(record_id)
    return (f"transaction {record_id} voided" if ok
            else f"no active transaction {record_id!r}")


def _list_transactions(settings: Settings, p: dict) -> str:
    """List records (newest first, capped at 20), optionally for one
    'YYYY-MM' `month` — one per line."""
    from assistant.agent.finance_store import FinanceStore

    from assistant.agent.finance_store import timestamp_of

    recs = FinanceStore(settings.profile_dir).records(
        str(p["month"]) if p.get("month") else None)
    lines = [f"[{r['id']}] {timestamp_of(r)} {r['type']} {r['amount']} "
             f"{r['currency']} · {r['category']}"
             + (f" · {r['note']}" if r.get("note") else "")
             for r in reversed(recs[-20:])]
    return "\n".join(lines) or "(no transactions recorded)"


def _finance_summary(settings: Settings, p: dict) -> str:
    """Deterministic monthly totals (income, spend, net, savings rate, top
    categories) for `month` (default: current)."""
    from assistant.agent.finance_store import FinanceStore, render_summary

    store = FinanceStore(settings.profile_dir)
    return render_summary(store.summary(str(p["month"]) if p.get("month") else None),
                          currency=settings.finance_currency, store=store)


def _recategorize_transaction(settings: Settings, p: dict) -> str:
    """Move ledger record `id` to `category`; reports old → new or why not."""
    from assistant.agent.finance_store import CATEGORIES, FinanceStore

    record_id, category = str(p.get("id", "")), str(p.get("category", "")).strip().lower()
    if not category:
        return "recategorize needs a category, e.g. housing"
    old = FinanceStore(settings.profile_dir).set_category(record_id, category)
    if old is None:
        return f"no active transaction {record_id!r}"
    hint = "" if category in CATEGORIES else f" (note: not a standard category — {', '.join(CATEGORIES)})"
    return f"{record_id} recategorized: {old} → {category}{hint}"


# ── health ───────────────────────────────────────────────────────────

def _health_record_line(record: dict) -> str:
    """One outcome line for a logged health record."""
    from assistant.agent.finance_store import timestamp_of

    detail = record.get("description") or record.get("activity") or ""
    if record["kind"] == "exercise" and record.get("duration_min") is not None:
        detail += f" {record['duration_min']}min"
    if record["kind"] == "weight":
        detail = f"{record['weight_kg']} kg"
    extras = " ".join(f"{k.split('_')[0]} {record[k]}"
                      for k in ("calories_kcal", "protein_g", "carbs_g", "fat_g")
                      if record.get(k) is not None)
    return (f"logged {record['id']}: {record['kind']} · {detail}"
            + (f" · {extras}" if extras else "")
            + f" · {timestamp_of(record)}")


def _query_health(settings: Settings, p: dict) -> str:
    """Retrieve health records for a day / range / kind / food-text, with range
    totals — so the agent can answer about any date, not just the context
    snapshot. `date` is a single-day shortcut for start==end."""
    from assistant.agent.health_store import HealthStore

    store = HealthStore(settings.profile_dir)
    day = str(p.get("date") or "").strip()
    start = day or str(p.get("start") or "").strip()
    end = day or str(p.get("end") or "").strip()
    kind = str(p.get("kind") or "").strip() or None
    contains = str(p.get("contains") or "").strip()
    recs = store.query(start=start, end=end, kind=kind, contains=contains)
    scope = (f"{start or '起始'}~{end or '至今'}"
             + (f" {kind}" if kind else "") + (f" '{contains}'" if contains else ""))
    if not recs:
        return f"no health records for {scope}"
    meals = [r for r in recs if r["kind"] == "meal"]
    kcal = round(sum(r.get("calories_kcal") or 0 for r in meals))
    prot = round(sum(r.get("protein_g") or 0 for r in meals), 1)
    exmin = round(sum(r.get("duration_min") or 0 for r in recs if r["kind"] == "exercise"))
    head = (f"{len(recs)} record(s) · {scope}"
            + (f" · {len(meals)} meals ~{kcal}kcal ~{prot}g protein" if meals else "")
            + (f" · exercise {exmin}min" if exmin else "") + ":")
    return "\n".join([head] + [
        f"[{r['id']}] {r['date']} {r.get('time', '')} {r['kind']} · "
        + (r.get("description") or r.get("activity") or "")
        + (f" · {r['calories_kcal']}kcal" if r.get("calories_kcal") else "")
        + (f" · {r['protein_g']}g蛋白" if r.get("protein_g") else "")
        + (f" · {r['duration_min']}min" if r.get("duration_min") else "")
        + (f" · {r['weight_kg']}kg" if r.get("weight_kg") else "")
        for r in recs])


def _query_transactions(settings: Settings, p: dict) -> str:
    """Retrieve finance records for a day / range / category / kind / note-text,
    with income/expense/net totals — arbitrary lookups beyond the current-month
    context snapshot."""
    from assistant.agent.finance_store import FinanceStore, timestamp_of

    store = FinanceStore(settings.profile_dir)
    day = str(p.get("date") or "").strip()
    start = day or str(p.get("start") or "").strip()
    end = day or str(p.get("end") or "").strip()
    category = str(p.get("category") or "").strip() or None
    kind = str(p.get("kind") or "").strip() or None
    contains = str(p.get("contains") or "").strip()
    recs = store.query(start=start, end=end, category=category, kind=kind, contains=contains)
    scope = (f"{start or '起始'}~{end or '至今'}" + (f" {category}" if category else "")
             + (f" {kind}" if kind else "") + (f" '{contains}'" if contains else ""))
    if not recs:
        return f"no transactions for {scope}"
    income = sum(r["amount"] for r in recs if r["type"] == "income")
    expense = sum(r["amount"] for r in recs if r["type"] == "expense")
    cur = recs[0].get("currency", "")
    head = (f"{len(recs)} record(s) · {scope} · income {round(income, 2)} "
            f"expense {round(expense, 2)} net {round(income - expense, 2)} {cur}:")
    return "\n".join([head] + [
        f"[{r['id']}] {timestamp_of(r)} {r['type']} {r['amount']} {r['currency']} "
        f"· {r['category']}" + (f" · {r['note']}" if r.get("note") else "")
        for r in recs])


def _log_meal(settings: Settings, p: dict) -> str:
    """Record a meal (description + optional calorie/macro estimates)."""
    from assistant.agent.health_store import HealthStore

    when, date_err = _log_date(p)
    if date_err:
        return f"meal rejected — {date_err}"
    status, record = HealthStore(settings.profile_dir).add(
        "meal", when=when, time=p.get("time", ""),
        description=p.get("description", ""), note=p.get("note", ""),
        calories_kcal=p.get("calories_kcal"), protein_g=p.get("protein_g"),
        carbs_g=p.get("carbs_g"), fat_g=p.get("fat_g"))
    if status == "invalid":
        return "meal rejected — needs a description (date YYYY-MM-DD / time HH:MM if given)"
    if status == "duplicate":
        return f"NOT logged — {record['id']} already covers that meal time ({record.get('description', '')})"
    return _health_record_line(record)


def _log_exercise(settings: Settings, p: dict) -> str:
    """Record an exercise session (activity + duration minutes)."""
    from assistant.agent.health_store import HealthStore

    when, date_err = _log_date(p)
    if date_err:
        return f"exercise rejected — {date_err}"
    status, record = HealthStore(settings.profile_dir).add(
        "exercise", when=when, time=p.get("time", ""),
        activity=p.get("activity", ""), duration_min=p.get("duration_min"),
        note=p.get("note", ""))
    if status == "invalid":
        return "exercise rejected — needs an activity (duration_min optional, 1-1440)"
    if status == "duplicate":
        return f"NOT logged — {record['id']} already covers that session time"
    return _health_record_line(record)


def _log_weight(settings: Settings, p: dict) -> str:
    """Record a body-weight measurement (kg)."""
    from assistant.agent.health_store import HealthStore

    when, date_err = _log_date(p)
    if date_err:
        return f"weight rejected — {date_err}"
    status, record = HealthStore(settings.profile_dir).add(
        "weight", when=when, time=p.get("time", ""),
        weight_kg=p.get("weight_kg"), note=p.get("note", ""))
    if status == "invalid":
        return "weight rejected — needs weight_kg (20-400)"
    if status == "duplicate":
        return f"NOT logged — duplicate of {record['id']} ({record['weight_kg']} kg)"
    return _health_record_line(record)


def _set_health_profile(settings: Settings, p: dict) -> str:
    """Update static body facts (sex / birth_year / height_cm)."""
    from assistant.agent.health_store import HealthStore

    profile = HealthStore(settings.profile_dir).set_profile(
        sex=p.get("sex"), birth_year=p.get("birth_year"),
        height_cm=p.get("height_cm"))
    facts = ", ".join(f"{k}={v}" for k, v in profile.items()) or "(empty)"
    return f"health profile now: {facts}"


def _add_health_need(settings: Settings, p: dict) -> str:
    """Track a nutrient/ingredient the owner wants covered."""
    from assistant.agent.health_store import HealthStore

    need = HealthStore(settings.profile_dir).add_need(
        p.get("item", ""), why=p.get("why", ""))
    return (f"tracking need {need['id']}: {need['item']}" if need
            else "need already tracked (or empty)")


def _done_health_need(settings: Settings, p: dict) -> str:
    """Mark a tracked need as covered."""
    from assistant.agent.health_store import HealthStore

    need_id = str(p.get("id", ""))
    ok = HealthStore(settings.profile_dir).done_need(need_id)
    return f"need {need_id} marked covered" if ok else f"no open need {need_id!r}"


def _health_summary(settings: Settings, p: dict) -> str:
    """Deterministic health picture for the trailing window (default 7 days)."""
    from assistant.agent.health_store import HealthStore, render_summary

    try:
        days = max(1, min(90, int(p.get("days") or 7)))
    except (TypeError, ValueError):
        days = 7
    return render_summary(HealthStore(settings.profile_dir).summary(days))


# ── task execution ───────────────────────────────────────────────────

def _execute_task(settings: Settings, p: dict) -> str:
    """Run a novel multi-step task agentically in the background (plan, act
    via actions, review outcomes, adapt); the final report is pushed to
    WeChat. Detached like trigger_run so chat replies immediately."""
    request = str(p.get("request", "")).strip()
    if not request:
        return "execute_task needs the request"
    if settings.deployment_mode == "multi_tenant":
        _enqueue(settings, "task", {"request": request})
        return ("task queued — I'll work through it step by step and message "
                "you the result on WeChat")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    log_file = (settings.data_dir / "task_run.log").open("a")
    subprocess.Popen([sys.executable, "-m", "assistant.cli", "task", request],
                     stdout=log_file, stderr=subprocess.STDOUT,
                     start_new_session=True)
    return ("task started in the background — I'll work through it step by "
            "step and message you the result on WeChat")


def _dispatch_task_record(settings: Settings, task_id: str) -> bool:
    """Launch execution of a `queued` task record — the ONE dispatch path for
    approvals, workflow starts, and orphan rescues. `multi_tenant`: enqueue a
    resume job with an **active-scoped** dedupe key, so an alive job dedupes
    (at most one worker per record) while a dead/failed one can be
    re-dispatched. `single_user`: detached CLI resume; a racing double-spawn
    is resolved by `_load_approved`'s locked queued→running transition (the
    loser exits as a no-op). Returns whether a new dispatch happened."""
    if settings.deployment_mode == "multi_tenant":
        from assistant.platform.jobs import JobQueue

        job = JobQueue(settings.shared_dir).enqueue(
            settings.uid, "task", {"approved_task_id": task_id},
            dedupe_key=f"{settings.uid}:task_resume:{task_id}",
            dedupe_scope="active")
        return job is not None
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    log_file = (settings.data_dir / "task_run.log").open("a")
    subprocess.Popen([sys.executable, "-m", "assistant.cli", "task",
                      "--approved-id", task_id],
                     stdout=log_file, stderr=subprocess.STDOUT,
                     start_new_session=True)
    return True


def _approve_task(settings: Settings, p: dict) -> str:
    """Release an awaiting background task (paused on a risky step or a risky
    plan): under the per-user write lock, transition `awaiting_approval →
    queued` with a one-shot `pre_approved` grant (it authorizes exactly the
    stored pending action — a later risky step pauses again), then dispatch.
    Also rescues stuck records: a `queued` orphan (crash between persist and
    dispatch) is re-dispatched idempotently; a `running` record re-dispatches
    only when its queue job is dead (active-scoped dedupe decides) —
    single_user names the manual escape instead. Ids are validated before any
    path is built (task ids are file names)."""
    import json
    from datetime import datetime

    from assistant.platform.locks import data_write_lock
    from assistant.agent.task_runner import TASK_ID_RE

    task_id = str(p.get("id", "")).strip()
    if not TASK_ID_RE.match(task_id):
        return (f"no task {task_id!r} — ids look like task-YYYYMMDD-HHMMSS-xxxxxx "
                "(see the approval message)")
    path = settings.data_dir / "tasks" / f"{task_id}.json"
    with data_write_lock(settings.data_dir):
        if not path.exists():
            return f"no task {task_id!r}"
        try:
            record = json.loads(path.read_text())
        except ValueError:
            return f"task {task_id} record is unreadable"
        status = record.get("status")
        if status == "awaiting_approval":
            record["status"] = "queued"
            record["pre_approved"] = True   # one-shot: consumed at resume,
            record["approved_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2))
            tmp.replace(path)
        elif status == "queued":
            pass                            # orphan rescue: re-dispatch below
        elif status == "running":
            if settings.deployment_mode == "multi_tenant":
                if _dispatch_task_record(settings, task_id):
                    return (f"task {task_id} looked stuck — re-dispatched, "
                            "report arrives on WeChat")
                return f"task {task_id} is already running"
            return (f"task {task_id} is already running — if it crashed, run "
                    f"`assistant task --approved-id {task_id} --force-resume`")
        else:
            return f"task {task_id} is {status or 'unknown'} — nothing to approve"
    _dispatch_task_record(settings, task_id)
    return f"task {task_id} approved — executing now, report arrives on WeChat"


# ── workflows (owner-authored reusable procedures) ───────────────────

_WF_ID_RE = None  # compiled lazily


def _wf_id(p: dict) -> str | None:
    """Validated workflow id from params, or None (ids are store keys)."""
    global _WF_ID_RE
    if _WF_ID_RE is None:
        import re

        _WF_ID_RE = re.compile(r"^wf\d+$")
    wid = str(p.get("id", "")).strip()
    return wid if _WF_ID_RE.match(wid) else None


def _create_workflow(settings: Settings, p: dict) -> str:
    """Save a reusable workflow. `steps` must be supplied by the model (it has
    the conversation context; drafting here would run an LLM call inside the
    chat executor's write lock) — a missing/invalid `steps` outcome routes
    through the repair round so the model supplies them on retry."""
    from assistant.agent.workflow_store import WorkflowStore, WorkflowStoreError, render_workflow, valid_steps

    if valid_steps(p.get("steps")) is None:
        return ("create_workflow rejected — needs steps: a list of 1-6 short "
                "concrete step strings (write them from the conversation)")
    try:
        status, wf = WorkflowStore(settings.profile_dir).create(
            p.get("name") or str(p.get("description", ""))[:40],
            p.get("description", ""), p.get("steps"), verify=p.get("verify", ""))
    except WorkflowStoreError as exc:
        return f"workflow store unavailable: {exc}"
    if status == "invalid":
        return ("create_workflow rejected — needs name, description, and 1-6 "
                "non-empty steps")
    if status == "duplicate":
        return (f"an active workflow already has that name: \n"
                + render_workflow(wf) + "\n(update_workflow edits it)")
    if status == "full":
        return "workflow limit reached (20 active) — retire one first"
    return ("workflow saved:\n" + render_workflow(wf)
            + f"\nRun it anytime (\"运行 {wf['name']}\" → run_workflow {wf['id']}); "
              f"schedule it with a routine bound to {wf['id']}")


def _run_workflow(settings: Settings, p: dict) -> str:
    """Execute a saved workflow: mint a pre-planned task record (status
    `queued`, plan = the workflow's steps/verify) under the write lock, then
    dispatch through the resume path — so a queue retry RESUMES from persisted
    steps instead of restarting, and every risky step still pauses for
    approval."""
    import json
    import uuid
    from datetime import datetime

    from assistant.platform.locks import data_write_lock
    from assistant.agent.workflow_store import WorkflowStore, WorkflowStoreError

    wid = _wf_id(p)
    if wid is None:
        return "run_workflow needs the workflow id, e.g. wf3 (see the saved list)"
    try:
        wf = WorkflowStore(settings.profile_dir).get(wid)
    except WorkflowStoreError as exc:
        return f"workflow store unavailable: {exc}"
    if wf is None:
        return f"no active workflow {wid!r}"
    task_id = datetime.now().strftime("task-%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    record = {"id": task_id,
              "request": f"[workflow {wf['id']}: {wf['name']}] {wf['description']}",
              "started": datetime.now().strftime("%Y-%m-%d %H:%M"),
              "status": "queued", "workflow_id": wf["id"],
              "plan": {"steps": list(wf["steps"]), "verify": wf.get("verify", ""),
                       "risks": "",
                       "milestones": [{"step": s, "done": False}
                                      for s in wf["steps"]]},
              "steps": [], "report": ""}
    with data_write_lock(settings.data_dir):
        tasks_dir = settings.data_dir / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        path = tasks_dir / f"{task_id}.json"
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2))
        tmp.replace(path)
    _dispatch_task_record(settings, task_id)
    return (f"workflow {wf['id']} \"{wf['name']}\" started ({task_id}) — "
            "I'll follow its steps and report on WeChat; outward steps will "
            "ask for your approval first")


def _show_workflow(settings: Settings, p: dict) -> str:
    """Full steps/verify/stats of one workflow (retired ones stay inspectable)."""
    from assistant.agent.workflow_store import WorkflowStore, WorkflowStoreError, render_workflow

    wid = _wf_id(p)
    if wid is None:
        return "show_workflow needs the workflow id, e.g. wf3"
    try:
        wf = WorkflowStore(settings.profile_dir).get_any(wid)
    except WorkflowStoreError as exc:
        return f"workflow store unavailable: {exc}"
    return render_workflow(wf) if wf else f"no workflow {wid!r}"


def _update_workflow(settings: Settings, p: dict) -> str:
    """Edit an active workflow's name/description/steps/verify."""
    from assistant.agent.workflow_store import WorkflowStore, WorkflowStoreError, render_workflow

    wid = _wf_id(p)
    if wid is None:
        return "update_workflow needs the workflow id, e.g. wf3"
    try:
        status, wf = WorkflowStore(settings.profile_dir).update(
            wid, name=p.get("name"), description=p.get("description"),
            steps=p.get("steps"), verify=p.get("verify"))
    except WorkflowStoreError as exc:
        return f"workflow store unavailable: {exc}"
    if status == "missing":
        return f"no active workflow {wid!r}"
    if status == "conflict":
        return f"rename rejected — {wf['id']} already uses that name"
    if status == "invalid":
        return ("update rejected — name/description must be non-empty; steps, "
                "when given, a list of 1-6 short strings")
    return "workflow updated:\n" + render_workflow(wf)


def _retire_workflow(settings: Settings, p: dict) -> str:
    """Retire a workflow (never deleted) and cancel routines bound to it, so a
    schedule can't keep firing a procedure the owner removed."""
    from assistant.agent.routines import RoutineStore
    from assistant.agent.workflow_store import WorkflowStore, WorkflowStoreError

    wid = _wf_id(p)
    if wid is None:
        return "retire_workflow needs the workflow id, e.g. wf3"
    try:
        ok = WorkflowStore(settings.profile_dir).retire(wid)
    except WorkflowStoreError as exc:
        return f"workflow store unavailable: {exc}"
    if not ok:
        return f"no active workflow {wid!r}"
    routines = RoutineStore(settings.data_dir)
    cancelled = [r["id"] for r in routines.active() if r.get("workflow") == wid]
    for rid in cancelled:
        routines.cancel(rid)
    note = f" (also cancelled bound routine(s): {', '.join(cancelled)})" if cancelled else ""
    return (f"workflow {wid} retired{note} — its history stays in git; a task "
            "already running finishes, pending ones are cancelled at start")


# ── self-evolution (learned behavior) ────────────────────────────────

def _learn_preference(settings: Settings, p: dict) -> str:
    """Store a durable behavior rule from the owner's direct feedback."""
    from assistant.agent.lessons_store import LessonsStore

    lesson = LessonsStore(settings.profile_dir).learn(
        p.get("rule", ""), why=p.get("why", ""), source="owner")
    return (f"learned {lesson['id']}: {lesson['rule']}" if lesson
            else "not stored — empty or already covered by an active lesson")


def _retire_preference(settings: Settings, p: dict) -> str:
    """Retire a learned rule by id (never deleted; git keeps the history)."""
    from assistant.agent.lessons_store import LessonsStore

    lesson_id = str(p.get("id", ""))
    ok = LessonsStore(settings.profile_dir).retire(lesson_id)
    return (f"lesson {lesson_id} retired" if ok
            else f"no active lesson {lesson_id!r}")


def _list_preferences(settings: Settings, p: dict) -> str:
    """List active learned rules with provenance."""
    from assistant.agent.lessons_store import LessonsStore

    lines = [f"[{l['id']}] ({l['source']}, {l['created']}) {l['rule']}"
             + (f" — {l['why']}" if l.get("why") else "")
             for l in LessonsStore(settings.profile_dir).active()]
    return "\n".join(lines) or "(no learned rules yet)"


def _self_evolve(settings: Settings, p: dict) -> str:
    """Analyze recent chats/tasks and distill new behavior lessons now."""
    from assistant.platform.llm import LLM
    from assistant.agent.tasks.evolve import evolve

    result = evolve(settings, LLM(settings))
    if not result["reviewed"]:
        return "nothing recent to learn from"
    if not result["learned"]:
        return (f"reviewed recent interactions — no new durable lesson "
                f"({len(result['proposed'])} proposal(s) were duplicates or empty)")
    return "learned:\n" + "\n".join(f"[{l['id']}] {l['rule']}"
                                    for l in result["learned"])
