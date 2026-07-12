"""The action registry and its dispatch: the `ACTIONS` table (one entry per
handler, the single source of truth), the chat-prompt generator, the LLM-action
executor, and the direct CLI/HTTP invoker.

`execute` honors only `llm`-flagged actions (what the chat model may emit) and
never lets one bad action eat the reply; `run_action` invokes any registry entry
directly.
"""

from .base import Action, validate
from .handlers import (
    _add_todo,
    _cancel_reminder,
    _cancel_routine,
    _create_routine,
    _done_reading,
    _done_todo,
    _finance_summary,
    _list_reading,
    _list_reminders,
    _list_routines,
    _list_todos,
    _list_transactions,
    _log_transaction,
    _plan_task,
    _run_phase,
    _run_status,
    _set_reminder,
    _show_profile,
    _trigger_run,
    _unrelated_reading,
    _void_transaction,
    _web_search,
)
from ..config import Settings

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
        name="unrelated_reading",
        description="negative feedback: mark a surfaced reading as unrelated so "
                    "future digests avoid similar topics",
        handler=_unrelated_reading,
        params={"id": {"required": True, "desc": "reading id, e.g. r5"}},
        llm=True,
        prompt_example='{"type": "unrelated_reading", "id": "r5"}   # owner says this '
                       'should not have been surfaced',
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
        name="run_phase",
        description="run one standalone pipeline phase now",
        handler=_run_phase,
        params={"phase": {"required": True,
                          "desc": "research|website|todos|resume|curate|consolidate|all"}},
        llm=True,
        prompt_example='{"type": "run_phase", "phase": "research"}   # research|website|todos'
                       '|resume|curate|consolidate, or "all" for the full daily run',
        slash="run",
    ),
    Action(
        name="web_search",
        description="search the internet and answer from the results",
        handler=_web_search,
        params={"query": {"required": True, "desc": "the search query"}},
        llm=True,
        prompt_example='{"type": "web_search", "query": "<what to look up>"}   # for '
                       'questions needing current/external information',
        slash="search",
    ),
    Action(
        name="set_reminder",
        description="schedule a one-shot WeChat reminder the agent sends by itself",
        handler=_set_reminder,
        params={"message": {"required": True, "desc": "what to remind about"},
                "when": {"required": True,
                         "desc": "'+30m' / '+2h' / '+1d', 'HH:MM', or 'YYYY-MM-DD HH:MM'"}},
        llm=True,
        prompt_example='{"type": "set_reminder", "message": "...", "when": "+2h"}   # '
                       'agent pings WeChat at the time, unprompted',
        slash="remind",
    ),
    Action(
        name="list_reminders",
        description="list pending reminders (cancel: set_reminder is one-shot)",
        handler=_list_reminders,
        slash="remind",
    ),
    Action(
        name="cancel_reminder",
        description="cancel a pending reminder by id",
        handler=_cancel_reminder,
        params={"id": {"required": True, "desc": "reminder id, e.g. m2"}},
        llm=True,
        prompt_example='{"type": "cancel_reminder", "id": "m2"}',
        slash="remind",
    ),
    Action(
        name="create_routine",
        description="recurring work: at a time on chosen days, optionally gated on a "
                    "real-world condition, the agent runs a task and messages WeChat",
        handler=_create_routine,
        params={"task": {"required": True, "desc": "what to do/say each time"},
                "time": {"required": True, "desc": "HH:MM"},
                "days": {"required": False,
                         "desc": "daily|workdays|weekends|'mon,wed,fri'|"
                                 "'monthly:<1-31>' (day of month)|'yearly:<MM-DD>' "
                                 "(default daily)"},
                "condition": {"required": False,
                              "desc": "free-text gate checked at fire time via web "
                                      "search, e.g. 'there is a weather alert in Shenzhen'"}},
        llm=True,
        prompt_example='{"type": "create_routine", "task": "...", "time": "08:30", '
                       '"days": "workdays", "condition": "<optional real-world gate>"}'
                       '   # days also: "monthly:1" (每月1号), "yearly:03-15" (每年3月15日)',
        slash="routine",
    ),
    Action(
        name="list_routines",
        description="list active routines",
        handler=_list_routines,
        slash="routine",
    ),
    Action(
        name="cancel_routine",
        description="cancel a routine by id",
        handler=_cancel_routine,
        params={"id": {"required": True, "desc": "routine id, e.g. rt2"}},
        llm=True,
        prompt_example='{"type": "cancel_routine", "id": "rt2"}',
        slash="routine",
    ),
    Action(
        name="plan_task",
        description="plan a novel multi-step task (booking, arranging, researching) "
                    "and track it as a todo",
        handler=_plan_task,
        params={"request": {"required": True, "desc": "the owner's task, one sentence"}},
        llm=True,
        prompt_example='{"type": "plan_task", "request": "<the task in one sentence>"}'
                       '   # for novel multi-step asks: bookings, arranging, research',
        slash="plan",
    ),
    Action(
        name="show_profile",
        description="summary of the owner profile",
        handler=_show_profile,
    ),
    Action(
        name="log_transaction",
        description="record an income or expense in the finance ledger",
        handler=_log_transaction,
        params={"kind": {"required": True, "desc": "income | expense"},
                "amount": {"required": True, "desc": "positive number"},
                "category": {"required": False,
                             "desc": "food/transport/housing/utilities/entertainment/"
                                     "shopping/health/education/travel/salary/bonus/"
                                     "investment/transfer/other"},
                "note": {"required": False,
                         "desc": "context: merchant/what it was for — used for dedup"},
                "date": {"required": False, "desc": "YYYY-MM-DD, default today"},
                "time": {"required": False,
                         "desc": "HH:MM if known (e.g. from a receipt) — used for dedup"},
                "currency": {"required": False, "desc": "e.g. CNY/HKD, default configured"}},
        llm=True,
        prompt_example='{"type": "log_transaction", "kind": "expense", "amount": 45, '
                       '"category": "food", "note": "午饭", "time": "12:30"}   '
                       '# kind: income|expense; note=context, time from receipt if shown; '
                       'duplicates (same kind+amount+currency+date+time+note) are rejected',
        slash="fin",
    ),
    Action(
        name="void_transaction",
        description="void a mistaken ledger record by id (never deletes)",
        handler=_void_transaction,
        params={"id": {"required": True, "desc": "record id, e.g. f3"}},
        llm=True,
        prompt_example='{"type": "void_transaction", "id": "f3"}',
        slash="fin",
    ),
    Action(
        name="list_transactions",
        description="list finance records, optionally one YYYY-MM month",
        handler=_list_transactions,
        params={"month": {"required": False, "desc": "YYYY-MM"}},
        slash="fin",
    ),
    Action(
        name="finance_summary",
        description="deterministic monthly finance totals (income, spend, net, "
                    "savings rate, top categories)",
        handler=_finance_summary,
        params={"month": {"required": False, "desc": "YYYY-MM, default current"}},
        llm=True,
        prompt_example='{"type": "finance_summary", "month": "2026-06"}   '
                       '# month optional',
        slash="fin",
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
