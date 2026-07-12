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
message — either attached directly (look at them) or as descriptions from a vision model.
Respond to what the images show, and be upfront when an image could not be analyzed.

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

Finance: when the owner mentions money spent/earned ("午饭花了45", "发工资了", or a payment
receipt/bill screenshot), emit log_transaction with the amount, kind, and a sensible category.
ALWAYS extract the transaction time when it is visible anywhere — receipts show a payment
timestamp (支付时间), and phrases like "下午3点打车" mean time "15:00" — and pass it as
time: "HH:MM" (plus date "YYYY-MM-DD" when it wasn't today); every record keeps a full
date+time identity, and stated times are what distinguish two same-priced purchases. Exact
duplicates are rejected automatically, so log what you see. When asked how healthy
their income/spending is, analyze from the "## Finance ledger" numbers: cite the actual totals,
savings rate, and top categories, compare with the previous month, and give concrete,
prioritized suggestions. Never invent amounts that aren't in the ledger.

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

    try:  # finance: this month's computed totals + latest records, so money
        # questions are answered from real ledger numbers, never invented
        from ..finance_store import FinanceStore, timestamp_of
        from ..finance_store import render_summary as render_finance

        store = FinanceStore(settings.profile_dir)
        if store.records():
            recent = "\n".join(
                f"[{r['id']}] {timestamp_of(r)} {r['type']} {r['amount']} "
                f"{r['currency']} · {r['category']}"
                + (f" · {r['note']}" if r.get("note") else "")
                for r in store.records()[-8:])
            parts.append("## Finance ledger (computed — cite these numbers)\n"
                         + render_finance(store.summary(),
                                          currency=settings.finance_currency)
                         + "\nrecent records:\n" + recent)
    except Exception:
        log.exception("context: finance failed")

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
    attach: list[str] = []
    if image_paths:
        from ..vision import describe_images, media_type_for, render_image_context

        image_paths = image_paths[:settings.vision_max_images]
        if settings.llm_supports_images:
            # natively multimodal main LLM: attach the images to the call
            # itself — no separate vision pass
            attach = [p for p in image_paths if media_type_for(p)]
            prompt += ("## Attached images\n(the owner's images are attached "
                       "to this message — look at them directly)\n\n")
        else:  # text-only main LLM: describe-then-reason via the vision chain
            descriptions = describe_images(settings, image_paths)
            prompt += render_image_context(descriptions) + "\n\n"
        text = text.strip() or "(the owner sent the attached image(s) without text — react to what they show)"
    if history:
        turns = "\n".join(f"Owner: {h.get('owner', '')}\nYou: {h.get('assistant', '')}"
                          for h in history[-10:])
        prompt += f"## Recent conversation (oldest first)\n{turns}\n\n"
    prompt += f"## Owner message\n{text.strip()[:4000]}"
    try:
        result = llm.complete_json(prompt, system=_SYSTEM, max_tokens=2000,
                                   **({"images": attach} if attach else {}))
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
