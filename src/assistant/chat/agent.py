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

from ..actions import execute, prompt_block, run_action
from ..config import Settings
from ..llm import LLM
from ..profile_store import ProfileStore, render_summary
from ..state import load_state
from ..todo_store import ReadingList, TodoStore

log = logging.getLogger("assistant")

_SYSTEM = f"""You are your owner's personal assistant, reachable by chat/email. Answer from the
context below (profile, open todos, reading list, active routines, pending reminders, last run).
Be concise and direct — this is a chat reply, not a report. Answer in the language the owner
wrote in. When an "## Attached images" section appears, the owner attached image(s) to this
message; the descriptions come from a vision model — respond to what the images show as if you
saw them, and be upfront when a description says an image could not be analyzed.

You may execute actions, but ONLY when the owner explicitly asks for them:
{prompt_block()}

When the owner asks for something novel and multi-step that no other action covers (book a
meeting, find a restaurant, arrange or research something), do NOT refuse — emit plan_task
with the request; the planner breaks it down and tracks it. When the owner asks to run,
refresh, or update part of the daily routine, emit run_phase with the closest phase. When a
question needs current or external information you don't have, emit web_search instead of
guessing or refusing. When the owner wants to be reminded or notified at/after some time,
emit set_reminder — the agent messages WeChat by itself at that time. When the owner wants
something RECURRING ("every workday…", "each morning…", possibly gated on a real-world
condition like a weather alert), emit create_routine, not set_reminder.

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

    # scheduled work the agent itself manages — without these it answers
    # about routines/reminders from todos alone and denies they exist
    for title, action in (("Active routines", "list_routines"),
                          ("Pending reminders", "list_reminders")):
        try:
            parts.append(f"## {title}\n" + run_action(action, {}, settings))
        except Exception:  # context is best-effort; a bad store must not kill chat
            log.exception("context: %s failed", action)

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
                   history: list[dict] | None = None,
                   image_paths: list[str] | None = None) -> str:
    """``history`` is optional prior exchanges for this session
    (``[{"owner": ..., "assistant": ...}, …]``, oldest first) — supplied by
    the serve daemon's session store so multi-turn references work.
    ``image_paths`` are local image files attached to this message; they are
    described by the vision chain (vision.py) and injected as context, so an
    image-only message (empty ``text``) still gets a real reply."""
    llm = llm or LLM(settings)
    prompt = f"## Context\n{build_context(settings)}\n\n"
    if image_paths:
        from ..vision import describe_images, render_image_context

        descriptions = describe_images(
            settings, image_paths[:settings.vision_max_images])
        prompt += render_image_context(descriptions) + "\n\n"
        text = text.strip() or "(the owner sent the attached image(s) without text — react to what they show)"
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
