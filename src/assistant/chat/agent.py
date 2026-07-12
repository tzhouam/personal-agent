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

from ..actions import execute, looks_failed, prompt_block, run_action
from ..config import Settings
from ..llm import LLM
from ..profile_store import ProfileStore, render_summary
from ..state import load_state
from ..todo_store import ReadingList, TodoStore

log = logging.getLogger("assistant")

_SYSTEM = f"""You are your owner's personal assistant, reachable by chat/email. Answer from the
context below (profile, open todos, reading list, active routines, pending reminders, finance
ledger, health, last run).
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

Health: when the owner mentions eating, exercising, or a body measurement — or sends a photo of
a meal, a nutrition label, or a body scale — emit the matching log action (log_meal /
log_exercise / log_weight; set_health_profile for height/sex/birth year). For food images,
estimate calories and protein/carbs/fat from what you see (or read them off the label verbatim),
put ingredient lists in the note, say in the reply that macros are estimates, and check the
ingredients against the "wants covered" needs list. When asked about health status or
improvements, analyze from the "## Health" computed numbers — BMI, weight trend, exercise
minutes, daily calorie/protein averages, open needs — with concrete, practical suggestions.
You give wellness guidance, not medical diagnosis; for medical concerns recommend a doctor.

The context sections all describe the SAME person — link them in every analysis instead of
treating them separately. Examples: health advice should use the owner profile (their work
style and projects imply desk time), existing exercise routines, and the finance ledger's food
pattern (frequent eating-out shows there even when meals go unlogged); finance advice should
use health data (food/health spending vs meals and nutrient needs) and the profile (age and
career stage shape savings advice); and the "## Cross-links" section gives you computed joins
(meal↔expense pairs, spend-vs-logged gaps) to cite directly.

Present analyses so they scan in seconds: a one-line headline first (totals/net), then short
labeled sections with an emoji each, percentages next to amounts, and one blank line between
sections. For every dominant cost area, drill into its sub-areas using the computed
"<category> detail / top / by time" lines — name the top merchants, the average and largest
transaction, and the time-of-day pattern — then give ONE concrete suggestion per section.
Numbers come from the computed blocks, never estimated.

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
                                          currency=settings.finance_currency,
                                          store=store)
                         + "\nrecent records:\n" + recent)
    except Exception:
        log.exception("context: finance failed")

    try:  # health: computed body facts + 7-day picture, so wellness questions
        # are answered from real logged numbers
        from ..health_store import HealthStore
        from ..health_store import render_summary as render_health

        store = HealthStore(settings.profile_dir)
        if store.records() or store.load()["profile"] or store.open_needs():
            parts.append("## Health (computed — cite these numbers)\n"
                         + render_health(store.summary()))
    except Exception:
        log.exception("context: health failed")

    try:  # cross-links: deterministic joins between the sub-stores (meal↔
        # expense pairs, food spend vs meals, health spend vs needs)
        from ..insights import build_crosslinks

        links = build_crosslinks(settings)
        if links:
            parts.append("## Cross-links (computed)\n" + links)
    except Exception:
        log.exception("context: crosslinks failed")

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
    actions = result.get("actions") or []
    outcomes = execute(actions, settings)
    all_outcomes = list(outcomes)

    # Review-and-retry: when an action outcome reports a failure (bad params,
    # wrong id, unknown action), show the model exactly what it emitted and
    # what came back so it can analyze, correct, and re-execute — up to 2
    # repair rounds. Duplicate rejections never retry (dedup working as
    # intended). Only the latest round's failures drive another round.
    for _ in range(2):
        if not any(looks_failed(o) for o in outcomes):
            break
        review = (f"{prompt}\n\n## Actions you just emitted\n"
                  f"{json.dumps(actions, ensure_ascii=False)}\n"
                  "## Their results (in order)\n"
                  + "\n".join(f"- {o}" for o in outcomes)
                  + "\n\n## Fix the failures\nSome actions FAILED. Analyze each "
                    "failure message, correct the parameters or pick the right "
                    "action/id, and respond with ONLY the corrected actions "
                    "(empty list if a failure cannot be fixed — e.g. the thing "
                    "genuinely doesn't exist). Do NOT re-emit actions that "
                    "succeeded or were rejected as duplicates. You may also "
                    "revise the reply.")
        try:
            fix = llm.complete_json(review, system=_SYSTEM, max_tokens=2000)
        except Exception:
            log.exception("action-review LLM call failed")
            break
        if isinstance(fix, dict) and str(fix.get("reply", "")).strip():
            reply = str(fix["reply"]).strip()  # revised even when unfixable
        actions = (fix.get("actions") or []) if isinstance(fix, dict) else []
        if not actions:
            break
        outcomes = execute(actions, settings)
        log.info("action review: retried %d action(s)", len(outcomes))
        all_outcomes += [f"(retry) {o}" for o in outcomes]

    if all_outcomes:
        reply += "\n\n✔ " + "\n✔ ".join(all_outcomes)
    return reply
