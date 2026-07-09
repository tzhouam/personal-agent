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
