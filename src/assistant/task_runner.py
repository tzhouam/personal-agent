"""Agentic executor for novel multi-step tasks (the copilot pattern), with
execution depth adapted to task difficulty.

Every task is first *assessed* (one cheap single-model call plus deterministic
keyword clamps) into a tier:

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
statuses (done/aborted/error) that a replayed queue job refuses to re-run.

Bounded on purpose: per-tier turn budgets, 3 consecutive failed actions →
abort with a partial report. Recursive/heavy actions (``execute_task``,
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

from .config import Settings

log = logging.getLogger("assistant")

EXCLUDED_ACTIONS = ("execute_task", "plan_task", "trigger_run", "approve_task")

# Task ids are file names — validate before any path is built.
TASK_ID_RE = re.compile(r"^task-\d{8}-\d{6}-[0-9a-f]{6}$")

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

_PLAN_SYSTEM = """You write a short execution plan for a task a personal assistant will perform
BY ITSELF with its typed actions (web_search, reminders, routines, todos, finance/health
logging and queries, run_phase). Respond with ONLY JSON:
{"steps": ["<one concrete, action-sized step>", ...],
 "verify": "<how to check the outcome before reporting>",
 "risks": "<one line: what could go wrong>"}
3-6 steps, each something the agent can actually do — no owner-only steps."""

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
    in the owner's language, citing what you found/did; admit what you couldn't do>"}}

Rules: prefer web_search for anything needing current/external information; use reminders/
routines/todos/ledgers when the task calls for them; never invent results an action didn't
return; finish as soon as the task is genuinely done (don't pad steps). Actions with outward
effects (publishing the website, rebooting) automatically pause the task for the owner's
approval — that pause is normal, not a failure. When the plan names a verify check, do it
and address the result in your finish report."""


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


def _draft_plan(request: str, context: str, llm, tier: str) -> dict | None:
    """A short persisted plan: steps + verify + risks, with per-milestone
    status. Medium drafts single-model (cost floor); complex drafts on the
    configured `task` role — the one spot MoA quality is warranted. None when
    the model produced nothing usable (the loop then runs planless)."""
    kwargs = {} if tier == "complex" else {"mixture": False}
    try:
        out = llm.complete_json(f"## Context\n{context[:4000]}\n\n## Task\n{request}",
                                system=_PLAN_SYSTEM, max_tokens=4000, role="task",
                                **kwargs)
    except Exception:
        log.warning("plan drafting failed — running planless", exc_info=True)
        return None
    if not isinstance(out, dict) or not out.get("steps"):
        return None
    steps = [str(s)[:200] for s in out["steps"][:6]]
    return {"steps": steps, "verify": str(out.get("verify", ""))[:300],
            "risks": str(out.get("risks", ""))[:300],
            "milestones": [{"step": s, "done": False} for s in steps]}


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
    return block


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
            from .notify import send_wechat

            plan = record.get("plan") or {}
            steps = "\n".join(f"  {i}. {m['step']}" for i, m in
                              enumerate(plan.get("milestones", [])[:6], 1))
            send_wechat(settings, (
                f"⏸ [任务待批准] {record['request'][:100]}\n{reason}\n"
                + (f"计划:\n{steps}\n" if steps else "")
                + (f"待执行动作: {json.dumps(pending_action, ensure_ascii=False)[:150]}\n"
                   if pending_action else "")
                + f"批准请回复: 批准任务 {record['id']}"))
        except Exception:
            log.exception("approval notify failed")
    return record


def _load_approved(settings: Settings, task_id: str) -> dict | None:
    """Locked `queued → running` transition for an approved task. Accepts
    `queued` (fresh approval) and `running` (crash-recovery replay of a queue
    job); refuses terminal or still-awaiting records — a replayed job must
    never re-run a finished task or jump an approval."""
    from .locks import data_write_lock

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
        if record.get("status") not in ("queued", "running"):
            return None
        record["status"] = "running"
        _persist(settings, record)
    return record


def run_task(request: str, settings: Settings, llm=None, max_turns: int = 12,
             notify: bool = True, cancel_check=None,
             approved_task_id: str | None = None) -> dict:
    """Execute `request` agentically; returns the task record `{id, request,
    tier, status, steps, report, …}` (status: done | aborted | error |
    awaiting_approval). `notify` pushes the report/approval ask to WeChat — on
    by default because tasks run detached; the CLI passes False in the
    foreground. `cancel_check` (§6): optional zero-arg callable invoked at the
    top of every turn; raising from it (the job worker passes
    `CancelToken.check`) aborts between steps. `approved_task_id` re-enters a
    previously paused task: it resumes from the persisted steps with the
    stored plan/tier and `pre_approved=True` (the pending risky action runs
    first)."""
    from .actions import execute, is_risky, looks_failed, prompt_block
    from .chat.agent import build_context
    from .llm import LLM
    from . import tracing

    llm = llm or LLM(settings)
    pre_approved = False
    if approved_task_id:
        record = _load_approved(settings, approved_task_id)
        if record is None:
            return {"id": str(approved_task_id), "status": "error",
                    "report": "task not found, not approved, or already finished",
                    "steps": []}
        pre_approved = True
        record.setdefault("steps", [])
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
        record["assessment"] = _assess(record["request"], settings, llm)
    tier = record["tier"] = record["assessment"]["tier"]
    single_model = tier in ("simple", "medium")
    loop_kwargs = {"mixture": False} if single_model else {}
    turns = min(max_turns, _TURN_BUDGET.get(tier, 12))

    actions_block = "\n".join(
        line for line in prompt_block(settings).splitlines()   # mode-aware: no
        if not any(f'"{name}"' in line for name in EXCLUDED_ACTIONS))  # admin actions for tenants (§10)
    system = _RUNNER_SYSTEM.format(actions=actions_block)
    try:
        from .lessons_store import combined_prompt_block

        system += combined_prompt_block(settings)   # shared G* then personal L*
    except Exception:
        log.exception("lessons injection failed")
    context = build_context(settings)

    if tier != "simple" and not record.get("plan"):
        record["plan"] = _draft_plan(record["request"], context, llm, tier)
    if not pre_approved and tier == "complex" and record["assessment"].get("risky"):
        # fast-path pause: a plan with publishing intent never starts unapproved
        # (the per-action gate below still guards every tier)
        result = _pause_for_approval(record, settings, notify,
                                     reason="计划包含对外发布类步骤，需要你确认")
        _record_task_metrics(settings, record, time.monotonic() - start)
        return result
    _persist(settings, record)

    consecutive_failures = 0
    pending = record.pop("pending_action", None) if pre_approved else None
    if pre_approved:
        turns += 1   # the approved pending action gets its own turn even when
                     # the pause landed on the last budgeted step

    while len(record["steps"]) < turns:
        if cancel_check is not None:   # §6: per-turn cancellation checkpoint —
            cancel_check()             # outside the LLM try so the raise propagates
        if pending is not None:        # the approved risky action runs first
            move = {"thought": "(owner approved the pending action)",
                    "action": pending}
            pending = None
        else:
            transcript = "\n".join(
                f"[step {i + 1}] thought: {s['thought']}\n  action: "
                f"{json.dumps(s.get('action'), ensure_ascii=False)}\n  result: {s.get('outcome')}"
                for i, s in enumerate(record["steps"])) or "(no steps yet)"
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
        milestone = move.get("milestone_done")
        if record.get("plan") and isinstance(milestone, int) \
                and 1 <= milestone <= len(record["plan"]["milestones"]):
            record["plan"]["milestones"][milestone - 1]["done"] = True
        if move.get("finish") is not None:
            record["status"] = "done"
            record["report"] = str(move.get("finish") or "").strip() or "(empty report)"
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
        elif is_risky(action["type"], action) and not pre_approved:
            # THE approval boundary: an outward/irreversible action pauses the
            # task at every tier — nothing executes until the owner approves.
            record["steps"].append({**step, "outcome": "(paused — owner approval required)"})
            result = _pause_for_approval(
                record, settings, notify,
                reason=f"下一步 {action['type']} 有对外影响，需要你确认",
                pending_action=action)
            _record_task_metrics(settings, record, time.monotonic() - start)
            return result
        else:
            outcomes = execute([action], settings)
            step["outcome"] = outcomes[0] if outcomes else "(no outcome)"
            consecutive_failures = (consecutive_failures + 1
                                    if looks_failed(step["outcome"]) else 0)
        record["steps"].append(step)
        _persist(settings, record)
        if consecutive_failures >= 3:
            record["status"] = "aborted"
            record["report"] = ("Stopped after 3 consecutive failed steps. "
                                "Progress so far:\n"
                                + "\n".join(str(s.get("outcome", "")) for s in
                                            record["steps"][-4:]))
            break
    else:
        record["status"] = "aborted"
        record["report"] = f"Stopped at the {turns}-step budget without finishing."

    _persist(settings, record)
    _record_task_metrics(settings, record, time.monotonic() - start)
    if notify:
        try:
            from .notify import send_wechat

            status = "✅" if record["status"] == "done" else "⚠️"
            send_wechat(settings, f"{status} [任务] {record['request'][:80]}\n"
                                  f"{record['report'][:1600]}")
        except Exception:
            log.exception("task result notify failed")
    return record


def _record_task_metrics(settings: Settings, record: dict, duration_s: float) -> None:
    """One numeric `task` metrics row per run (record_metrics drops
    non-floats, so statuses are one-hot and the tier is its index)."""
    try:
        from .events_store import EventsStore

        events = EventsStore(settings.events_db)
        events.record_metrics(record["id"], "task", {
            "duration_s": round(duration_s, 2),
            "steps": len(record.get("steps") or []),
            "tier": TIERS.index(record.get("tier", "medium")),
            "done": int(record.get("status") == "done"),
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
