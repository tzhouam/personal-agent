from assistant.actions import ACTIONS, execute, prompt_block, run_action, validate
from assistant.state import persist_state
from assistant.todo_store import ReadingList, TodoStore


def test_registry_covers_the_llm_action_set():
    llm_actions = {name for name, a in ACTIONS.items() if a.llm}
    assert llm_actions == {"add_todo", "done_todo", "done_reading", "trigger_run"}
    block = prompt_block()
    for name in llm_actions:
        assert name in block
    # non-LLM actions never appear in the chat prompt
    assert "list_todos" not in block and "run_status" not in block


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
