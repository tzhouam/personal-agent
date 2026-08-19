"""Agentic executor for novel multi-step tasks (the copilot pattern), with
execution depth adapted to task difficulty.

Every task except a deterministically routed personal-history lookup is first
*assessed* (one cheap single-model call plus deterministic keyword clamps) into a tier:

- **simple** — answer directly or run a couple of actions: no plan, a 3-turn
  budget, and every LLM call forced single-model (no MoA) — the latency/cost
  floor for trivial tasks.
- **medium** — a short plan (3–6 steps, drafted single-model) is persisted in
  the task record and steered by in the loop; 12-turn budget, still no MoA.
- **complex** — the plan is drafted on the configured ``task`` role (the one
  place MoA quality is warranted) and carries per-milestone status the model
  updates each turn (``"milestone_done": n``) plus a verify check the finish
  report must address.

**Approval is gated at action dispatch, at every tier** — the safety boundary
is the registry's ``risky`` metadata (`actions.is_risky`), not the request
text: before executing any action with outward/irreversible effects
(``run_phase website``, ``reboot``) an unapproved task pauses as
``awaiting_approval`` with the pending action persisted and the owner notified
("批准请回复: 批准任务 <id>"). A complex task whose *assessment* is risky
(publishing intent) pauses before its first step as a fast-path. The owner's
``approve_task`` action re-launches it (`approved_task_id`), which resumes from
the persisted steps — executed steps are never replayed — and executes the
pending action first.

Lifecycle is atomic and idempotent: collision-safe ids
(``task-YYYYMMDD-HHMMSS-<6 hex>``), atomic ``os.replace`` persistence, locked
status transitions (``awaiting_approval → queued → running``), and terminal
statuses (done/partial/blocked/error) that a replayed queue job refuses to re-run.

Bounded on purpose: per-tier turn budgets, 3 consecutive failed actions →
blocked with a progress report. Recursive/heavy actions (``execute_task``,
``plan_task``, ``trigger_run``, ``approve_task``) are excluded from the
runner's action set. Every run gets its own trace
(``DATA_DIR/tasks/<id>-trace.jsonl``) and a numeric ``task`` metrics row.
"""

import json
import logging
import re
import time
import uuid
from datetime import datetime

from assistant.platform.config import Settings

log = logging.getLogger("assistant")

EXCLUDED_ACTIONS = ("execute_task", "plan_task", "trigger_run", "approve_task",
                    # Tasks use the evidence-preserving batch surface instead:
                    # web_search spends another LLM call per query and returns
                    # prose rather than source records.
                    "web_search",
                    # the workflow surface is owner-only: a background task must
                    # not author persistent behavior or fan out detached runs
                    "create_workflow", "run_workflow", "show_workflow",
                    "update_workflow", "retire_workflow")

# Task ids are file names — validate before any path is built.
TASK_ID_RE = re.compile(r"^task-\d{8}-\d{6}-[0-9a-f]{6}$")
_URL_RE = re.compile(r"https?://[^\s\]\)}>]+")

TIERS = ("simple", "medium", "complex")
_TURN_BUDGET = {"simple": 3, "medium": 12, "complex": 12}

# Deterministic clamps — they only ever RAISE the assessed tier (a fast-path,
# not the safety boundary: that is the per-action `is_risky` gate below).
_COMPLEX_MARKERS = ("发布", "publish", "网站", "website", "简历", "résumé",
                    "resume", "群发", "broadcast", "deploy", "邮件", "email")
_MEDIUM_MARKERS = ("记账", "转账", "花了", "工资", "transaction", "体重", "吃了",
                   "健身", "锻炼", "发送", "send", "通知", "notify", "买", "buy",
                   "订", "book")

_ASSESS_SYSTEM = """You classify a personal-assistant task request before execution. Respond with
ONLY JSON:
{"tier": "simple|medium|complex",
 "flags": {"external_side_effects": false, "mutates_finance_or_health": false,
           "publishes": false, "ambiguous": false, "long_running": false}}
tier guide: simple = one lookup or one action (a search, one reminder, one log);
medium = a few coordinated steps toward a clear goal; complex = many steps, real
ambiguity, outward side effects, or anything the owner would want to sign off on."""

_PLAN_SYSTEM = """You write a short, capability-valid execution plan for a personal assistant.
The exact available actions are supplied in the prompt. Respond with ONLY JSON:
{"requirements": ["<observable deliverable>", ...],
 "steps": [{"step": "<concrete step>",
            "action": {"type": "<available action>", "<arg>": "<value>"},
            "produces": ["<required output>"]},
           {"step": "<reason over accumulated evidence; omit action when no tool runs>"}],
 "verify": "<how to check every requested outcome before reporting>",
 "risks": "<one line: what could go wrong>"}
Use 2-6 steps. Never name an action that is not in Available actions. Prefer ONE
web_research action containing several independent queries over sequential searches.
For older chats or previously recorded owner data use search_personal_data. Read-only
independent work should be batched. Never infer a year absent from evidence."""

_RUNNER_SYSTEM = """You are executing a task for your owner, step by step, on your own.
Work from the context below; use actions to gather information and to act. One step per
response. Review each action's result before deciding the next step — if an action failed,
analyze the message and try a corrected or different approach rather than repeating it.

Available actions (one per response):
{actions}

Respond with ONLY JSON, one of:
  {{"thought": "<why this step>", "action": {{"type": "<name>", ...params}},
    "milestone_done": <n — optional: this step completes plan milestone n>}}
  {{"thought": "<wrap-up>", "finish": "<final report for the owner — concise, concrete,
    in the owner's language, citing evidence ids such as [P-…]/[W-…]; admit gaps>",
    "completion": "full|partial|blocked"}}

Rules: prefer web_research for anything needing current/external information; use reminders/
routines/todos/ledgers when the task calls for them; never invent results an action didn't
return; finish as soon as the task is genuinely done (don't pad steps). Actions with outward
effects (publishing the website, rebooting) automatically pause the task for the owner's
approval — that pause is normal, not a failure. When the plan names a verify check, do it
and address the result in your finish report. Never infer an event year that is absent from
the evidence. If sources conflict, report the conflict. Use only exact URLs returned by an
action. A task is full only when every requested deliverable is verified."""


_PERSONAL_HISTORY_MARKERS = (
    "历史对话", "历史聊天", "对话记录", "聊天记录", "更早之前", "过去的记录",
    "之前聊过", "以前提过", "我说过", "之前的记录",
    "conversation history", "chat history", "past conversation", "earlier conversation",
    "we discussed", "i mentioned before", "previously recorded",
)


def _personal_history_request(request: str) -> bool:
    low = str(request).casefold()
    return any(marker in low for marker in _PERSONAL_HISTORY_MARKERS)


def _available_actions(settings: Settings, include=None) -> str:
    """Mode-aware task action block, excluding recursive/heavy surfaces."""
    from assistant.agent.actions import prompt_block

    return "\n".join(
        line for line in prompt_block(settings, include=include).splitlines()
        if not any(f'"{name}"' in line for name in EXCLUDED_ACTIONS))


def _personal_history_plan(request: str) -> dict:
    """Deterministic route for the production failure where retained chat data
    existed but the general planner claimed it was inaccessible."""
    search = {"type": "search_personal_data", "query": request,
              "sources": "sessions,todos,reminders", "limit": 20}
    needs_update = any(marker in request.casefold() for marker in (
        "设置提醒", "设个提醒", "添加待办", "创建待办", "取消提醒", "完成待办",
        "顺便提醒", "set a reminder", "add a todo", "cancel reminder", "mark todo"))
    steps = [
        {"step": "Search retained conversations, todos, and reminders for the named records.",
         "action": search, "produces": ["timestamped personal evidence"]},
    ]
    if needs_update:
        steps.append(
            {"step": "Perform the requested todo/reminder updates and verify their outcomes.",
             "produces": ["requested durable updates"]})
    steps.append(
        {"step": "Extract and report the requested fields with [P…] evidence ids; "
                 "preserve unknown fields as unknown.",
         "produces": ["verified owner report"]},
    )
    requirements = ["recover and cite the requested historical facts"]
    if needs_update:
        requirements.append("perform and verify the requested follow-up actions")
    return {"steps": [s["step"] for s in steps],
            "requirements": requirements,
            "verify": "Re-query or list the affected stores and cite the personal evidence ids.",
            "risks": "Retained records may omit a requested field; report it as unknown.",
            "milestones": [{**step, "done": False} for step in steps],
            "bootstrap_action": search,
            "bootstrap_milestone": 1,
            "needs_update": needs_update,
            "route": "personal_history"}


def _assess(request: str, settings: Settings, llm) -> dict:
    """Tier + risk flags for `request`: one cheap **single-model** call (the
    classification must never pay MoA), then the deterministic keyword clamps —
    which only raise the tier. LLM failure degrades to `medium` (the safe
    middle). Returns `{"tier", "flags", "risky"}`."""
    flags = {k: False for k in ("external_side_effects", "mutates_finance_or_health",
                                "publishes", "ambiguous", "long_running")}
    tier = "medium"
    try:
        out = llm.complete_json(f"## Task request\n{request}", system=_ASSESS_SYSTEM,
                                max_tokens=2000, role="task", mixture=False)
        if isinstance(out, dict):
            if out.get("tier") in TIERS:
                tier = out["tier"]
            got = out.get("flags") or {}
            for key in flags:
                flags[key] = bool(got.get(key))
    except Exception:
        log.warning("task assessment failed — defaulting to medium", exc_info=True)
    low = request.lower()
    if any(m in low for m in _COMPLEX_MARKERS):
        tier, flags["publishes"] = "complex", True
    elif tier == "simple" and (any(m in low for m in _MEDIUM_MARKERS)
                               or flags["external_side_effects"]
                               or flags["mutates_finance_or_health"]):
        tier = "medium"
    if flags["publishes"]:
        tier = "complex"
    return {"tier": tier, "flags": flags, "risky": flags["publishes"]}


def _draft_plan(request: str, context: str, llm, tier: str,
                actions_block: str) -> dict | None:
    """A short persisted plan: steps + verify + risks, with per-milestone
    status. Medium drafts single-model (cost floor); complex drafts on the
    configured `task` role — the one spot MoA quality is warranted. None when
    the model produced nothing usable (the loop then runs planless)."""
    kwargs = {} if tier == "complex" else {"mixture": False}
    try:
        out = llm.complete_json(
                                f"## Available actions\n{actions_block}\n\n"
                                f"## Context\n{context[:4000]}\n\n## Task\n{request}",
                                system=_PLAN_SYSTEM, max_tokens=4000, role="task",
                                **kwargs)
    except Exception:
        log.warning("plan drafting failed — running planless", exc_info=True)
        return None
    if not isinstance(out, dict) or not out.get("steps"):
        return None
    from assistant.agent.actions import ACTIONS, validate

    milestones = []
    capability_errors = []
    for raw in out["steps"][:6]:
        if isinstance(raw, dict):
            description = str(raw.get("step") or "")[:300]
            planned_action = raw.get("action")
            produces = [str(v)[:120] for v in (raw.get("produces") or [])[:6]]
        else:  # tolerate the old planner schema during upgrades and in tests
            description, planned_action, produces = str(raw)[:300], None, []
        if not description:
            continue
        milestone = {"step": description, "done": False}
        if isinstance(planned_action, dict) and planned_action.get("type"):
            action_name = str(planned_action["type"])
            action_def = ACTIONS.get(action_name)
            if (action_def is None or not action_def.llm
                    or f'"{action_name}"' not in actions_block
                    or action_name in EXCLUDED_ACTIONS):
                capability_errors.append(
                    f"step {len(milestones) + 1}: unavailable action {action_name}")
            else:
                validation_error = validate(action_def, planned_action)
                if validation_error:
                    capability_errors.append(
                        f"step {len(milestones) + 1}: {validation_error}")
                else:
                    milestone["action"] = planned_action
        if produces:
            milestone["produces"] = produces
        milestones.append(milestone)
    if not milestones:
        return None
    requirements = [str(v)[:200] for v in (out.get("requirements") or [])[:8]]
    plan = {"steps": [m["step"] for m in milestones],
            "requirements": requirements or [m["step"] for m in milestones],
            "verify": str(out.get("verify", ""))[:300],
            "risks": str(out.get("risks", ""))[:300],
            "milestones": milestones,
            "capability_errors": capability_errors}
    # A validated first read-only step is safe to dispatch without spending a
    # controller turn asking the model to repeat its own plan.
    for index, milestone in enumerate(milestones, 1):
        planned_action = milestone.get("action")
        action_def = ACTIONS.get((planned_action or {}).get("type"))
        if planned_action and action_def and action_def.read_only:
            plan["bootstrap_action"] = planned_action
            plan["bootstrap_milestone"] = index
            break
        if planned_action:  # don't jump over an earlier mutation/dependency
            break
    return plan


def _plan_block(record: dict) -> str:
    """The per-turn plan section: milestones with live checkboxes plus the
    verify instruction — rebuilt each turn so ticked milestones show."""
    plan = record.get("plan")
    if not plan:
        return ""
    lines = [f"{i}. [{'x' if m.get('done') else ' '}] {m['step']}"
             for i, m in enumerate(plan.get("milestones", []), 1)]
    block = ("\n\n## Plan (follow it; adapt when a step fails; tick progress with "
             '"milestone_done": <n>)\n' + "\n".join(lines))
    if plan.get("verify"):
        block += f"\nVerify before finishing: {plan['verify']}"
    if plan.get("capability_errors"):
        block += ("\nPlanner validation rejected these unavailable/invalid steps; "
                  "adapt using Available actions:\n- "
                  + "\n- ".join(plan["capability_errors"]))
    return block


def _mark_milestone(record: dict, number) -> None:
    """Tick a 1-based milestone without trusting model-supplied indices."""
    milestones = (record.get("plan") or {}).get("milestones", [])
    if isinstance(number, int) and 1 <= number <= len(milestones):
        milestones[number - 1]["done"] = True


def _coverage(record: dict) -> float:
    milestones = (record.get("plan") or {}).get("milestones", [])
    if milestones:
        return sum(bool(m.get("done")) for m in milestones) / len(milestones)
    return 1.0 if record.get("completion") == "full" else 0.0


def _record_evidence(record: dict, provenance: list[dict]) -> None:
    """Persist stable evidence ids so finish claims can be mechanically gated."""
    ids = record.setdefault("evidence_ids", [])
    for source in provenance:
        evidence_id = str(source.get("id") or "")
        if evidence_id and evidence_id not in ids:
            ids.append(evidence_id)


def _plan_action_names(record: dict) -> set[str] | None:
    """Return the action shortlist implied by a validated plan.

    Personal-history tasks also need the durable update/verification tools even
    though the exact mutation cannot be known until retrieval has run.
    """
    plan = record.get("plan") or {}
    names = {
        str(m["action"]["type"])
        for m in plan.get("milestones", [])
        if isinstance(m.get("action"), dict) and m["action"].get("type")
    }
    if plan.get("route") == "personal_history":
        names.add("search_personal_data")
        if plan.get("needs_update"):
            names.update({"list_todos", "add_todo", "done_todo", "list_reminders",
                          "set_reminder", "cancel_reminder"})
    return names or None


def _clean_url(url: str) -> str:
    return str(url).rstrip(".,;:!?，。；！、'\"")


def _compact_result(result) -> dict:
    data = result.data if isinstance(result.data, dict) else {}
    grounded_text = []
    if isinstance(data.get("hits"), list):
        grounded_text.extend(
            f"{hit.get('title', '')} {hit.get('text', '')}"
            for hit in data["hits"] if isinstance(hit, dict))
    if isinstance(data.get("sources"), list):
        grounded_text.extend(
            f"{source.get('title', '')} {source.get('snippet', '')}"
            for source in data["sources"] if isinstance(source, dict))
    if isinstance(data.get("queries"), list):
        grounded_text.extend(
            str(query.get("answer") or "") for query in data["queries"]
            if isinstance(query, dict))
    # For mutation/list actions the text itself is the typed handler's ground
    # truth. Retrieval actions deliberately exclude headers such as
    # `retrieved_at`, which otherwise make the current year look supported.
    support = "\n".join(grounded_text) if grounded_text else result.text
    supported_urls = {_clean_url(url) for url in _URL_RE.findall(support)}
    supported_urls.update(_clean_url(str(source.get("url")))
                          for source in result.provenance if source.get("url"))
    return {"ok": bool(result.ok), "error": str(result.error or "")[:300],
            "confidence": result.confidence,
            "evidence_ids": [str(v.get("id")) for v in result.provenance
                             if v.get("id")],
            "supported_years": sorted(set(re.findall(r"\b20\d{2}\b", support))),
            "supported_urls": sorted(supported_urls)}


def _unsupported_years(record: dict, report: str) -> list[str]:
    """Event years claimed after retrieval but absent from the retrieved facts.

    This is intentionally narrow: it activates only when evidence was gathered,
    and accepts years the owner explicitly supplied. It catches the observed
    failure of turning an undated event into "2026" merely because the task ran
    in 2026.
    """
    if not record.get("evidence_ids"):
        return []
    claimed = set(re.findall(r"\b20\d{2}\b", report))
    supported = set(re.findall(r"\b20\d{2}\b", record.get("request", "")))
    for step in record.get("steps", []):
        supported.update((step.get("result") or {}).get("supported_years") or [])
    return sorted(claimed - supported)


def _unsupported_urls(record: dict, report: str) -> list[str]:
    """URLs in a retrieval-backed report must come from evidence or the owner."""
    if not record.get("evidence_ids"):
        return []
    claimed = {_clean_url(url) for url in _URL_RE.findall(report)}
    supported = {_clean_url(url) for url in _URL_RE.findall(record.get("request", ""))}
    for step in record.get("steps", []):
        supported.update((step.get("result") or {}).get("supported_urls") or [])
    return sorted(claimed - supported)


def _pause_for_approval(record: dict, settings: Settings, notify: bool,
                        reason: str, pending_action: dict | None = None) -> dict:
    """Persist the task as awaiting_approval (with any pending risky action)
    and tell the owner how to release it. Returns the record."""
    record["status"] = "awaiting_approval"
    record["approval_reason"] = reason
    if pending_action is not None:
        record["pending_action"] = pending_action
    _persist(settings, record)
    if notify:
        try:
            from assistant.platform.notify import send_wechat

            plan = record.get("plan") or {}
            steps = "\n".join(f"  {i}. {m['step']}" for i, m in
                              enumerate(plan.get("milestones", [])[:6], 1))
            status = send_wechat(settings, (
                f"⏸ [任务待批准] {record['request'][:100]}\n{reason}\n"
                + (f"计划:\n{steps}\n" if steps else "")
                + (f"待执行动作: {json.dumps(pending_action, ensure_ascii=False)[:150]}\n"
                   if pending_action else "")
                + f"批准请回复: 批准任务 {record['id']}"))
            # This push carries the ONLY copy of the id `approve_task` requires,
            # so losing it strands the task permanently. send_wechat never
            # raises — it RETURNS "sent"/"disabled …"/"failed: …" — so the
            # except below could never have caught a drop. Record it on the D5
            # surface, which carries the id into the owner's next reply.
            _note_push_failure(settings, status, "任务待批准但推送失败", record)
        except Exception:
            log.exception("approval notify failed")
    return record


def _note_push_failure(settings: Settings, status: str, what: str,
                       record: dict) -> None:
    """Log a WeChat push verdict and, when it isn't "sent", put it on the D5
    failure surface so it reaches the owner's next chat reply.

    `send_wechat` reports failure by RETURN VALUE ("sent" / "disabled …" /
    "failed: …"), never by raising, so a bare call discards the only signal
    there is. Two live cases make that concrete: a tenant whose ANNOUNCE_* is
    unset gets "disabled" for 100% of pushes, and the documented ~24h WeChat
    context-token window closes on an idle owner. Never raises — a failure to
    report a failure must not fail the task."""
    if status == "sent":
        return
    log.warning("task %s push not delivered: %s", record.get("id"), status)
    try:
        from assistant.platform.delivery import OutboxDB

        db = OutboxDB(settings.data_dir)
        try:
            db.add_system_note(f"{what}: {record.get('id')} "
                               f"({str(status)[:60]})")
        finally:
            db.close()
    except Exception:
        log.exception("could not record task push failure")


def _load_approved(settings: Settings, task_id: str,
                   allow_running: bool = False) -> dict | None:
    """Locked `queued → running` transition for a dispatched task record.

    Accepts `queued` (fresh approval / workflow start / rescue). `running` is
    accepted ONLY with `allow_running=True` — the queue-worker retry path,
    where the previous holder is dead by construction (the queue marks
    failure after the worker raised) — plus the owner's manual
    `--force-resume` escape. The CLI/approve paths never resume a `running`
    record, so at most one live runner ever holds a record (the queue's
    active-scoped dedupe key guards the enqueue side). Terminal and
    still-awaiting records are always refused: a replayed job must never
    re-run a finished task or jump an approval."""
    from assistant.platform.locks import data_write_lock

    if not TASK_ID_RE.match(str(task_id or "")):
        return None
    path = settings.data_dir / "tasks" / f"{task_id}.json"
    with data_write_lock(settings.data_dir):
        if not path.exists():
            return None
        try:
            record = json.loads(path.read_text())
        except ValueError:
            return None
        allowed = ("queued", "running") if allow_running else ("queued",)
        if record.get("status") not in allowed:
            return None
        record["status"] = "running"
        _persist(settings, record)
    return record


def run_task(request: str, settings: Settings, llm=None, max_turns: int = 12,
             notify: bool = True, cancel_check=None,
             approved_task_id: str | None = None,
             force_resume: bool = False) -> dict:
    """Execute `request` agentically; returns the task record `{id, request,
    tier, status, steps, report, …}` (status: done | partial | blocked | error |
    awaiting_approval | cancelled). `notify` pushes the report/approval ask to
    WeChat — on by default because tasks run detached; the CLI passes False in
    the foreground. `cancel_check` (§6): optional zero-arg callable invoked at
    the top of every turn; raising from it (the job worker passes
    `CancelToken.check`) aborts between steps.

    `approved_task_id` re-enters a dispatched record (owner approval, a
    workflow start, or a queue retry — `force_resume=True` additionally
    accepts a `running` record, the worker/owner-escape path). Approval is
    **at-most-once, per action**: the record's `pre_approved` +
    `pending_action` are consumed (popped and persisted) at load; the pending
    action then executes exactly once without re-gating, and every LATER
    risky action pauses afresh. A crash between consumption and execution
    loses the authorization — the model re-emits the action and the task
    re-pauses for approval (safe direction: never double-executed)."""
    from assistant.agent.actions import execute_results, is_risky
    from assistant.agent.chat.agent import build_context
    from assistant.platform.llm import LLM
    from assistant.platform import tracing

    llm = llm or LLM(settings)
    pending = None
    resumed = False
    if approved_task_id:
        record = _load_approved(settings, approved_task_id,
                                allow_running=force_resume)
        if record is None:
            return {"id": str(approved_task_id), "status": "error",
                    "report": "task not found, not approved, or already finished",
                    "steps": []}
        resumed = True
        record.setdefault("steps", [])
        # consume the one-shot approval NOW (persisted): at most one action
        # runs unguarded, and a replay can never find a live authorization
        approved_one = bool(record.pop("pre_approved", False))
        pending = record.pop("pending_action", None)
        if pending is not None and not approved_one:
            log.warning("task %s: pending action without approval — discarded "
                        "(it will re-gate if the model re-emits it)", record["id"])
            pending = None
        _persist(settings, record)
        # a task belonging to a retired/unavailable workflow must not run
        if record.get("workflow_id"):
            from assistant.agent.workflow_store import WorkflowStore, WorkflowStoreError

            try:
                wf = WorkflowStore(settings.profile_dir).get(record["workflow_id"])
            except WorkflowStoreError:
                wf = None
            if wf is None:
                record["status"] = "cancelled"
                record["report"] = (f"workflow {record['workflow_id']} is retired "
                                    "or unavailable — task cancelled")
                _persist(settings, record)
                return record
    else:
        task_id = (datetime.now().strftime("task-%Y%m%d-%H%M%S-")
                   + uuid.uuid4().hex[:6])
        record = {"id": task_id, "request": str(request)[:500],
                  "started": datetime.now().strftime("%Y-%m-%d %H:%M"),
                  "status": "running", "steps": [], "report": ""}

    # per-task trace (ContextVar-scoped): LLM/MoA spans land in the task's own
    # file — without this, spans outside a pipeline run are silent no-ops
    tracing.init(record["id"], settings.data_dir / "tasks" / f"{record['id']}-trace.jsonl")
    start = time.monotonic()

    if "assessment" not in record:
        if _personal_history_request(record["request"]):
            # Known route, known capability: skip an LLM classification call.
            record["assessment"] = {
                "tier": "medium",
                "flags": {k: False for k in (
                    "external_side_effects", "mutates_finance_or_health",
                    "publishes", "ambiguous", "long_running")},
                "risky": False,
                "route": "personal_history",
            }
        else:
            record["assessment"] = _assess(record["request"], settings, llm)
    tier = record["assessment"]["tier"]
    if record.get("workflow_id") and tier == "simple":
        tier = "medium"              # a saved workflow is multi-step by definition
    record["assessment"]["tier"] = tier   # persist the CLAMPED tier — a resume
    record["tier"] = tier                 # must never re-derive a laxer one
    # Planning is the only task-runner call where complex work may warrant MoA.
    # Execution turns are short control decisions; single-model avoids multiplying
    # both latency and synthesis failure at every step.
    loop_kwargs = {"mixture": False}
    turns = min(max_turns, _TURN_BUDGET.get(tier, 12))

    all_actions_block = _available_actions(settings)
    context = build_context(settings)

    if tier != "simple" and not record.get("plan"):
        if _personal_history_request(record["request"]):
            record["plan"] = _personal_history_plan(record["request"])
        else:
            record["plan"] = _draft_plan(record["request"], context, llm, tier,
                                         all_actions_block)

    actions_block = _available_actions(settings, include=_plan_action_names(record))
    system = _RUNNER_SYSTEM.format(actions=actions_block)
    try:
        from assistant.agent.lessons_store import combined_prompt_block

        system += combined_prompt_block(settings)   # shared G* then personal L*
    except Exception:
        log.exception("lessons injection failed")
    if not resumed and tier == "complex" and record["assessment"].get("risky"):
        # fast-path pause on FRESH tasks only: a plan with publishing intent
        # never starts unapproved. On resume, approval authorized *starting* —
        # the per-action gate below still pauses every risky action afresh.
        result = _pause_for_approval(record, settings, notify,
                                     reason="计划包含对外发布类步骤，需要你确认")
        _record_task_metrics(settings, record, time.monotonic() - start)
        return result
    _persist(settings, record)

    consecutive_failures = 0
    bootstrap = (record.get("plan") or {}).get("bootstrap_action")
    if bootstrap and not record.get("bootstrap_done") and pending is None:
        # Retrieval-first for known personal-history work. This is deterministic,
        # so the model cannot incorrectly decide that retained data is inaccessible.
        result = execute_results([bootstrap], settings)[0]
        record["steps"].append({
            "thought": "Run the validated first read-only plan step before reasoning.",
            "action": bootstrap,
            "outcome": result.text,
            "result": _compact_result(result),
        })
        _record_evidence(record, result.provenance)
        if result.ok:
            _mark_milestone(record, (record.get("plan") or {}).get(
                "bootstrap_milestone", 1))
        else:
            consecutive_failures = 1
        record["bootstrap_done"] = True
        _persist(settings, record)

    if pending is not None:
        turns += 1   # the approved pending action gets its own turn even when
                     # the pause landed on the last budgeted step

    while len(record["steps"]) < turns:
        if cancel_check is not None:   # §6: per-turn cancellation checkpoint —
            cancel_check()             # outside the LLM try so the raise propagates
        # Approval is tracked HERE, in a local the model cannot reach — never as
        # a key read back out of `move`. `move` is either the synthetic dict
        # below or raw `llm.complete_json` output, so a model that emitted
        # `_approved: true` next to a risky action used to walk straight through
        # the gate. Task prompts carry web-search results and action outcomes,
        # which makes that an injection surface onto the one boundary protecting
        # outward actions.
        approved_this_turn = False
        if pending is not None:        # the ONE approved action — runs unguarded,
            move = {"thought": "(owner approved the pending action)",
                    "action": pending}                       # then gating resumes
            approved_this_turn = True
            pending = None
        else:
            # Bound controller context growth. Keep a generous slice of the
            # latest evidence-bearing result and compact older steps; the full
            # result remains in the persisted task artifact.
            visible = record["steps"][-8:]
            first_number = len(record["steps"]) - len(visible) + 1
            rendered = []
            for offset, old_step in enumerate(visible):
                limit = 12_000 if offset == len(visible) - 1 else 1_500
                outcome = str(old_step.get("outcome") or "")[:limit]
                rendered.append(
                    f"[step {first_number + offset}] thought: "
                    f"{old_step.get('thought', '')}\n  action: "
                    f"{json.dumps(old_step.get('action'), ensure_ascii=False)}\n"
                    f"  result_meta: {json.dumps(old_step.get('result'), ensure_ascii=False)}\n"
                    f"  result: {outcome}")
            transcript = "\n".join(rendered) or "(no steps yet)"
            prompt = (f"## Context\n{context}{_plan_block(record)}\n\n"
                      f"## Task from the owner\n{record['request']}"
                      f"\n\n## Steps so far\n{transcript}\n\n## Next\nDecide the "
                      "next single step, or finish with the report.")
            try:
                move = llm.complete_json(prompt, system=system, max_tokens=6000,
                                         role="task", **loop_kwargs)
            except Exception as exc:
                record["status"], record["report"] = "error", f"LLM failed mid-task: {exc}"
                break
            if not isinstance(move, dict):
                record["status"], record["report"] = "error", "unparseable step from the model"
                break
        if move.get("finish") is not None:
            _mark_milestone(record, move.get("milestone_done"))
            milestones = (record.get("plan") or {}).get("milestones", [])
            undone = [str(i + 1) for i, m in enumerate(milestones)
                      if not m.get("done")]
            report = str(move.get("finish") or "").strip() or "(empty report)"
            evidence_ids = record.get("evidence_ids", [])
            citations_missing = bool(evidence_ids) and not any(
                f"[{evidence_id}]" in report for evidence_id in evidence_ids)
            unsupported_years = _unsupported_years(record, report)
            unsupported_urls = _unsupported_urls(record, report)
            if ((undone or citations_missing or unsupported_years or unsupported_urls)
                    and not record.get("finish_nudged")):
                # Truthful completion: one bounded correction turn when coverage
                # or evidence attribution is incomplete.
                record["finish_nudged"] = True
                record["last_finish"] = report
                issues = []
                if undone:
                    issues.append(f"milestones {', '.join(undone)} are incomplete")
                if citations_missing:
                    record["citation_corrections"] = record.get(
                        "citation_corrections", 0) + 1
                    issues.append("the report cites none of the gathered evidence ids "
                                  + ", ".join(evidence_ids[:8]))
                if unsupported_years:
                    record["unsupported_year_corrections"] = record.get(
                        "unsupported_year_corrections", 0) + 1
                    issues.append("the report introduces year(s) absent from the evidence: "
                                  + ", ".join(unsupported_years))
                if unsupported_urls:
                    record["unsupported_url_corrections"] = record.get(
                        "unsupported_url_corrections", 0) + 1
                    issues.append("the report introduces URL(s) absent from the evidence: "
                                  + ", ".join(unsupported_urls[:4]))
                record["steps"].append({
                    "thought": str(move.get("thought", ""))[:300], "action": None,
                    "outcome": ("(finish rejected once — " + "; ".join(issues)
                                + "; complete them or explicitly report the gaps)")})
                _persist(settings, record)
                continue
            requested = str(move.get("completion") or "").lower()
            if requested == "blocked":
                record["status"] = record["completion"] = "blocked"
            elif (undone or citations_missing or unsupported_years or unsupported_urls
                  or requested == "partial"):
                record["status"] = record["completion"] = "partial"
            else:
                record["status"], record["completion"] = "done", "full"
            record["report"] = report
            record["steps"].append({"thought": str(move.get("thought", ""))[:300],
                                    "action": None, "outcome": "(finished)"})
            break

        action = move.get("action")
        step = {"thought": str(move.get("thought", ""))[:300], "action": action}
        if not isinstance(action, dict) or not action.get("type"):
            step["outcome"] = "no action emitted — emit an action or finish"
            consecutive_failures += 1
        elif action.get("type") in EXCLUDED_ACTIONS:
            step["outcome"] = f"action {action['type']!r} is not available inside a task"
            consecutive_failures += 1
        elif is_risky(action["type"], action) and not approved_this_turn:
            # THE approval boundary: an outward/irreversible action pauses the
            # task at every tier — only the single just-approved pending action
            # bypasses it, so a task with two risky steps pauses twice.
            record["steps"].append({**step, "outcome": "(paused — owner approval required)"})
            result = _pause_for_approval(
                record, settings, notify,
                reason=f"下一步 {action['type']} 有对外影响，需要你确认",
                pending_action=action)
            _record_task_metrics(settings, record, time.monotonic() - start)
            return result
        else:
            action_key = json.dumps(action, ensure_ascii=False, sort_keys=True)
            prior = next((old for old in reversed(record["steps"])
                          if isinstance(old.get("action"), dict)
                          and json.dumps(old["action"], ensure_ascii=False,
                                         sort_keys=True) == action_key
                          and (old.get("result") or {}).get("ok")), None)
            if prior is not None:
                step["outcome"] = ("duplicate action skipped — use the prior result: "
                                   + str(prior.get("outcome", ""))[:1200])
                step["result"] = {"ok": False, "error": "duplicate_action",
                                  "confidence": None, "evidence_ids": []}
                failed = True
            else:
                action_result = execute_results([action], settings)[0]
                step["outcome"] = action_result.text
                step["result"] = _compact_result(action_result)
                _record_evidence(record, action_result.provenance)
                failed = not action_result.ok
            consecutive_failures = consecutive_failures + 1 if failed else 0
            if not failed:   # a milestone only counts on a successful outcome
                _mark_milestone(record, move.get("milestone_done"))
        record["steps"].append(step)
        _persist(settings, record)
        if consecutive_failures >= 3:
            record["status"] = record["completion"] = "blocked"
            record["report"] = ("Stopped after 3 consecutive failed steps. "
                                "Progress so far:\n"
                                + "\n".join(str(s.get("outcome", "")) for s in
                                            record["steps"][-4:]))
            break
    else:
        verified = [s for s in record["steps"] if (s.get("result") or {}).get("ok")]
        if verified:
            record["status"] = record["completion"] = "partial"
            progress = "\n".join(
                f"- {str(s.get('outcome') or '')[:400]}" for s in verified[-5:])
            candidate = str(record.get("last_finish") or "").strip()
            record["report"] = ((candidate + "\n\n") if candidate else "") + (
                f"Reached the {turns}-step budget. Verified progress:\n{progress}\n"
                "Remaining work was not completed.")
        else:
            record["status"] = record["completion"] = "blocked"
            record["report"] = (f"Reached the {turns}-step budget without a verified "
                                "result. No completion is claimed.")

    if record.get("workflow_id") and record.get("status") in ("done", "partial"):
        # BEFORE the terminal persist: a crash between the two replays the
        # finish, and mark_ran's task-id idempotency makes the counter
        # exactly-once (a crash in the other order would undercount forever)
        try:
            from assistant.agent.workflow_store import WorkflowStore

            WorkflowStore(settings.profile_dir).mark_ran(
                record["workflow_id"],
                "partial" if record.get("status") == "partial" else "done",
                record["id"])
        except Exception:
            log.exception("workflow mark_ran failed")
    _persist(settings, record)
    _record_task_metrics(settings, record, time.monotonic() - start)
    if notify:
        try:
            from assistant.platform.notify import send_wechat

            mark = "✅" if record["status"] == "done" else "⚠️"
            status = send_wechat(settings,
                                 f"{mark} [任务] {record['request'][:80]}\n"
                                 f"{record['report'][:1600]}")
            _note_push_failure(settings, status, "任务报告没送达", record)
        except Exception:
            log.exception("task result notify failed")
    return record


def _record_task_metrics(settings: Settings, record: dict, duration_s: float) -> None:
    """One numeric `task` metrics row per run (record_metrics drops
    non-floats, so statuses are one-hot and the tier is its index)."""
    try:
        from assistant.agent.events_store import EventsStore

        steps = record.get("steps") or []
        events = EventsStore(settings.events_db)
        events.record_metrics(record["id"], "task", {
            "duration_s": round(duration_s, 2),
            "steps": len(steps),
            "tier": TIERS.index(record.get("tier", "medium")),
            "workflow": int(bool(record.get("workflow_id"))),
            "done": int(record.get("status") == "done"),
            "full": int(record.get("completion") == "full"),
            "partial": int(record.get("status") == "partial"),
            "blocked": int(record.get("status") == "blocked"),
            "coverage": round(_coverage(record), 3),
            "evidence": len(record.get("evidence_ids") or []),
            "failed_steps": sum(
                isinstance(s.get("result"), dict) and not s["result"].get("ok")
                for s in steps),
            "duplicate_actions": sum(
                (s.get("result") or {}).get("error") == "duplicate_action"
                for s in steps),
            "capability_errors": len((record.get("plan") or {}).get(
                "capability_errors") or []),
            "citation_corrections": record.get("citation_corrections", 0),
            "unsupported_year_corrections": record.get(
                "unsupported_year_corrections", 0),
            "unsupported_url_corrections": record.get(
                "unsupported_url_corrections", 0),
            "aborted": int(record.get("status") == "aborted"),
            "awaiting": int(record.get("status") == "awaiting_approval"),
            "error": int(record.get("status") == "error")})
        events.close()
    except Exception:
        log.exception("task metrics failed")


def _persist(settings: Settings, record: dict) -> None:
    """Atomically write the task record under DATA_DIR/tasks/ (tmp +
    `os.replace` — a crash mid-write can't leave a torn record). Best-effort."""
    try:
        tasks_dir = settings.data_dir / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        path = tasks_dir / f"{record['id']}.json"
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2))
        tmp.replace(path)
    except Exception:
        log.exception("task persist failed")
