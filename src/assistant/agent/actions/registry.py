"""The action registry and its dispatch: the `ACTIONS` table (one entry per
handler, the single source of truth), the chat-prompt generator, the LLM-action
executor, and the direct CLI/HTTP invoker.

`execute` honors only `llm`-flagged actions (what the chat model may emit) and
never lets one bad action eat the reply; `run_action` invokes any registry entry
directly.
"""

from assistant.agent.actions.base import Action, ActionResult, validate
from assistant.agent.actions.handlers import _acknowledge_failure, _add_health_need, _add_todo, _approve_task, _build_personal_website, _connect_github, _create_workflow, _retire_workflow, _run_workflow, _show_workflow, _update_workflow, _cancel_reminder, _cancel_routine, _create_routine, _done_health_need, _done_reading, _done_todo, _execute_task, _finance_summary, _health_summary, _learn_preference, _list_preferences, _list_reading, _list_reminders, _list_routines, _list_todos, _list_transactions, _log_exercise, _log_meal, _log_transaction, _log_weight, _plan_task, _query_health, _query_transactions, _reboot, _recategorize_transaction, _run_phase, _retire_preference, _run_status, _search_personal_data, _self_evolve, _set_health_profile, _set_reminder, _show_profile, _trigger_run, _unrelated_reading, _void_transaction, _web_research, _web_search
from assistant.platform.config import Settings

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
        name="acknowledge_failure",
        description="clear one delivery-failure notice (知道了 <id>)",
        handler=_acknowledge_failure,
        params={"id": {"required": True,
                       "desc": "failure id, e.g. dfrem m3 / dfe…-… / dfo…"}},
        llm=True,
        prompt_example='{"type": "acknowledge_failure", "id": "dfremm3"}',
    ),
    Action(
        name="list_todos",
        description="list open todos",
        handler=_list_todos,
        llm=True,
        prompt_example='{"type": "list_todos"}',
        slash="todo",
        read_only=True,
    ),
    Action(
        name="search_personal_data",
        description="search retained conversations and durable personal records "
                    "(all todo/reminder statuses, tasks, observations) with evidence ids",
        handler=_search_personal_data,
        params={"query": {"required": True, "desc": "literal names/terms to find"},
                "sources": {"required": False,
                            "desc": "comma-separated sessions,todos,reminders,tasks,observations"},
                "limit": {"required": False, "desc": "1-30, default 12"}},
        llm=True,
        prompt_example='{"type": "search_personal_data", "query": "Project Phoenix", '
                       '"sources": "sessions,todos,reminders"}   # older chats or '
                       'anything the owner previously recorded',
        read_only=True,
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
        # website publishes to the public site; the other phases are internal
        # (resume's own push already sits behind `approve-resume`)
        risky=lambda p: str(p.get("phase", "")).strip().lower() == "website",
    ),
    Action(
        name="connect_github",
        description="store the owner's GitHub token (pasted in chat) after "
                    "validating it — prerequisite for building their website",
        handler=_connect_github,
        params={"token": {"required": False,
                          "desc": "leave empty — the real token is read from the owner's "
                                  "message by code; do NOT copy or abbreviate it yourself"}},
        llm=True,
        prompt_example='{"type": "connect_github"}   # emit this whenever the owner pastes '
                       'a GitHub token or asks to connect GitHub. Do NOT put the token in '
                       'the JSON — code extracts it from their message; the raw token is '
                       'scrubbed from history automatically',
        slash="connect",
    ),
    Action(
        name="build_personal_website",
        description="build & publish the owner's personal website (<login>.github.io) "
                    "from their GitHub activity — needs connect_github first",
        handler=_build_personal_website,
        params={"confirm": {"required": False,
                            "desc": "truthy / 'overwrite' — replace an existing non-empty site"}},
        llm=True,
        prompt_example='{"type": "build_personal_website"}   # owner said "build/make my '
                       'website from my github"; add "confirm": "overwrite" only when they '
                       'explicitly agree to replace an existing site',
        slash="buildsite",
        # publishes to a public site — a background task must never do this
        # autonomously; only a live owner request in chat may.
        risky=True,
    ),
    Action(
        name="reboot",
        description="restart the assistant daemon so it reloads code (after an "
                    "update / when it's misbehaving)",
        handler=_reboot,
        llm=True,
        prompt_example='{"type": "reboot"}   # owner says 重启/restart/重新启动 — '
                       'reload the agent (comes back in a few seconds)',
        slash="reboot",
        risky=True,   # a task must never bounce the daemon on its own
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
        read_only=True,
    ),
    Action(
        name="web_research",
        description="batch up to 8 independent web queries in parallel and return exact "
                    "source snippets/URLs with W evidence ids; no per-query LLM synthesis",
        handler=_web_research,
        params={"query": {"required": False, "desc": "one query"},
                "queries": {"required": False, "desc": "list of independent queries"}},
        llm=True,
        prompt_example='{"type": "web_research", "queries": ["official query 1", '
                       '"official query 2"]}   # batch independent current/external research; '
                       'cite returned [W…] ids and never infer a missing year',
        slash="search",
        read_only=True,
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
        llm=True,
        prompt_example='{"type": "list_reminders"}',
        slash="remind",
        read_only=True,
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
        params={"task": {"required": False,
                         "desc": "what to do/say each time (optional when a "
                                 "workflow is bound)"},
                "time": {"required": True, "desc": "HH:MM"},
                "days": {"required": False,
                         "desc": "daily|workdays|weekends|'mon,wed,fri'|"
                                 "'monthly:<1-31>' (day of month)|'yearly:<MM-DD>' "
                                 "(default daily)"},
                "condition": {"required": False,
                              "desc": "free-text gate checked at fire time via web "
                                      "search, e.g. 'there is a weather alert in Shenzhen'"},
                "workflow": {"required": False,
                             "desc": "saved workflow id (wf3) to run each time — "
                                     "deterministic, preferred over describing it in task"}},
        llm=True,
        prompt_example='{"type": "create_routine", "task": "...", "time": "08:30", '
                       '"days": "workdays", "condition": "<optional real-world gate>", '
                       '"workflow": "<optional wf-id to run each time>"}'
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
        name="learn_preference",
        description="remember a durable behavior rule from the owner's feedback "
                    "(how the agent should act from now on)",
        handler=_learn_preference,
        params={"rule": {"required": True,
                         "desc": "one imperative sentence, e.g. '记账默认用港币'"},
                "why": {"required": False, "desc": "what prompted it"}},
        llm=True,
        prompt_example='{"type": "learn_preference", "rule": "<the behavior rule>"}'
                       '   # when the owner gives DURABLE feedback: 以后…/别再…/记住要…/'
                       'corrections of how you behave — not one-off reminders',
        slash="learn",
    ),
    Action(
        name="retire_preference",
        description="retire a learned behavior rule by id",
        handler=_retire_preference,
        params={"id": {"required": True, "desc": "lesson id, e.g. L3"}},
        llm=True,
        prompt_example='{"type": "retire_preference", "id": "L3"}   # 忘掉那条规则',
        slash="learn",
    ),
    Action(
        name="list_preferences",
        description="list the learned behavior rules",
        handler=_list_preferences,
        slash="learn",
    ),
    Action(
        name="self_evolve",
        description="analyze recent chats/tasks now and distill new behavior lessons",
        handler=_self_evolve,
        params={},
        llm=True,
        prompt_example='{"type": "self_evolve"}   # owner asks you to reflect/improve '
                       'yourself from recent conversations',
        slash="learn",
    ),
    Action(
        name="execute_task",
        description="agentically EXECUTE a novel multi-step task in the "
                    "background (search, act, adapt, report to WeChat)",
        handler=_execute_task,
        params={"request": {"required": True, "desc": "the owner's task, one sentence"}},
        llm=True,
        prompt_example='{"type": "execute_task", "request": "<the task in one sentence>"}'
                       '   # for DOABLE novel tasks: research X and summarize, find and '
                       'compare, gather info then remind — the agent does it and reports',
        slash="task",
    ),
    Action(
        name="approve_task",
        description="approve an awaiting background task (paused on a risky "
                    "step or a risky plan) so it executes",
        handler=_approve_task,
        params={"id": {"required": True, "desc": "task id, e.g. task-20260717-153000-a1b2c3"}},
        llm=True,
        prompt_example='{"type": "approve_task", "id": "task-..."}   # owner says '
                       '批准任务 <id> / approve task <id> — release an awaiting task',
        slash="task",
    ),
    Action(
        name="create_workflow",
        description="save a reusable named workflow: an owner-approved "
                    "procedure as 1-6 concrete text steps",
        handler=_create_workflow,
        params={"name": {"required": False, "desc": "short unique name"},
                "description": {"required": True, "desc": "what the workflow achieves"},
                "steps": {"required": True,
                          "desc": "list of 1-6 short concrete step strings"},
                "verify": {"required": False,
                           "desc": "how to check the outcome before reporting"}},
        llm=True,
        prompt_example='{"type": "create_workflow", "name": "周报", "description": '
                       '"...", "steps": ["step 1", "step 2"], "verify": "..."}   '
                       '# owner wants to SAVE a procedure for reuse (存成工作流/'
                       '以后照这个流程) — write the steps yourself from the conversation',
    ),
    Action(
        name="run_workflow",
        description="execute a saved workflow now (its steps become the task "
                    "plan; outward steps still need approval)",
        handler=_run_workflow,
        params={"id": {"required": True, "desc": "workflow id, e.g. wf3"}},
        llm=True,
        prompt_example='{"type": "run_workflow", "id": "wf3"}   # owner asks to '
                       'run a saved workflow (ids are in the context list)',
    ),
    Action(
        name="show_workflow",
        description="show a workflow's full steps, verify check, and run stats",
        handler=_show_workflow,
        params={"id": {"required": True, "desc": "workflow id, e.g. wf3"}},
        llm=True,
        prompt_example='{"type": "show_workflow", "id": "wf3"}',
    ),
    Action(
        name="update_workflow",
        description="edit a saved workflow's name/description/steps/verify",
        handler=_update_workflow,
        params={"id": {"required": True, "desc": "workflow id, e.g. wf3"},
                "name": {"required": False, "desc": "new name"},
                "description": {"required": False, "desc": "new description"},
                "steps": {"required": False, "desc": "full replacement step list"},
                "verify": {"required": False, "desc": "new verify check"}},
        llm=True,
        prompt_example='{"type": "update_workflow", "id": "wf3", "steps": ["..."]}'
                       '   # owner corrects a saved workflow',
    ),
    Action(
        name="retire_workflow",
        description="retire a workflow (kept in history; bound routines are "
                    "cancelled)",
        handler=_retire_workflow,
        params={"id": {"required": True, "desc": "workflow id, e.g. wf3"}},
        llm=True,
        prompt_example='{"type": "retire_workflow", "id": "wf3"}   # 不要这个'
                       '工作流了',
    ),
    Action(
        name="show_profile",
        description="summary of the owner profile",
        handler=_show_profile,
    ),
    Action(
        name="log_meal",
        description="record a meal in the health log (estimate nutrition when "
                    "reading a food photo or label)",
        handler=_log_meal,
        params={"description": {"required": True, "desc": "what was eaten, short"},
                "calories_kcal": {"required": False, "desc": "estimate ok"},
                "protein_g": {"required": False, "desc": "estimate ok"},
                "carbs_g": {"required": False, "desc": "estimate ok"},
                "fat_g": {"required": False, "desc": "estimate ok"},
                "date": {"required": False, "desc": "event day YYYY-MM-DD; resolve "
                        "今天/昨天/前天/N天前 via the [temporal anchor] and pass it — "
                        "omit ONLY when the day is genuinely today"},
                "time": {"required": False, "desc": "HH:MM when known"},
                "note": {"required": False, "desc": "e.g. ingredients seen on a label"}},
        llm=True,
        prompt_example='{"type": "log_meal", "description": "牛肉面", '
                       '"calories_kcal": 550, "protein_g": 25, "date": "2026-07-19", '
                       '"time": "12:30"}   # "昨天中午的牛肉面": set date to the event day '
                       '(resolve 昨天 via the [temporal anchor]); omit date only if today',
        slash="health",
    ),
    Action(
        name="log_exercise",
        description="record an exercise session in the health log",
        handler=_log_exercise,
        params={"activity": {"required": True, "desc": "e.g. running/swim/gym; "
                             "put sets/reps in note for strength work"},
                "duration_min": {"required": False, "desc": "minutes if timed — "
                                 "OMIT for set/rep work rather than inventing one"},
                "date": {"required": False, "desc": "event day YYYY-MM-DD; resolve "
                        "今天/昨天/前天/N天前 via the [temporal anchor] and pass it — "
                        "omit ONLY when the day is genuinely today"},
                "time": {"required": False, "desc": "HH:MM when known"},
                "note": {"required": False, "desc": "sets/reps, distance, intensity"}},
        llm=True,
        prompt_example='{"type": "log_exercise", "activity": "跑步", "duration_min": 30, '
                       '"date": "2026-07-19"}   # date = the event day (resolve 昨天/前天 via '
                       'the anchor); duration_min optional — omit for set/rep work',
        slash="health",
    ),
    Action(
        name="log_weight",
        description="record a body-weight measurement (kg) in the health log",
        handler=_log_weight,
        params={"weight_kg": {"required": True, "desc": "kilograms"},
                "date": {"required": False, "desc": "event day YYYY-MM-DD; resolve "
                        "今天/昨天/前天/N天前 via the [temporal anchor] and pass it — "
                        "omit ONLY when the day is genuinely today"},
                "time": {"required": False, "desc": "HH:MM when known"}},
        llm=True,
        prompt_example='{"type": "log_weight", "weight_kg": 70.5, "date": "2026-07-19"}   '
                       '# date = the day measured (omit only if today); also from body-scale photos',
        slash="health",
    ),
    Action(
        name="set_health_profile",
        description="update static body facts: sex, birth_year, height_cm",
        handler=_set_health_profile,
        params={"sex": {"required": False, "desc": "male|female"},
                "birth_year": {"required": False, "desc": "e.g. 1999"},
                "height_cm": {"required": False, "desc": "e.g. 178"}},
        llm=True,
        prompt_example='{"type": "set_health_profile", "height_cm": 178}',
        slash="health",
    ),
    Action(
        name="add_health_need",
        description="track a nutrient/ingredient the owner wants covered",
        handler=_add_health_need,
        params={"item": {"required": True, "desc": "e.g. 维生素D / protein"},
                "why": {"required": False, "desc": "one short reason"}},
        llm=True,
        prompt_example='{"type": "add_health_need", "item": "维生素D", "why": "久坐室内"}',
        slash="health",
    ),
    Action(
        name="done_health_need",
        description="mark a tracked nutrient/ingredient need as covered",
        handler=_done_health_need,
        params={"id": {"required": True, "desc": "need id, e.g. n2"}},
        llm=True,
        prompt_example='{"type": "done_health_need", "id": "n2"}',
        slash="health",
    ),
    Action(
        name="health_summary",
        description="deterministic health picture: body facts/BMI, weight trend, "
                    "exercise totals, daily calorie/protein averages, open needs",
        handler=_health_summary,
        params={"days": {"required": False, "desc": "window, default 7 (max 90)"}},
        llm=True,
        prompt_example='{"type": "health_summary", "days": 7}   # days optional',
        slash="health",
    ),
    Action(
        name="query_health",
        description="retrieve health records for a specific day, date range, "
                    "kind, or food/ingredient text (with totals) — look up any "
                    "day/period not shown in the context",
        handler=_query_health,
        params={"date": {"required": False, "desc": "YYYY-MM-DD single day"},
                "start": {"required": False, "desc": "YYYY-MM-DD range start"},
                "end": {"required": False, "desc": "YYYY-MM-DD range end"},
                "kind": {"required": False, "desc": "meal|exercise|weight"},
                "contains": {"required": False, "desc": "text to match in a meal/note"}},
        llm=True,
        prompt_example='{"type": "query_health", "date": "2026-07-13"}   # or '
                       'start/end range, kind, contains — look up meals/exercise/'
                       'weight for ANY day when the ## Health block does not show it',
        slash="health",
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
                                     "investment/transfer/other — 物业费/房租/mortgage → "
                                     "housing; 水电燃气 → utilities"},
                "note": {"required": False,
                         "desc": "context: merchant/what it was for — used for dedup"},
                "date": {"required": False, "desc": "event day YYYY-MM-DD; resolve "
                        "今天/昨天/前天/N天前 via the [temporal anchor] and pass it — "
                        "omit ONLY when the day is genuinely today"},
                "time": {"required": False,
                         "desc": "HH:MM — ALWAYS pass when the receipt/message shows "
                                 "one; distinguishes same-priced purchases (auto-filled "
                                 "with the logging time otherwise)"},
                "currency": {"required": False, "desc": "e.g. CNY/HKD, default configured"}},
        llm=True,
        prompt_example='{"type": "log_transaction", "kind": "expense", "amount": 45, '
                       '"category": "food", "note": "午饭", "date": "2026-07-19", "time": "12:30"}   '
                       '# "昨天午饭花了45": set date to the event day (resolve via the anchor), '
                       'time from receipt if shown; duplicates are rejected',
        slash="fin",
    ),
    Action(
        name="recategorize_transaction",
        description="move a ledger record to another category (owner corrections)",
        handler=_recategorize_transaction,
        params={"id": {"required": True, "desc": "record id, e.g. f37"},
                "category": {"required": True, "desc": "target category"}},
        llm=True,
        prompt_example='{"type": "recategorize_transaction", "id": "f37", '
                       '"category": "housing"}',
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
    Action(
        name="query_transactions",
        description="retrieve finance records for a specific day, date range, "
                    "category, kind, or note text (with income/expense/net "
                    "totals) — arbitrary lookups beyond the current month",
        handler=_query_transactions,
        params={"date": {"required": False, "desc": "YYYY-MM-DD single day"},
                "start": {"required": False, "desc": "YYYY-MM-DD range start"},
                "end": {"required": False, "desc": "YYYY-MM-DD range end"},
                "category": {"required": False, "desc": "food/housing/... category"},
                "kind": {"required": False, "desc": "income|expense"},
                "contains": {"required": False, "desc": "text to match in the note"}},
        llm=True,
        prompt_example='{"type": "query_transactions", "start": "2026-05-01", '
                       '"end": "2026-05-31", "category": "food"}   # any period/'
                       'category/merchant when the ## Finance block does not cover it',
        slash="fin",
    ),
]}


# Actions whose outcome is retrieved data to answer FROM (the chat loop runs a
# compose pass feeding the result back), not a mutation to confirm with a "✔".
RETRIEVAL_ACTIONS = frozenset({"query_health", "query_transactions",
                               "search_personal_data", "web_research"})


def is_risky(name: str, params: dict) -> bool:
    """Whether executing `name` with `params` has outward/irreversible effects
    an autonomous task must not perform unapproved (the `Action.risky`
    metadata — the safety boundary the task runner gates on)."""
    action = ACTIONS.get(name)
    if action is None:
        return False
    if callable(action.risky):
        try:
            return bool(action.risky(params or {}))
        except Exception:
            return True   # a broken predicate must fail safe, not open
    return bool(action.risky)

# Shared/admin actions affect the WHOLE deployment, so a tenant must never invoke
# them: `reboot` restarts the daemon and disrupts every user (§10). In
# multi_tenant these are refused on both the chat-action path and direct
# /actions/* dispatch; they live behind `assistant admin …` instead. single_user
# (one owner) keeps `reboot` as a normal action.
SHARED_ADMIN_ACTIONS = frozenset({"reboot"})


def _tenant_forbidden(name: str, settings: Settings) -> bool:
    """Whether `name` is a shared/admin action a tenant may not run in this mode."""
    return settings.deployment_mode == "multi_tenant" and name in SHARED_ADMIN_ACTIONS


def prompt_block(settings: Settings | None = None,
                 include: set[str] | frozenset[str] | None = None) -> str:
    """The chat system prompt's action list, generated from the registry.

    With `settings`, shared/admin actions a tenant may not run are omitted
    (multi_tenant, §10) — the prompt must never advertise what dispatch would
    refuse, or the model wastes a repair round emitting it. Without `settings`
    (legacy callers) the full `llm` set is listed, matching single_user."""
    acts = [a for a in ACTIONS.values() if a.llm]
    if settings is not None:
        acts = [a for a in acts if not _tenant_forbidden(a.name, settings)]
    if include is not None:
        acts = [a for a in acts if a.name in include]
    return "\n".join(f"  {a.prompt_example}" for a in acts)


import contextvars

# PA-Mix seam (doc/BENCHMARKS.md §2.6): when set, `execute` delegates to the
# override instead of running handlers. ContextVar-scoped, so a bench run in
# one context can never leak a sandbox into normal operation — and because
# handle_turn's repair rounds and the task runner all call THIS `execute`,
# the override covers them by construction. Defaults to None: production
# behavior is byte-identical when unset.
_executor_override: contextvars.ContextVar = contextvars.ContextVar(
    "actions_executor_override", default=None)


def _result(raw, *, fallback_error: str = "") -> ActionResult:
    """Normalize a new structured or legacy string handler result."""
    if isinstance(raw, ActionResult):
        return raw
    text = str(raw)
    failed = looks_failed(text)
    return ActionResult(ok=not failed, text=text,
                        error=(fallback_error or text) if failed else "")


def execute_results(actions: list, settings: Settings,
                    max_actions: int = 5) -> list[ActionResult]:
    """Structured executor used by autonomous tasks.

    Existing chat callers use :func:`execute`, which projects these results
    back to their legacy strings.  Tool spans live here so task traces finally
    distinguish model time from retrieval/mutation time.
    """
    override = _executor_override.get()
    if override is not None:
        return [_result(item) for item in override(actions, settings, max_actions)]
    import logging
    from contextlib import nullcontext

    from assistant.platform.locks import user_write_lock
    from assistant.platform import tracing

    log = logging.getLogger("assistant")
    results: list[ActionResult] = []
    requested = [raw for raw in (actions or [])[:max_actions] if isinstance(raw, dict)]
    # Preserve the old batch-atomicity rule whenever any mutation is present.
    # A purely read-only batch must not monopolize the user's write lock while
    # waiting on web/network retrieval.
    resolved = [ACTIONS.get(raw.get("type")) for raw in requested if raw.get("type")]
    read_only_batch = bool(resolved) and all(a is not None and a.read_only for a in resolved)
    lock = nullcontext() if read_only_batch else user_write_lock(settings)
    # Serialize this user's writes for the whole batch (chat action / routine /
    # task): the stores load→mutate→git-commit with no lock of their own, and the
    # daemon is multi-threaded, so concurrent turns would race the YAML/git repo.
    # Per-user lock — other users proceed in parallel; reentrant so it's safe if
    # a handler itself takes the lock (locks.py, DESIGN §8).
    with lock:
        for raw in requested:
            kind = raw.get("type")
            if not kind:
                continue
            action = ACTIONS.get(kind)
            if action is None or not action.llm:
                text = f"unknown action {kind!r} ignored"
                results.append(ActionResult(False, text, error=text))
                continue
            if _tenant_forbidden(kind, settings):
                text = f"action {kind!r} is admin-only (use `assistant admin`)"
                results.append(ActionResult(False, text, error=text))
                continue
            error = validate(action, raw)
            if error:
                results.append(ActionResult(False, error, error=error))
                continue
            try:
                with tracing.span("tool", tool=kind, read_only=action.read_only) as span:
                    try:
                        result = _result(action.handler(settings, raw))
                    except Exception as exc:
                        span.set(ok=False, error=type(exc).__name__)
                        raise
                    span.set(ok=result.ok, evidence=len(result.provenance),
                             confidence=result.confidence)
                    if isinstance(result.data, dict):
                        rows = (result.data.get("hits") or result.data.get("sources")
                                or result.data.get("queries"))
                        if isinstance(rows, list):
                            span.set(result_count=len(rows))
                results.append(result)
            except Exception as exc:  # one bad action must not eat the reply
                log.exception("chat action %s failed", kind)
                text = f"action {kind} failed: {exc}"
                results.append(ActionResult(False, text, error=str(exc)))
    return results


def execute(actions: list, settings: Settings, max_actions: int = 5) -> list[str]:
    """Backward-compatible text projection of :func:`execute_results`."""
    return [result.text for result in execute_results(actions, settings, max_actions)]


def run_action(name: str, params: dict, settings: Settings) -> str:
    """Direct invocation (CLI / HTTP / slash commands) — any registry entry.

    In `multi_tenant`, shared/admin actions (e.g. `reboot`) are refused here too,
    so a tenant can't reach one via `/actions/<name>` (§10)."""
    action = ACTIONS.get(name)
    if action is None:
        raise KeyError(name)
    if _tenant_forbidden(name, settings):
        raise ValueError(f"action {name!r} is admin-only in multi_tenant mode")
    error = validate(action, params)
    if error:
        raise ValueError(error)
    result = action.handler(settings, params)
    return result.text if isinstance(result, ActionResult) else result


# Failure markers in handler outcomes — the chat agent's review loop retries
# actions whose outcome matches. Dedup rejections ("NOT logged — duplicate")
# are deliberately absent: they are correct behavior, and a retry would
# double-log the very thing dedup caught.
_FAILURE_MARKERS = ("rejected", "couldn't", "failed", "unknown action",
                    "missing required", "— need", "needs a", "no open todo",
                    "no active transaction", "no unread item", "no open need",
                    "no reading item", "usage:")


def looks_failed(outcome: str) -> bool:
    """Did this action outcome report a failure the model should fix?"""
    lower = str(outcome).lower()
    if "duplicate" in lower:
        return False
    return any(marker in lower for marker in _FAILURE_MARKERS)
