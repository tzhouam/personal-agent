"""Interactive chat agent: one owner message in → one reply out.

Same safety philosophy as the daily pipeline: the LLM's write surface is the
typed actions in the registry (``assistant.agent.actions``), executed by code;
everything else is read-only context. Channels authenticate the sender, so
only the owner ever reaches this. Action outcomes are appended to the reply
from what the code actually did, not from what the LLM claims it did.
"""

import json
import logging
import time
from dataclasses import dataclass
from datetime import date

from assistant.agent.actions import RETRIEVAL_ACTIONS, execute, looks_failed, prompt_block, run_action
from assistant.platform.config import Settings
from assistant.platform.llm import LLM
from assistant.agent.profile_store import ProfileStore, render_summary
from assistant.agent.state import load_state
from assistant.agent.todo_store import ReadingList, TodoStore

log = logging.getLogger("assistant")

_TODO_CONTEXT_LIMIT = 25


@dataclass
class TurnResult:
    """What one chat turn produced, for callers that persist sessions: the
    reply plus the provisional outcome label, friction flags, and the owner's
    verdict on the PREVIOUS turn (the satisfaction ground truth — "success"
    means the owner was satisfied, not merely that output was produced)."""

    reply: str
    outcome: str               # success | fail | neutral (provisional)
    repaired: bool = False     # an action failed and was fixed on retry
    self_reported: bool = False   # label came from the model's self_check
    prev_verdict: str | None = None   # satisfied | dissatisfied | None


# Owner-side correction markers → deterministic "dissatisfied" verdict about
# the previous reply. scripts/self_improve_evidence.py keeps a standalone copy
# (it must run without the package importable) — update both together.
CORRECTION_MARKERS = ("不对", "不是", "错", "改成", "别再", "以后", "应该", "重新", "取消",
                      "wrong", "not what", "incorrect", "undo that", "redo")


def classify_turn(all_outcomes: list, last_outcomes: list, retrieved: bool = False,
                  hard_fail: bool = False, self_check=None) -> tuple[str, bool, bool]:
    """Stage-1 provisional label, computed in code — fail > success >
    self-report > neutral. Returns ``(label, repaired, self_reported)``.

    ``last_outcomes`` is the FINAL repair round: ``all_outcomes`` keeps the
    superseded failure lines after a successful "(retry)", so it must not
    drive the fail decision. ``self_check`` (the model's own verdict) is only
    consulted for action-less turns — code-observed outcomes always win."""
    repaired = any(str(o).startswith("(retry) ") for o in all_outcomes)
    if hard_fail or any(looks_failed(o) for o in last_outcomes):
        return "fail", repaired, False
    if all_outcomes or retrieved:
        return "success", repaired, False
    check = str(self_check).strip().lower() if self_check else ""
    if check in ("success", "fail"):
        return check, repaired, True
    return "neutral", repaired, False


def owner_verdict(owner_text: str, model_feedback=None) -> str | None:
    """Stage-2 verdict the owner's NEW message delivers about the previous
    reply — the satisfaction signal. Deterministic correction markers first
    (a lenient model can't miss them), else the model's prev_feedback."""
    low = str(owner_text or "").lower()
    if any(m in low for m in CORRECTION_MARKERS):
        return "dissatisfied"
    feedback = str(model_feedback).strip().lower() if model_feedback else ""
    return feedback if feedback in ("satisfied", "dissatisfied") else None

# Rendered per call by `system_prompt()` (NOT an import-time f-string): the
# «ACTIONS»/«REBOOT» placeholders depend on `settings.deployment_mode`, so the
# action list and the reboot guidance must be resolved when the mode is known —
# in multi_tenant `reboot` is admin-only (§10) and must not be advertised.
# Plain `.replace()` on unique tokens, not `.format()`: the prompt body contains
# literal JSON braces.
_SYSTEM_TEMPLATE = """You are your owner's personal assistant, reachable by chat/email. Answer from the
context below (profile, open todos, reading list, active routines, pending reminders, finance
ledger, health, last run).
Be concise and direct — this is a chat reply, not a report. Answer in the language the owner
wrote in. When an "## Attached images" section appears, the owner attached image(s) to this
message — either attached directly (look at them) or as descriptions from a vision model.
Respond to what the images show, and be upfront when an image could not be analyzed.

You may execute actions, but ONLY when the owner explicitly asks for them:
«ACTIONS»

When the owner asks for something novel and multi-step that no other action covers, do NOT
refuse: if the agent can complete it alone (research and summarize, find/compare options,
gather information then set reminders), emit execute_task — it runs the task step by step in
the background and reports to WeChat; if it needs the owner's own participation (attending,
signing, in-person errands), emit plan_task so it's broken down and tracked instead. When the owner asks to run,
refresh, or update part of the daily routine, emit run_phase with the closest phase. «REBOOT» When a
question needs current or external information you don't have, emit web_search instead of
guessing or refusing. When the owner wants to be reminded or notified at/after some time,
emit set_reminder — the agent messages WeChat by itself at that time. When the owner wants
something RECURRING ("every workday…", "each morning…", possibly gated on a real-world
condition like a weather alert), emit create_routine, not set_reminder.

Finance: when the owner mentions money spent/earned ("午饭花了45", "发工资了", or a payment
receipt/bill screenshot), emit log_transaction with the amount, kind, and a sensible category.
ALWAYS extract the transaction time when it is visible anywhere — receipts show a payment
timestamp (支付时间), and phrases like "下午3点打车" mean time "15:00" — and pass it as
time: "HH:MM". ALWAYS set the event day too: if the owner names a past day (今天/昨天/前天/
大前天/N天前/上周X, or a receipt's date), use the [temporal anchor] at the end of this prompt
to compute the absolute date and pass date: "YYYY-MM-DD" — only omit date when the day is
genuinely unstated (then it defaults to today). Every record keeps a full date+time identity,
and stated times are what distinguish two same-priced purchases. Exact
duplicates are rejected automatically, so log what you see. When asked how healthy
their income/spending is, analyze from the "## Finance ledger" numbers: cite the actual totals,
savings rate, and top categories, compare with the previous month, and give concrete,
prioritized suggestions. Never invent amounts that aren't in the ledger. For a month, period,
category, or merchant NOT covered by the "## Finance ledger" block, emit query_transactions
(date / start+end / category / kind / contains) to retrieve those records before answering.

Health: when the owner mentions eating, exercising, or a body measurement — or sends a photo of
a meal, a nutrition label, or a body scale — emit the matching log action (log_meal /
log_exercise / log_weight; set_health_profile for height/sex/birth year). Set the event day the
same way as finance: if the owner refers to a past day ("昨天中午吃了…", "前天跑步", N天前), use
the [temporal anchor] to compute the absolute date and pass date: "YYYY-MM-DD" on the log action;
omit date only when the day is genuinely today. Pass time: "HH:MM" whenever a time is stated. For food images,
estimate calories and protein/carbs/fat from what you see (or read them off the label verbatim),
put ingredient lists in the note, say in the reply that macros are estimates, and check the
ingredients against the "wants covered" needs list. When asked about health status or
improvements, analyze from the "## Health" computed numbers — BMI, weight trend, exercise
minutes, daily calorie/protein averages, open needs — with concrete, practical suggestions.
For a specific day ("昨天吃了多少"), read the per-day totals and the "recent records" list in
that block — NEVER claim a meal wasn't logged without checking them first; a record you can
see there IS logged, even if it isn't in the recent chat. If the day or period you need is NOT
shown in that block (older than the recent window, a text/ingredient search, a range), emit
query_health (date / start+end / kind / contains) to retrieve it before answering — never
guess or say "没记录" without querying.
You give wellness guidance, not medical diagnosis; for medical concerns recommend a doctor.

The context sections all describe the SAME person — link them in every analysis instead of
treating them separately. Examples: health advice should use the owner profile (their work
style and projects imply desk time), existing exercise routines, and the finance ledger's food
pattern (frequent eating-out shows there even when meals go unlogged); finance advice should
use health data (food/health spending vs meals and nutrient needs) and the profile (age and
career stage shape savings advice); and the "## Cross-links" section gives you computed joins
(meal↔expense pairs, spend-vs-logged gaps) to cite directly.

Learned rules apply to ACTION PARAMETERS, not just words: if a rule sets a default (currency,
category, language, timing), every matching action you emit must carry that parameter — saying
you followed a rule while the action ignored it is the worst failure mode.

Self-evolution: when the owner gives DURABLE feedback about your behavior — "以后…",
"别再…", "记住要…", "你应该…", or corrects how you just acted — emit learn_preference with the
rule (and keep applying it immediately); "忘掉/取消那条规则" → retire_preference. Distinguish
from one-off reminders (set_reminder) and world facts. When the owner asks you to reflect on
recent conversations and improve, emit self_evolve.

Workflows: when the owner wants to SAVE a repeatable procedure ("存成工作流", "以后照这个
流程", "make this a workflow"), emit create_workflow — write the 1-6 concrete steps yourself
from the conversation; don't ask them to dictate steps. When they ask to run a saved one
("跑一下周报流程"), emit run_workflow with its id from the "## Saved workflows" list. For a
recurring schedule, emit create_routine with the workflow id bound. A workflow's outward
steps still pause for the owner's approval when it runs.

Present analyses so they scan in seconds: a one-line headline first (totals/net), then short
labeled sections with an emoji each, percentages next to amounts, and one blank line between
sections. For every dominant cost area, drill into its sub-areas using the computed
"<category> detail / top / by time" lines — name the top merchants, the average and largest
transaction, and the time-of-day pattern — then give ONE concrete suggestion per section.
Numbers come from the computed blocks, never estimated.

Respond with ONLY JSON: {"reply": "<chat reply>", "actions": [],
"self_check": "success|fail|neutral", "prev_feedback": "satisfied|dissatisfied|unclear"}
self_check is your honest verdict on THIS reply: "success" only when it fully answers the
owner from real context/data or completes what they asked; "fail" when you could not actually
help (missing info, unable, had to guess); "neutral" for greetings, chit-chat, acknowledgments.
prev_feedback reads the owner's NEW message as feedback on your PREVIOUS reply: "dissatisfied"
when they correct it, complain, or re-ask the same thing; "satisfied" when they accept, thank,
or build on it; otherwise "unclear".
Never claim an action succeeded in the reply — outcomes are appended automatically."""

# The «REBOOT» sentence, per mode. single_user keeps today's guidance verbatim;
# multi_tenant tells the model reboot is admin-only instead of advertising an
# action that dispatch would refuse (§10).
_REBOOT_HINT_SINGLE = ('When the\nowner asks to restart / reboot the assistant '
                       '("重启", "重新启动", "restart", "reboot"), emit\n'
                       'reboot — it reloads the agent and comes back in a few seconds.')
_REBOOT_HINT_MT = ('Restarting the assistant is admin-only in this deployment: when the owner '
                   'asks to\nrestart / reboot ("重启", "restart"), explain that an administrator '
                   'must run `assistant admin\nreboot` — do NOT emit any reboot action.')


def _render_system(settings: Settings) -> str:
    """Resolve the mode-dependent placeholders: the action list (admin actions
    omitted for tenants) and the reboot guidance."""
    mt = settings.deployment_mode == "multi_tenant"
    return (_SYSTEM_TEMPLATE
            .replace("«ACTIONS»", prompt_block(settings))
            .replace("«REBOOT»", _REBOOT_HINT_MT if mt else _REBOOT_HINT_SINGLE))


def system_prompt(settings: Settings) -> str:
    """The chat system prompt: the static core (rendered for this deployment
    mode) plus the learned behavior rules — shared cross-user rules first
    (multi_tenant, `G*`), then the user's personal rules (`L*`, which take
    precedence) — the agent's self-evolution surface. Rebuilt per turn so a
    lesson learned in one message governs the next."""
    core = _render_system(settings)
    try:
        from assistant.agent.lessons_store import combined_prompt_block

        return core + combined_prompt_block(settings)
    except Exception:  # lessons are an enhancement, never a blocker
        log.exception("lessons injection failed")
        return core


def build_context(settings: Settings) -> str:
    """Read-only snapshot the agent answers from."""
    parts = [f"Today is {date.today().isoformat()}."]
    profile_store = ProfileStore(settings.profile_dir)
    if profile_store.exists():
        parts.append("## Owner profile\n" + render_summary(profile_store.load()))

    # context budget: the full todo list once hit 18KB of a 23KB context —
    # show the top of the urgency ranking, summarize the rest
    from assistant.agent.urgency import urgency

    todos = sorted(TodoStore(settings.profile_dir).open_items(),
                   key=urgency, reverse=True)
    shown, rest = todos[:_TODO_CONTEXT_LIMIT], todos[_TODO_CONTEXT_LIMIT:]
    lines = [f"[{t['id']}] {t['title']}" + (f" (due {t['due']})" if t.get("due") else "")
             + (f" — {t.get('detail', '')[:120]}" if t.get("detail") else "")
             for t in shown]
    if rest:
        lines.append(f"…and {len(rest)} lower-urgency todos "
                     "(list_todos shows all; never claim these are everything)")
    parts.append("## Open todos (top by urgency)\n" + ("\n".join(lines) or "(none)"))

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

    try:  # saved workflows: the ids run_workflow needs (only when any exist)
        from assistant.agent.workflow_store import WorkflowStore

        workflows = WorkflowStore(settings.profile_dir).active()
        if workflows:
            parts.append("## Saved workflows (run_workflow executes one; "
                         "show_workflow shows full steps)\n" + "\n".join(
                             f"[{w['id']}] {w['name']} — {w['description'][:80]} "
                             f"({len(w['steps'])} steps"
                             + (f", ran {w['run_count']}×" if w.get("run_count") else "")
                             + ")"
                             for w in workflows))
    except Exception:
        log.exception("context: workflows failed")

    try:  # finance: this month's computed totals + latest records, so money
        # questions are answered from real ledger numbers, never invented
        from assistant.agent.finance_store import FinanceStore, timestamp_of
        from assistant.agent.finance_store import render_summary as render_finance

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
        from assistant.agent.health_store import HealthStore
        from assistant.agent.health_store import render_summary as render_health

        store = HealthStore(settings.profile_dir)
        if store.records() or store.load()["profile"] or store.open_needs():
            block = ["## Health (computed — cite these numbers)",
                     render_health(store.summary())]
            recent = store.records(days=3)[-16:]  # individual meals/exercise, so
            if recent:                            # per-day questions are answerable
                block.append("recent records (id · date time · what):")
                block += [
                    f"[{r['id']}] {r['date']} {r.get('time', '')} · "
                    + (r.get("description") or r.get("activity", ""))
                    + (f" · {r['calories_kcal']}kcal" if r.get("calories_kcal") else "")
                    + (f" · {r['protein_g']}g蛋白" if r.get("protein_g") else "")
                    + (f" · {r['duration_min']}min" if r.get("duration_min") else "")
                    for r in recent]
            parts.append("\n".join(block))
    except Exception:
        log.exception("context: health failed")

    try:  # cross-links: deterministic joins between the sub-stores (meal↔
        # expense pairs, food spend vs meals, health spend vs needs)
        from assistant.agent.insights import build_crosslinks

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


def handle_turn(text: str, settings: Settings, llm: LLM | None = None,
                history: list[dict] | None = None,
                image_paths: list[str] | None = None) -> TurnResult:
    """``history`` is optional prior exchanges for this session
    (``[{"owner": ..., "assistant": ...}, …]``, oldest first) — supplied by
    the serve daemon's session store so multi-turn references work.
    ``image_paths`` are local image files attached to this message; they are
    described by the vision chain (vision.py) and injected as context, so an
    image-only message (empty ``text``) still gets a real reply.

    Returns a `TurnResult` — the reply plus the turn's outcome label and the
    owner's verdict on the previous turn, which session-persisting callers
    (serve.py) store for the self-evolution passes."""
    llm = llm or LLM(settings)
    turn_start = time.monotonic()

    def _finish(reply: str, label: str, *, repaired: bool = False,
                self_reported: bool = False, actions_n: int = 0,
                repair_rounds: int = 0, failures_left: int = 0,
                model_feedback=None) -> TurnResult:
        """Every exit path funnels through here: derive the previous-turn
        verdict, record the per-turn metrics row (best-effort — labeling can
        never break the reply), and build the result. Hard-failure exits are
        measured too, which the old bare-string returns never were."""
        verdict = owner_verdict(text, model_feedback) if history else None
        try:
            from assistant.agent.events_store import EventsStore

            events = EventsStore(settings.events_db)
            events.record_metrics(f"chat-{date.today().isoformat()}", "chat_turn", {
                "duration_s": round(time.monotonic() - turn_start, 2),
                "prompt_chars": len(prompt), "actions": actions_n,
                "repair_rounds": repair_rounds, "failures_left": failures_left,
                "images": len(attach),
                "success": int(label == "success"), "fail": int(label == "fail"),
                "neutral": int(label == "neutral"), "repaired": int(repaired),
                "prev_satisfied": int(verdict == "satisfied"),
                "prev_dissatisfied": int(verdict == "dissatisfied")})
            events.close()
        except Exception:
            log.exception("chat metrics failed")
        return TurnResult(reply, label, repaired, self_reported, verdict)

    prompt = f"## Context\n{build_context(settings)}\n\n"
    attach: list[str] = []
    if image_paths:
        from assistant.platform.vision import describe_images, media_type_for, render_image_context

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
    try:  # learned rules ride next to the owner message too — end-of-prompt
        # placement keeps them salient for long system prompts
        from assistant.agent.lessons_store import LessonsStore, shared_store

        mt = settings.deployment_mode == "multi_tenant"
        rules: list = []
        if mt:
            try:  # shared G* rules first; a broken shared store never costs L*
                rules += shared_store(settings).active()
            except Exception:
                log.exception("shared lessons prompt injection failed")
        rules += LessonsStore(settings.profile_dir).active()
        if rules:
            # single_user header stays byte-identical to the legacy prompt
            scope = (" (G* rules are shared across all users; personal L* rules "
                     "win on conflict)" if mt else "")
            prompt += ("\n\n## Learned rules — apply to your reply AND to every "
                       f"action's parameters{scope}\n"
                       + "\n".join(f"- [{l['id']}] {l['rule']}" for l in rules))
    except Exception:
        log.exception("lessons prompt injection failed")
    system = system_prompt(settings)
    try:
        result = llm.complete_json(prompt, system=system, max_tokens=6000, role="chat",
                                   **({"images": attach} if attach else {}))
    except Exception as exc:
        if attach:
            # The native image call failed. When a SEPARATE vision backend is
            # configured, degrade to describe-then-reason. Otherwise the main
            # model IS the vision backend (llm_supports_images) — the failure
            # was almost certainly transient, so RETRY the native call rather
            # than routing to a backend that isn't there (which used to surface
            # a bogus "视觉后端不可用" even though the model can read images).
            log.warning("native image call failed (%s) — recovering", exc)
            from assistant.platform.vision import describe_images, render_image_context

            try:
                if settings.vision_api_key and settings.vision_model:
                    descriptions = describe_images(settings, attach)
                    fallback_prompt = prompt.replace(
                        "## Attached images\n(the owner's images are attached "
                        "to this message — look at them directly)",
                        render_image_context(descriptions))
                    result = llm.complete_json(fallback_prompt, system=system,
                                               max_tokens=6000)
                else:
                    result = llm.complete_json(prompt, system=system, max_tokens=6000,
                                               role="chat", images=attach)
            except Exception:
                log.exception("image retry/fallback failed too")
                return _finish("图片这次没能处理，请稍后重发一次 🙏 "
                               "急的话也可以把关键内容用文字发我。", "fail")
        else:
            log.exception("chat LLM call failed")
            return _finish("我这边连不上大脑了（LLM 接口报错），请稍后再试。"
                           f"\n技术细节: {str(exc)[:200]}", "fail")
    if not isinstance(result, dict):
        return _finish("(assistant error: unparseable model response)", "fail")
    reply = str(result.get("reply", "")).strip()
    actions = result.get("actions") or []
    self_check = result.get("self_check")
    model_feedback = result.get("prev_feedback")
    outcomes = execute(actions, settings)
    all_outcomes = list(outcomes)
    repair_rounds = 0
    hard_fail = False

    # Empty reply AND nothing done = the model returned nothing usable (mimo /
    # other reasoning models occasionally do this on an ambiguous fragment).
    # Retry once with a nudge; if still blank, degrade to a human ask — never
    # surface a raw "(empty reply)" to the owner.
    if not reply and not all_outcomes:
        log.warning("empty model reply with no actions — retrying once")
        try:
            retry = llm.complete_json(
                prompt + "\n\n(Your previous response was empty. Reply now with a "
                "concrete answer, or emit the right action — as JSON.)",
                system=system, max_tokens=6000, role="chat")
            if isinstance(retry, dict):
                reply = str(retry.get("reply", "")).strip()
                actions = retry.get("actions") or []
                self_check = retry.get("self_check") or self_check
                model_feedback = retry.get("prev_feedback") or model_feedback
                outcomes = execute(actions, settings)
                all_outcomes = list(outcomes)
        except Exception:
            log.exception("empty-reply retry failed")
        if not reply and not all_outcomes:
            reply = "抱歉，我刚才没组织好回复 🙏 可以再说一次，或者换个说法吗？"
            hard_fail = True  # the model never produced anything usable

    # Retrieval → compose: query_* actions pull profile records on demand (any
    # day/period, not just the fixed context snapshot). The first reply was
    # written before the query ran, so feed the fetched records back and let the
    # model answer FROM them; the composed answer cites the data, so the raw
    # records aren't echoed as a "✔" outcome. (well-formed actions map 1:1 to
    # outcomes in order — execute only skips non-dict/type-less entries.)
    wf = [a for a in actions if isinstance(a, dict) and a.get("type")][:5]
    retrieved = [(a, o) for a, o in zip(wf, outcomes)
                 if a["type"] in RETRIEVAL_ACTIONS and not looks_failed(o)]
    if retrieved:
        data = "\n\n".join(f"### {a['type']} result\n{o}" for a, o in retrieved)
        try:
            comp = llm.complete_json(
                f"{prompt}\n\n## Records you just retrieved from the profile\n{data}"
                "\n\nAnswer the owner's message using these retrieved records and the "
                "context above. Cite the specific records/totals; never say something "
                "is missing that appears here; do NOT re-run the query. Respond with "
                'ONLY JSON {"reply": "...", "actions": []}.',
                system=system, max_tokens=6000, role="chat")
            if isinstance(comp, dict) and str(comp.get("reply", "")).strip():
                reply = str(comp["reply"]).strip()
        except Exception:
            log.exception("retrieval compose failed")
        drop = {o for _, o in retrieved}
        all_outcomes = [o for o in all_outcomes if o not in drop]

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
            fix = llm.complete_json(review, system=system, max_tokens=6000, role="chat")
        except Exception:
            log.exception("action-review LLM call failed")
            break
        if isinstance(fix, dict):
            if str(fix.get("reply", "")).strip():
                reply = str(fix["reply"]).strip()  # revised even when unfixable
            self_check = fix.get("self_check") or self_check
            model_feedback = fix.get("prev_feedback") or model_feedback
        actions = (fix.get("actions") or []) if isinstance(fix, dict) else []
        if not actions:
            break
        outcomes = execute(actions, settings)
        repair_rounds += 1
        log.info("action review: retried %d action(s)", len(outcomes))
        all_outcomes += [f"(retry) {o}" for o in outcomes]

    if all_outcomes:
        reply = (reply + "\n\n" if reply else "") + "✔ " + "\n✔ ".join(all_outcomes)
    label, repaired, self_reported = classify_turn(
        all_outcomes, outcomes, retrieved=bool(retrieved), hard_fail=hard_fail,
        self_check=self_check)
    return _finish(reply, label, repaired=repaired, self_reported=self_reported,
                   actions_n=len(all_outcomes), repair_rounds=repair_rounds,
                   failures_left=sum(1 for o in all_outcomes if looks_failed(o)),
                   model_feedback=model_feedback)


def handle_message(text: str, settings: Settings, llm: LLM | None = None,
                   history: list[dict] | None = None,
                   image_paths: list[str] | None = None) -> str:
    """Back-compat string facade over `handle_turn` for callers that only
    need the reply (CLI ask, routines, the legacy channel service)."""
    return handle_turn(text, settings, llm, history=history,
                       image_paths=image_paths).reply
