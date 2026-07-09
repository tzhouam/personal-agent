"""Interactive chat agent: one owner message in → one reply out.

Same safety philosophy as the daily pipeline: the LLM's write surface is the
typed actions in the registry (``assistant.actions``), executed by code;
everything else is read-only context. Channels authenticate the sender, so
only the owner ever reaches this. Action outcomes are appended to the reply
from what the code actually did, not from what the LLM claims it did.
"""

import json
import logging
from datetime import date

from ..actions import execute, prompt_block
from ..config import Settings
from ..llm import LLM
from ..profile_store import ProfileStore, render_summary
from ..state import load_state
from ..todo_store import ReadingList, TodoStore

log = logging.getLogger("assistant")

_SYSTEM = f"""You are your owner's personal assistant, reachable by chat/email. Answer from the
context below (profile, open todos, reading list, last run). Be concise and direct — this is a
chat reply, not a report. Answer in the language the owner wrote in.

You may execute actions, but ONLY when the owner explicitly asks for them:
{prompt_block()}

When the owner asks for something novel and multi-step that no other action covers (book a
meeting, find a restaurant, arrange or research something), do NOT refuse — emit plan_task
with the request; the planner breaks it down and tracks it. When the owner asks to run,
refresh, or update part of the daily routine, emit run_phase with the closest phase. When a
question needs current or external information you don't have, emit web_search instead of
guessing or refusing.

Respond with ONLY JSON: {{"reply": "<chat reply>", "actions": []}}
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


def handle_message(text: str, settings: Settings, llm: LLM | None = None,
                   history: list[dict] | None = None) -> str:
    """``history`` is optional prior exchanges for this session
    (``[{"owner": ..., "assistant": ...}, …]``, oldest first) — supplied by
    the serve daemon's session store so multi-turn references work."""
    llm = llm or LLM(settings)
    prompt = f"## Context\n{build_context(settings)}\n\n"
    if history:
        turns = "\n".join(f"Owner: {h.get('owner', '')}\nYou: {h.get('assistant', '')}"
                          for h in history[-10:])
        prompt += f"## Recent conversation (oldest first)\n{turns}\n\n"
    prompt += f"## Owner message\n{text.strip()[:4000]}"
    try:
        result = llm.complete_json(prompt, system=_SYSTEM, max_tokens=2000)
    except Exception as exc:
        log.exception("chat LLM call failed")
        return f"(assistant error: {exc})"
    if not isinstance(result, dict):
        return "(assistant error: unparseable model response)"
    reply = str(result.get("reply", "")).strip() or "(empty reply)"
    outcomes = execute(result.get("actions") or [], settings)
    if outcomes:
        reply += "\n\n✔ " + "\n✔ ".join(outcomes)
    return reply
