"""Agentic executor for novel multi-step tasks (the copilot pattern).

`plan_task` produces a plan for the owner; this module *does* the task: a
bounded ReAct loop where the LLM, given the owner's request plus the full
chat context, chooses one registry action per turn (web_search, reminders,
todos, finance/health logging, run_phase, …), sees the real outcome — the
same `looks_failed` review the chat loop uses — adapts, and finishes with a
report. Every turn is persisted to `DATA_DIR/tasks/<id>.json` so a crash
leaves an audit trail, and the final report is pushed to the owner's WeChat
(`notify.send_wechat`) since tasks usually run detached in the background.

Bounded on purpose: `max_turns` LLM turns, 3 consecutive failed actions →
abort with a partial report. Recursive/heavy actions (`execute_task` itself,
`plan_task`, `trigger_run`) are excluded from the runner's action set.
"""

import json
import logging
from datetime import datetime

from .config import Settings

log = logging.getLogger("assistant")

EXCLUDED_ACTIONS = ("execute_task", "plan_task", "trigger_run")

_RUNNER_SYSTEM = """You are executing a task for your owner, step by step, on your own.
Work from the context below; use actions to gather information and to act. One step per
response. Review each action's result before deciding the next step — if an action failed,
analyze the message and try a corrected or different approach rather than repeating it.

Available actions (one per response):
{actions}

Respond with ONLY JSON, one of:
  {{"thought": "<why this step>", "action": {{"type": "<name>", ...params}}}}
  {{"thought": "<wrap-up>", "finish": "<final report for the owner — concise, concrete,
    in the owner's language, citing what you found/did; admit what you couldn't do>"}}

Rules: prefer web_search for anything needing current/external information; use reminders/
routines/todos/ledgers when the task calls for them; never invent results an action didn't
return; finish as soon as the task is genuinely done (don't pad steps)."""


def run_task(request: str, settings: Settings, llm=None, max_turns: int = 12,
             notify: bool = True) -> dict:
    """Execute `request` agentically; returns the task record
    `{id, request, status, steps, report}` (status: done | aborted | error).
    `notify` pushes the report to WeChat — on by default because tasks run
    detached; the CLI passes False when running in the foreground."""
    from .actions import execute, looks_failed, prompt_block
    from .chat.agent import build_context
    from .llm import LLM

    llm = llm or LLM(settings)
    task_id = datetime.now().strftime("task-%Y%m%d-%H%M%S")
    record: dict = {"id": task_id, "request": str(request)[:500],
                    "started": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "status": "running", "steps": [], "report": ""}
    actions_block = "\n".join(
        line for line in prompt_block().splitlines()
        if not any(f'"{name}"' in line for name in EXCLUDED_ACTIONS))
    system = _RUNNER_SYSTEM.format(actions=actions_block)
    try:
        from .lessons_store import LessonsStore

        system += LessonsStore(settings.profile_dir).prompt_block()
    except Exception:
        log.exception("lessons injection failed")
    context = build_context(settings)
    consecutive_failures = 0

    for _ in range(max_turns):
        transcript = "\n".join(
            f"[step {i + 1}] thought: {s['thought']}\n  action: "
            f"{json.dumps(s.get('action'), ensure_ascii=False)}\n  result: {s.get('outcome')}"
            for i, s in enumerate(record["steps"])) or "(no steps yet)"
        prompt = (f"## Context\n{context}\n\n## Task from the owner\n{record['request']}"
                  f"\n\n## Steps so far\n{transcript}\n\n## Next\nDecide the next single "
                  "step, or finish with the report.")
        try:
            move = llm.complete_json(prompt, system=system, max_tokens=2500)
        except Exception as exc:
            record["status"], record["report"] = "error", f"LLM failed mid-task: {exc}"
            break
        if not isinstance(move, dict):
            record["status"], record["report"] = "error", "unparseable step from the model"
            break
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
                                "Progress so far:\n" + transcript[-800:])
            break
    else:
        record["status"] = "aborted"
        record["report"] = f"Stopped at the {max_turns}-step budget without finishing."

    _persist(settings, record)
    if notify:
        try:
            from .notify import send_wechat

            status = "✅" if record["status"] == "done" else "⚠️"
            send_wechat(settings, f"{status} [任务] {record['request'][:80]}\n"
                                  f"{record['report'][:1600]}")
        except Exception:
            log.exception("task result notify failed")
    return record


def _persist(settings: Settings, record: dict) -> None:
    """Write the task record as JSON under DATA_DIR/tasks/ (best-effort)."""
    try:
        tasks_dir = settings.data_dir / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        (tasks_dir / f"{record['id']}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2))
    except Exception:
        log.exception("task persist failed")
