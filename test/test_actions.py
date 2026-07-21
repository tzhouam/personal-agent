from assistant.agent.actions import ACTIONS, execute, prompt_block, run_action, validate
from assistant.agent.state import persist_state
from assistant.agent.todo_store import ReadingList, TodoStore


def test_registry_covers_the_llm_action_set():
    llm_actions = {name for name, a in ACTIONS.items() if a.llm}
    assert llm_actions == {"add_todo", "done_todo", "done_reading", "trigger_run",
                           "run_phase", "reboot", "plan_task", "web_search",
                           "set_reminder", "cancel_reminder",
                           "create_routine", "cancel_routine", "unrelated_reading",
                           "log_transaction", "void_transaction", "finance_summary",
                           "recategorize_transaction", "query_transactions",
                           "log_meal", "log_exercise", "log_weight",
                           "set_health_profile", "add_health_need",
                           "done_health_need", "health_summary", "query_health",
                           "execute_task", "approve_task", "create_workflow",
                           "run_workflow", "show_workflow", "update_workflow",
                           "retire_workflow", "learn_preference",
                           "retire_preference", "self_evolve"}
    block = prompt_block()
    for name in llm_actions:
        assert name in block
    # non-LLM actions never appear in the chat prompt
    assert "list_todos" not in block and "run_status" not in block


def test_run_phase_validation_and_dispatch(settings, monkeypatch):
    # subprocess + _trigger_run live in the actions.handlers submodule
    from assistant.agent.actions import handlers as actions_mod

    # unknown phase → helpful error, nothing spawned
    result = run_action("run_phase", {"phase": "frobnicate"}, settings)
    assert "unknown phase" in result and "research" in result

    # website runs inline and reports the sync result
    monkeypatch.setattr("assistant.agent.website.sync_website",
                        lambda s, p, t, reading=None: {"status": "pushed", "url": "https://x"})
    monkeypatch.setattr("assistant.agent.profile_store.ProfileStore.load", lambda self: {})
    assert run_action("run_phase", {"phase": "website"}, settings) \
        == "website sync: pushed https://x"

    # slow phases spawn `assistant run-phase <phase>` in the background
    spawned = []
    monkeypatch.setattr(actions_mod.subprocess, "Popen",
                        lambda cmd, **kw: spawned.append(cmd))
    result = run_action("run_phase", {"phase": "research"}, settings)
    assert "started in the background" in result
    assert spawned[0][-2:] == ["run-phase", "research"]

    # pipeline-dependent phases fall back to the full run
    monkeypatch.setattr(actions_mod, "_trigger_run", lambda s, p: "daily run started")
    assert "full pipeline" in run_action("run_phase", {"phase": "digest"}, settings)


def test_plan_task_plans_and_tracks(settings, monkeypatch):
    plan = {"title": "Book team dinner", "due": "2026-07-15",
            "steps": [{"who": "agent", "step": "track and remind"},
                      {"who": "owner", "step": "pick one of the 3 candidates"}],
            "next": "search Dianping for Sichuan near the office"}

    class FakeLLM:
        def __init__(self, settings):
            pass

        def complete_json(self, prompt, system=None, **kw):
            assert "book a dinner" in prompt
            return plan

    monkeypatch.setattr("assistant.platform.llm.LLM", FakeLLM)
    result = run_action("plan_task", {"request": "book a dinner for the team"}, settings)
    assert result.startswith("planned: Book team dinner (todo t1)")
    assert "[owner] pick one of the 3 candidates" in result
    assert "→ next: search Dianping" in result
    todo = TodoStore(settings.profile_dir).open_items()[0]
    assert todo["title"] == "Book team dinner" and todo["due"] == "2026-07-15"
    assert "[agent] track and remind" in todo["detail"]

    # unplannable → graceful line, no todo
    monkeypatch.setattr(FakeLLM, "complete_json", lambda self, *a, **k: {"steps": []})
    assert "couldn't produce a plan" in run_action("plan_task", {"request": "x"}, settings)
    assert len(TodoStore(settings.profile_dir).open_items()) == 1


def test_run_action_todo_roundtrip(settings):
    assert run_action("list_todos", {}, settings) == "(no open todos)"
    line = run_action("add_todo", {"title": "Buy GPU", "due": "2026-07-15"}, settings)
    assert line == "added todo t1: Buy GPU"
    assert "[t1] Buy GPU" in run_action("list_todos", {}, settings)
    assert "due:2026-07-15" in run_action("list_todos", {}, settings)
    # dedup on open key
    assert run_action("add_todo", {"title": "Buy GPU"}, settings) == "todo already tracked"
    assert run_action("done_todo", {"id": "t1"}, settings) == "todo t1 marked done"
    assert run_action("done_todo", {"id": "t1"}, settings) == "no open todo 't1'"


def test_run_action_reading_and_status(settings):
    ReadingList(settings.profile_dir).upsert("arxiv:1", title="Paper A", url="http://x")
    assert "[r1] Paper A" in run_action("list_reading", {}, settings)
    assert run_action("done_reading", {"id": "r1"}, settings) == "reading item r1 marked read"
    assert run_action("list_reading", {}, settings) == "(reading list empty)"

    assert run_action("run_status", {}, settings) == "no runs yet"
    persist_state(settings.state_file, run_id="run-x", phase="research")
    status = run_action("run_status", {}, settings)
    assert "run-x" in status and "incomplete" in status


def test_run_action_unknown_and_invalid(settings):
    try:
        run_action("rm_rf", {}, settings)
        assert False, "unknown action accepted"
    except KeyError:
        pass
    try:
        run_action("add_todo", {"title": "  "}, settings)
        assert False, "missing title accepted"
    except ValueError as exc:
        assert "missing required 'title'" in str(exc)


def test_execute_llm_surface_only(settings):
    outcomes = execute(
        [{"type": "add_todo", "title": "Review PR"},
         {"type": "list_todos"},           # registered but not llm-exposed
         {"type": "delete_profile"},       # unregistered
         {"type": "done_todo"},            # missing required id
         "not-a-dict"],
        settings)
    assert outcomes == [
        "added todo t1: Review PR",
        "unknown action 'list_todos' ignored",
        "unknown action 'delete_profile' ignored",
        "action done_todo: missing required 'id'",
    ]
    assert [t["title"] for t in TodoStore(settings.profile_dir).open_items()] \
        == ["Review PR"]


def test_execute_caps_action_count(settings):
    outcomes = execute([{"type": "add_todo", "title": f"T{i}"} for i in range(9)],
                       settings, max_actions=5)
    assert len(outcomes) == 5


def test_trigger_run_refuses_while_incomplete(settings):
    persist_state(settings.state_file, run_id="run-y", phase="deliver")
    assert run_action("trigger_run", {}, settings) \
        == "a run is already in progress (run-y)"


def test_validate_passes_optional_params():
    assert validate(ACTIONS["add_todo"], {"title": "x"}) is None
    assert validate(ACTIONS["trigger_run"], {}) is None


def test_reboot_action_spawns_detached_cli(settings, monkeypatch):
    # the chat reboot action must fire a detached `assistant.cli reboot --delay 3`
    # (delay so the reply flushes) and never block on the restart.
    import assistant.agent.actions.handlers as h

    spawned = []
    monkeypatch.setattr(h.subprocess, "Popen",
                        lambda cmd, **kw: spawned.append(cmd) or object())
    out = run_action("reboot", {}, settings)
    assert "重启" in out or "restart" in out.lower()
    assert spawned and spawned[0][-4:] == ["assistant.cli", "reboot", "--delay", "3"] \
        or ("assistant.cli" in spawned[0] and spawned[0][-2:] == ["--delay", "3"])


def test_serve_reboot_starts_daemon_when_unsupervised(settings, monkeypatch):
    # with no running daemon and no supervisor respawn, reboot() starts a fresh
    # detached `serve` and returns once it answers /healthz.
    import assistant.platform.serve as sv

    spawned = []
    monkeypatch.setattr(sv.subprocess, "Popen",
                        lambda cmd, **kw: spawned.append(cmd) or object())
    calls = {"n": 0}

    def fake_healthz(_settings):
        calls["n"] += 1
        return calls["n"] >= 2  # comes up shortly after we spawn it

    monkeypatch.setattr(sv, "_healthz", fake_healthz)
    monkeypatch.setattr(sv, "_pid_alive", lambda pid: False)
    result = sv.reboot(settings, timeout=3, stop_wait=0.5)
    assert result["status"] == "rebooted"
    assert spawned and spawned[0][-1] == "serve"
