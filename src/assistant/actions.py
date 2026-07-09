"""Typed action registry — the single source of truth for what the agent can DO.

One table drives three surfaces that used to be maintained by hand in
parallel: the chat system prompt (which actions the LLM may emit), the
executor that applies them, and the CLI/HTTP entry points. Handlers return
one human-readable line describing what the code actually did — replies are
built from these outcomes, never from LLM claims.
"""

import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable

from .config import Settings
from .state import load_state
from .todo_store import ReadingList, TodoStore


@dataclass(frozen=True)
class Action:
    name: str
    description: str
    handler: Callable[[Settings, dict], str]
    # param name -> {"required": bool, "desc": str}; values are strings
    params: dict = field(default_factory=dict)
    llm: bool = False            # exposed to the chat LLM as an emittable action
    prompt_example: str = ""     # exact line shown in the chat system prompt
    slash: str | None = None     # OpenClaw slash-command family ("todo", …)


def validate(action: Action, params: dict) -> str | None:
    """Return an error line, or None when params satisfy the action's spec."""
    for name, spec in action.params.items():
        if spec.get("required") and not str(params.get(name, "")).strip():
            return f"action {action.name}: missing required {name!r}"
    return None


# ── handlers ─────────────────────────────────────────────────────────

def _add_todo(settings: Settings, p: dict) -> str:
    source = p.get("source", "chat")
    extra = {"due": p["due"]} if p.get("due") else {}
    item = TodoStore(settings.profile_dir).upsert(
        f"{source}:{p['title']}", title=p["title"], source=source,
        priority="yellow", **extra)
    return (f"added todo {item['id']}: {item['title']}" if item
            else "todo already tracked")


def _done_todo(settings: Settings, p: dict) -> str:
    item_id = str(p.get("id", ""))
    ok = TodoStore(settings.profile_dir).mark_done(item_id)
    return f"todo {item_id} marked done" if ok else f"no open todo {item_id!r}"


def _list_todos(settings: Settings, p: dict) -> str:
    lines = []
    for t in TodoStore(settings.profile_dir).open_items():
        due = f" due:{t['due']}" if t.get("due") else ""
        lines.append(f"[{t['id']}] {t['title']} ({t.get('source', '')}, "
                     f"since {t.get('created', '')}{due})")
    return "\n".join(lines) or "(no open todos)"


def _done_reading(settings: Settings, p: dict) -> str:
    item_id = str(p.get("id", ""))
    ok = ReadingList(settings.profile_dir).mark_done(item_id)
    return (f"reading item {item_id} marked read" if ok
            else f"no unread item {item_id!r}")


def _list_reading(settings: Settings, p: dict) -> str:
    lines = [f"[{r['id']}] {r['title']}  {r.get('url', '')}".rstrip()
             for r in ReadingList(settings.profile_dir).open_items()]
    return "\n".join(lines) or "(reading list empty)"


def _trigger_run(settings: Settings, p: dict) -> str:
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


def _show_profile(settings: Settings, p: dict) -> str:
    from .profile_store import ProfileStore, render_summary

    store = ProfileStore(settings.profile_dir)
    if not store.exists():
        return "(no profile yet — run `assistant bootstrap`)"
    return render_summary(store.load(), max_items=12)


# phases the owner may run standalone; the rest need the full pipeline's
# upstream state (collect→profile→digest), so they map to trigger_run
RUNNABLE_PHASES = ("research", "website", "todos", "resume", "curate", "consolidate")


def _run_phase(settings: Settings, p: dict) -> str:
    phase = str(p.get("phase", "")).strip().lower()
    if phase in ("run", "all", "daily", "digest", "collect", "profile", "deliver"):
        return _trigger_run(settings, {}) + " (that phase needs the full pipeline)"
    if phase not in RUNNABLE_PHASES:
        return (f"unknown phase {phase!r} — runnable: {', '.join(RUNNABLE_PHASES)}, "
                "or 'all' for the full daily run")
    if phase == "website":  # fast — run it inline and report the real result
        from .profile_store import ProfileStore
        from .todo_store import ReadingList, TodoStore
        from .urgency import urgency
        from .website import sync_website

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


_SEARCH_ANSWER_SYSTEM = """You answer the owner's question from the web search results below.
Be concise and concrete; cite the source URL in parentheses after each claim you take from a
result. If the results don't answer the question, say so and suggest a better query.
Answer in the language the owner used. Plain text, no markdown headings."""


def _web_search(settings: Settings, p: dict) -> str:
    from .llm import LLM
    from .search import format_results, web_search_answer

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


def _set_reminder(settings: Settings, p: dict) -> str:
    from .notify import ReminderStore, parse_when

    due = parse_when(p.get("when", ""))
    if due is None:
        return (f"couldn't parse when={p.get('when')!r} — use '+30m', '+2h', "
                "'HH:MM', or 'YYYY-MM-DD HH:MM'")
    reminder = ReminderStore(settings.data_dir).add(str(p.get("message", "")), due)
    return (f"reminder {reminder['id']} set for {reminder['due_at']} — "
            "I'll ping you on WeChat")


def _list_reminders(settings: Settings, p: dict) -> str:
    from .notify import ReminderStore

    pending = ReminderStore(settings.data_dir).pending()
    return "\n".join(f"[{r['id']}] {r['due_at']} — {r['message']}"
                     for r in pending) or "(no pending reminders)"


def _cancel_reminder(settings: Settings, p: dict) -> str:
    from .notify import ReminderStore

    reminder_id = str(p.get("id", ""))
    ok = ReminderStore(settings.data_dir).cancel(reminder_id)
    return (f"reminder {reminder_id} cancelled" if ok
            else f"no pending reminder {reminder_id!r}")


def _create_routine(settings: Settings, p: dict) -> str:
    from .routines import RoutineStore

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
    from .routines import RoutineStore

    lines = [f"[{r['id']}] {r['days']} {r['time']}"
             + (f" (if: {r['condition']})" if r.get("condition") else "")
             + f" — {r['task']}"
             for r in RoutineStore(settings.data_dir).active()]
    return "\n".join(lines) or "(no routines)"


def _cancel_routine(settings: Settings, p: dict) -> str:
    from .routines import RoutineStore

    routine_id = str(p.get("id", ""))
    ok = RoutineStore(settings.data_dir).cancel(routine_id)
    return (f"routine {routine_id} cancelled" if ok
            else f"no active routine {routine_id!r}")


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
    from .llm import LLM
    from .profile_store import ProfileStore, render_summary
    from .search import format_results, web_search

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


# ── the registry ─────────────────────────────────────────────────────

ACTIONS: dict[str, Action] = {a.name: a for a in [
    Action(
        name="add_todo",
        description="add an open todo",
        handler=_add_todo,
        params={"title": {"required": True, "desc": "short imperative title"},
                "due": {"required": False, "desc": "YYYY-MM-DD"},
                "source": {"required": False, "desc": "origin tag, default chat"}},
        llm=True,
        prompt_example='{"type": "add_todo", "title": "<short imperative>", '
                       '"due": "YYYY-MM-DD"}   # due optional',
        slash="todo",
    ),
    Action(
        name="done_todo",
        description="close an open todo by id",
        handler=_done_todo,
        params={"id": {"required": True, "desc": "todo id, e.g. t3"}},
        llm=True,
        prompt_example='{"type": "done_todo", "id": "t3"}',
        slash="todo",
    ),
    Action(
        name="list_todos",
        description="list open todos",
        handler=_list_todos,
        slash="todo",
    ),
    Action(
        name="done_reading",
        description="mark a reading-list item read by id",
        handler=_done_reading,
        params={"id": {"required": True, "desc": "reading id, e.g. r2"}},
        llm=True,
        prompt_example='{"type": "done_reading", "id": "r2"}',
        slash="read",
    ),
    Action(
        name="list_reading",
        description="list unread reading-list items",
        handler=_list_reading,
        slash="read",
    ),
    Action(
        name="trigger_run",
        description="start a full daily digest run in the background",
        handler=_trigger_run,
        params={"resume": {"required": False, "desc": "truthy = resume last run"}},
        llm=True,
        prompt_example='{"type": "trigger_run"}    # start a full daily digest '
                       'run in the background',
        slash="digest",
    ),
    Action(
        name="run_status",
        description="last run id/phase and open counts",
        handler=_run_status,
        slash="status",
    ),
    Action(
        name="run_phase",
        description="run one standalone pipeline phase now",
        handler=_run_phase,
        params={"phase": {"required": True,
                          "desc": "research|website|todos|resume|curate|consolidate|all"}},
        llm=True,
        prompt_example='{"type": "run_phase", "phase": "research"}   # research|website|todos'
                       '|resume|curate|consolidate, or "all" for the full daily run',
        slash="run",
    ),
    Action(
        name="web_search",
        description="search the internet and answer from the results",
        handler=_web_search,
        params={"query": {"required": True, "desc": "the search query"}},
        llm=True,
        prompt_example='{"type": "web_search", "query": "<what to look up>"}   # for '
                       'questions needing current/external information',
        slash="search",
    ),
    Action(
        name="set_reminder",
        description="schedule a one-shot WeChat reminder the agent sends by itself",
        handler=_set_reminder,
        params={"message": {"required": True, "desc": "what to remind about"},
                "when": {"required": True,
                         "desc": "'+30m' / '+2h' / '+1d', 'HH:MM', or 'YYYY-MM-DD HH:MM'"}},
        llm=True,
        prompt_example='{"type": "set_reminder", "message": "...", "when": "+2h"}   # '
                       'agent pings WeChat at the time, unprompted',
        slash="remind",
    ),
    Action(
        name="list_reminders",
        description="list pending reminders (cancel: set_reminder is one-shot)",
        handler=_list_reminders,
        slash="remind",
    ),
    Action(
        name="cancel_reminder",
        description="cancel a pending reminder by id",
        handler=_cancel_reminder,
        params={"id": {"required": True, "desc": "reminder id, e.g. m2"}},
        llm=True,
        prompt_example='{"type": "cancel_reminder", "id": "m2"}',
        slash="remind",
    ),
    Action(
        name="create_routine",
        description="recurring work: at a time on chosen days, optionally gated on a "
                    "real-world condition, the agent runs a task and messages WeChat",
        handler=_create_routine,
        params={"task": {"required": True, "desc": "what to do/say each time"},
                "time": {"required": True, "desc": "HH:MM"},
                "days": {"required": False,
                         "desc": "daily|workdays|weekends|'mon,wed,fri' (default daily)"},
                "condition": {"required": False,
                              "desc": "free-text gate checked at fire time via web "
                                      "search, e.g. 'there is a weather alert in Shenzhen'"}},
        llm=True,
        prompt_example='{"type": "create_routine", "task": "...", "time": "08:30", '
                       '"days": "workdays", "condition": "<optional real-world gate>"}',
        slash="routine",
    ),
    Action(
        name="list_routines",
        description="list active routines",
        handler=_list_routines,
        slash="routine",
    ),
    Action(
        name="cancel_routine",
        description="cancel a routine by id",
        handler=_cancel_routine,
        params={"id": {"required": True, "desc": "routine id, e.g. rt2"}},
        llm=True,
        prompt_example='{"type": "cancel_routine", "id": "rt2"}',
        slash="routine",
    ),
    Action(
        name="plan_task",
        description="plan a novel multi-step task (booking, arranging, researching) "
                    "and track it as a todo",
        handler=_plan_task,
        params={"request": {"required": True, "desc": "the owner's task, one sentence"}},
        llm=True,
        prompt_example='{"type": "plan_task", "request": "<the task in one sentence>"}'
                       '   # for novel multi-step asks: bookings, arranging, research',
        slash="plan",
    ),
    Action(
        name="show_profile",
        description="summary of the owner profile",
        handler=_show_profile,
    ),
]}


def prompt_block() -> str:
    """The chat system prompt's action list, generated from the registry."""
    return "\n".join(f"  {a.prompt_example}"
                     for a in ACTIONS.values() if a.llm)


def execute(actions: list, settings: Settings, max_actions: int = 5) -> list[str]:
    """Apply LLM-emitted typed actions; return what actually happened, one
    line each. Only registry entries marked ``llm`` are honored here."""
    import logging

    log = logging.getLogger("assistant")
    results = []
    for raw in (actions or [])[:max_actions]:
        if not isinstance(raw, dict):
            continue
        kind = raw.get("type")
        if not kind:
            continue
        action = ACTIONS.get(kind)
        if action is None or not action.llm:
            results.append(f"unknown action {kind!r} ignored")
            continue
        error = validate(action, raw)
        if error:
            results.append(error)
            continue
        try:
            results.append(action.handler(settings, raw))
        except Exception as exc:  # one bad action must not eat the reply
            log.exception("chat action %s failed", kind)
            results.append(f"action {kind} failed: {exc}")
    return results


def run_action(name: str, params: dict, settings: Settings) -> str:
    """Direct invocation (CLI / HTTP / slash commands) — any registry entry."""
    action = ACTIONS.get(name)
    if action is None:
        raise KeyError(name)
    error = validate(action, params)
    if error:
        raise ValueError(error)
    return action.handler(settings, params)
