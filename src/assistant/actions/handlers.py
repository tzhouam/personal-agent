"""Action handler implementations, grouped by domain: todos, reading list, run
control, profile, web search, reminders, routines, and task planning.

Each `_handler(settings, params) -> str` performs the effect against the live
stores and returns one human-readable line describing what actually happened —
replies are built from these outcomes, never from LLM claims. Store/LLM imports
are lazy so importing the registry stays cheap.
"""

import subprocess
import sys

from ..config import Settings
from ..state import load_state
from ..todo_store import ReadingList, TodoStore


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

def _trigger_run(settings: Settings, p: dict) -> str:
    """Start a full daily run in the background (optionally `resume` the last
    one), unless a run is already in progress. Detaches via Popen and logs to
    the data dir."""
    state = load_state(settings.state_file) or {}
    if state.get("phase") not in (None, "done"):
        return f"a run is already in progress ({state.get('run_id')})"
    cmd = [sys.executable, "-m", "assistant.cli", "run"]
    if p.get("resume"):
        cmd.append("--resume")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    log_file = (settings.data_dir / "chat_run.log").open("a")
    subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT,
                     start_new_session=True)
    return "daily run started in the background"


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
        from ..profile_store import ProfileStore
        from ..todo_store import ReadingList, TodoStore
        from ..urgency import urgency
        from ..website import sync_website

        todos = sorted(TodoStore(settings.profile_dir).open_items(),
                       key=urgency, reverse=True)
        result = sync_website(settings, ProfileStore(settings.profile_dir).load(),
                              todos, reading=ReadingList(settings.profile_dir).open_items())
        return f"website sync: {result.get('status')} {result.get('url', '')}".strip()
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
    from ..profile_store import ProfileStore, render_summary

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
    from ..llm import LLM
    from ..search import format_results, web_search_answer

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
    from ..notify import ReminderStore, parse_when

    due = parse_when(p.get("when", ""))
    if due is None:
        return (f"couldn't parse when={p.get('when')!r} — use '+30m', '+2h', "
                "'HH:MM', or 'YYYY-MM-DD HH:MM'")
    reminder = ReminderStore(settings.data_dir).add(str(p.get("message", "")), due)
    return (f"reminder {reminder['id']} set for {reminder['due_at']} — "
            "I'll ping you on WeChat")


def _list_reminders(settings: Settings, p: dict) -> str:
    """List pending reminders (id, due time, message) — one per line."""
    from ..notify import ReminderStore

    pending = ReminderStore(settings.data_dir).pending()
    return "\n".join(f"[{r['id']}] {r['due_at']} — {r['message']}"
                     for r in pending) or "(no pending reminders)"


def _cancel_reminder(settings: Settings, p: dict) -> str:
    """Cancel the pending reminder with `id`; reports whether one was found."""
    from ..notify import ReminderStore

    reminder_id = str(p.get("id", ""))
    ok = ReminderStore(settings.data_dir).cancel(reminder_id)
    return (f"reminder {reminder_id} cancelled" if ok
            else f"no pending reminder {reminder_id!r}")


# ── routines ─────────────────────────────────────────────────────────

def _create_routine(settings: Settings, p: dict) -> str:
    """Create a recurring routine: `task` at `time` on `days`, optionally gated
    on a free-text `condition` checked at fire time. Returns the routine id +
    schedule, or a format hint when time/days are invalid."""
    from ..routines import RoutineStore

    routine = RoutineStore(settings.data_dir).add(
        task=str(p.get("task", "")), time=str(p.get("time", "")),
        days=str(p.get("days") or "daily"), condition=str(p.get("condition") or ""))
    if routine is None:
        return ("couldn't create routine — time must be HH:MM and days one of "
                "daily/workdays/weekends or e.g. 'mon,wed,fri'")
    when = f"{routine['days']} at {routine['time']}"
    gate = f", only when: {routine['condition']}" if routine["condition"] else ""
    return f"routine {routine['id']} created — {when}{gate}: {routine['task']}"


def _list_routines(settings: Settings, p: dict) -> str:
    """List active routines (id, schedule, optional condition, task)."""
    from ..routines import RoutineStore

    lines = [f"[{r['id']}] {r['days']} {r['time']}"
             + (f" (if: {r['condition']})" if r.get("condition") else "")
             + f" — {r['task']}"
             for r in RoutineStore(settings.data_dir).active()]
    return "\n".join(lines) or "(no routines)"


def _cancel_routine(settings: Settings, p: dict) -> str:
    """Cancel the active routine with `id`; reports whether one was found."""
    from ..routines import RoutineStore

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
    from ..llm import LLM
    from ..profile_store import ProfileStore, render_summary
    from ..search import format_results, web_search

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
