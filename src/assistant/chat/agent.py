"""Interactive chat agent: one owner message in → one reply out.

Same safety philosophy as the daily pipeline: the LLM's write surface is a
small set of typed actions executed by code, everything else is read-only
context. Channels authenticate the sender, so only the owner ever reaches
this. Action outcomes are appended to the reply from what the code actually
did, not from what the LLM claims it did.
"""

import json
import logging
import subprocess
import sys
from datetime import date

from ..config import Settings
from ..llm import LLM
from ..profile_store import ProfileStore, render_summary
from ..state import load_state
from ..todo_store import ReadingList, TodoStore

log = logging.getLogger("assistant")

_MAX_ACTIONS = 5

_SYSTEM = """You are your owner's personal assistant, reachable by chat/email. Answer from the
context below (profile, open todos, reading list, last run). Be concise and direct — this is a
chat reply, not a report. Answer in the language the owner wrote in.

You may execute actions, but ONLY when the owner explicitly asks for them:
  {"type": "add_todo", "title": "<short imperative>", "due": "YYYY-MM-DD"}   # due optional
  {"type": "done_todo", "id": "t3"}
  {"type": "done_reading", "id": "r2"}
  {"type": "trigger_run"}    # start a full daily digest run in the background

Respond with ONLY JSON: {"reply": "<chat reply>", "actions": []}
Never claim an action succeeded in the reply — outcomes are appended automatically."""


def build_context(settings: Settings) -> str:
    """Read-only snapshot the agent answers from."""
    parts = [f"Today is {date.today().isoformat()}."]
    profile_store = ProfileStore(settings.profile_dir)
    if profile_store.exists():
        parts.append("## Owner profile\n" + render_summary(profile_store.load()))

    todos = TodoStore(settings.profile_dir).open_items()
    parts.append("## Open todos\n" + ("\n".join(
        f"[{t['id']}] {t['title']}" + (f" (due {t['due']})" if t.get("due") else "")
        + (f" — {t.get('detail', '')[:160]}" if t.get("detail") else "")
        for t in todos
    ) or "(none)"))

    reading = ReadingList(settings.profile_dir).open_items()
    parts.append("## Reading list\n" + ("\n".join(
        f"[{r['id']}] {r['title']}" for r in reading[:15]) or "(empty)"))

    state = load_state(settings.state_file) or {}
    if state.get("run_id"):
        parts.append(f"## Last run\n{state['run_id']} — phase: {state.get('phase', '?')}"
                     + (" (incomplete)" if state.get("phase") not in (None, "done") else ""))
        digest_file = settings.runs_dir / state["run_id"] / "digest.json"
        if digest_file.exists():
            try:
                digest = json.loads(digest_file.read_text())
                red = digest.get("sections", {}).get("red", [])
                parts.append("Red notifications that run:\n" + ("\n".join(
                    f"- {i.get('summary', i.get('title', ''))}" for i in red[:10]) or "(none)"))
            except Exception:  # a corrupt artifact must not kill the chat
                pass
    return "\n\n".join(parts)


def _execute(actions: list, settings: Settings) -> list[str]:
    """Apply the typed actions; return what actually happened, one line each."""
    todos = TodoStore(settings.profile_dir)
    reading = ReadingList(settings.profile_dir)
    results = []
    for action in actions[:_MAX_ACTIONS]:
        if not isinstance(action, dict):
            continue
        kind = action.get("type")
        try:
            if kind == "add_todo" and action.get("title"):
                extra = {"due": action["due"]} if action.get("due") else {}
                item = todos.upsert(f"chat:{action['title']}", title=action["title"],
                                    source="chat", priority="yellow", **extra)
                results.append(f"added todo {item['id']}: {item['title']}" if item
                               else "todo already tracked")
            elif kind == "done_todo":
                ok = todos.mark_done(str(action.get("id", "")))
                results.append(f"todo {action.get('id')} marked done" if ok
                               else f"no open todo {action.get('id')!r}")
            elif kind == "done_reading":
                ok = reading.mark_done(str(action.get("id", "")))
                results.append(f"reading item {action.get('id')} marked read" if ok
                               else f"no unread item {action.get('id')!r}")
            elif kind == "trigger_run":
                state = load_state(settings.state_file) or {}
                if state.get("phase") not in (None, "done"):
                    results.append(f"a run is already in progress ({state.get('run_id')})")
                else:
                    log_file = (settings.data_dir / "chat_run.log").open("a")
                    subprocess.Popen([sys.executable, "-m", "assistant.cli", "run"],
                                     stdout=log_file, stderr=subprocess.STDOUT,
                                     start_new_session=True)
                    results.append("daily run started in the background")
            elif kind:
                results.append(f"unknown action {kind!r} ignored")
        except Exception as exc:  # one bad action must not eat the reply
            log.exception("chat action %s failed", kind)
            results.append(f"action {kind} failed: {exc}")
    return results


def handle_message(text: str, settings: Settings, llm: LLM | None = None) -> str:
    llm = llm or LLM(settings)
    try:
        result = llm.complete_json(
            f"## Context\n{build_context(settings)}\n\n## Owner message\n{text.strip()[:4000]}",
            system=_SYSTEM, max_tokens=2000,
        )
    except Exception as exc:
        log.exception("chat LLM call failed")
        return f"(assistant error: {exc})"
    if not isinstance(result, dict):
        return "(assistant error: unparseable model response)"
    reply = str(result.get("reply", "")).strip() or "(empty reply)"
    outcomes = _execute(result.get("actions") or [], settings)
    if outcomes:
        reply += "\n\n✔ " + "\n✔ ".join(outcomes)
    return reply
