"""End-to-end kernel journeys use scripted models and real durable owner stores."""

import copy
import json
import sqlite3

import pytest

from assistant.agent.runtime.kernel import Kernel
from assistant.platform.runtime.contracts import BackendRequest, Completion, ToolCall
from assistant.platform.runtime.store import RuntimeStore


class ScriptedBackend:
    """Supply deterministic model decisions while capturing exact provider context."""

    kind = "scripted"
    capabilities = frozenset({"tools"})

    def __init__(self, *responses):
        """Retain response scripts without mocking the kernel or its storage."""
        self.responses = list(responses)
        self.requests = []

    def complete(self, request: BackendRequest) -> Completion:
        """Snapshot messages before the real kernel appends tool outcomes."""
        self.requests.append({"messages": copy.deepcopy(request.messages),
                              "system": request.system, "tools": request.tools,
                              "max_tokens": request.max_tokens})
        if not self.responses:
            raise AssertionError("unexpected provider call")
        result = self.responses.pop(0)
        return result(request) if callable(result) else result


def action(name, arguments, ident="call-1"):
    """Construct a host tool call, independent of any provider's JSON spelling."""
    return Completion(tool_calls=[ToolCall(ident, name, arguments)])


def test_truncated_completion_is_not_reported_done(settings):
    """A partial model answer cannot become a successful durable turn receipt."""
    kernel = Kernel(settings, ScriptedBackend(Completion(text="partial", finish_reason="length")))
    with pytest.raises(RuntimeError, match="did not complete"):
        kernel.turn("Explain the project", session="truncated")
    assert [m["role"] for m in kernel.store.messages("truncated")] == ["user"]


def test_memory_tool_receipt_persists_across_kernel_restart(settings):
    """A remembered owner fact and its source survive a fresh runtime instance."""
    first_backend = ScriptedBackend(
        action("memory_remember", {"text": "Orchid uses SQLite", "project": "orchid"}),
        Completion(text="Saved your Orchid decision."))
    first = Kernel(settings, first_backend)
    result = first.turn("Remember that Orchid uses SQLite.", session="room-a", project="orchid")
    assert result.status == "done"
    tool = first_backend.requests[1]["messages"][-1]
    fact = json.loads(tool["content"])
    assert tool["role"] == "tool" and tool["tool_call_id"] == "call-1"
    assert fact["source"] == "session:room-a" and fact["project"] == "orchid"
    assert [event["kind"] for event in first.store.events(result.run_id)] == [
        "turn_started", "tool_result", "turn_done"]

    second_backend = ScriptedBackend(
        action("memory_search", {"query": "SQLite", "project": "orchid"}),
        Completion(text="Orchid uses SQLite, per your earlier message."))
    restarted = Kernel(settings, second_backend)
    reply = restarted.turn("What database does Orchid use?", session="room-a", project="orchid")
    assert reply.status == "done"
    assert fact["id"] in second_backend.requests[0]["system"]
    hits = json.loads(second_backend.requests[1]["messages"][-1]["content"])
    assert hits[0]["id"] == fact["id"] and hits[0]["source"] == "session:room-a"
    assert len(RuntimeStore(settings.data_dir).messages("room-a")) == 4


@pytest.mark.parametrize("phase", ["all", "run", "daily", "digest", "collect", "profile", "deliver"])
def test_repeated_full_pipeline_aliases_pause_without_execution(settings, monkeypatch, phase):
    """Indirect full-run aliases cannot bypass approvals on repeated requests."""
    from assistant.agent import orchestrator
    executed = []
    monkeypatch.setattr(orchestrator, "run", lambda *args, **kwargs: executed.append(kwargs) or 0)
    backend = ScriptedBackend(*[
        action("skill_run", {"name": "run_phase", "arguments": {"phase": phase}}, f"call-{index}")
        for index in range(2)])
    kernel = Kernel(settings, backend)
    first = kernel.turn("Refresh my daily information", session="room-a")
    second = kernel.turn("Try again", session="room-a")
    assert executed == []
    assert first.status == second.status == "awaiting_approval"
    assert first.approvals != second.approvals
    for turn in (first, second):
        pending = kernel.store.get_approval(turn.approvals[0])
        assert pending["status"] == "pending"
        assert pending["args"] == {"name": "run_phase", "arguments": {"phase": phase}}


def test_approval_is_exact_session_scoped_single_use_and_restart_safe(settings, monkeypatch):
    """Only the originating conversation can spend a durable exact-action grant."""
    from assistant.agent import orchestrator
    executed = []
    monkeypatch.setattr(orchestrator, "run", lambda *args, **kwargs: executed.append(kwargs) or 0)
    kernel = Kernel(settings, ScriptedBackend(action("skill_run", {
        "name": "run_phase", "arguments": {"phase": "all"}})))
    paused = kernel.turn("Run the daily pipeline", session="room-a")
    ident = paused.approvals[0]
    pending = kernel.store.get_approval(ident)
    assert not kernel.store.consume_approval(ident, "room-a", "skill_run", {
        "name": "run_phase", "arguments": {"phase": "website"}})
    assert kernel.store.get_approval(ident)["status"] == "pending"

    restarted = Kernel(settings, ScriptedBackend())
    with pytest.raises(ValueError, match="another session"):
        restarted.turn(f"/approve {ident}", session="room-b")
    assert executed == [] and restarted.store.get_approval(ident)["status"] == "pending"
    approved = restarted.turn(f"/approve {ident}", session="room-a")
    assert approved.status == "done" and len(executed) == 1
    assert restarted.store.get_approval(ident)["status"] == "consumed"
    assert pending["tool"] == "skill_run"
    with pytest.raises(ValueError, match="consumed"):
        restarted.turn(f"/approve {ident}", session="room-a")
    assert len(executed) == 1


def test_pause_stops_later_calls_in_the_same_model_batch(settings):
    """An approval pause must stop the current batch before another tool runs."""
    backend = ScriptedBackend(Completion(tool_calls=[
        ToolCall("risky", "skill_run", {"name": "daily", "arguments": {}}),
        ToolCall("later", "memory_remember", {"text": "This must not be remembered"})]))
    kernel = Kernel(settings, backend)
    result = kernel.turn("Prepare the daily report", session="room-a")
    assert result.status == "awaiting_approval"
    assert kernel.memory.recall("must not be remembered") == []
    assert len(backend.requests) == 1


@pytest.mark.parametrize("name,args", [
    ("shell", {"command": "do not execute"}),
    ("memory_remember", {}),
    ("memory_remember", {"text": 3}),
    ("memory_search", {"query": "fact", "handler": "os.system"}),
    ("skill_run", {"name": "list_todos", "arguments": "not-an-object"}),
])
def test_malformed_and_unknown_tools_return_failures_to_model(settings, name, args):
    """Invalid model output is observable and cannot reach a trusted handler."""
    backend = ScriptedBackend(action(name, args), Completion(text="That operation was refused."))
    result = Kernel(settings, backend).turn("Check this operation", session="room-a")
    assert result.status == "done"
    receipt = json.loads(backend.requests[1]["messages"][-1]["content"])
    assert receipt["ok"] is False and receipt["error"]


def test_tool_loop_budget_records_failure_instead_of_completion(settings):
    """A model that keeps asking for tools cannot run forever or report done."""
    backend = ScriptedBackend(*[action("skills_list", {}, str(i)) for i in range(3)])
    kernel = Kernel(settings, backend, max_steps=3)
    with pytest.raises(RuntimeError, match="3-step budget"):
        kernel.turn("Keep looking", session="loop")
    assert len(backend.requests) == 3
    assert [row["role"] for row in kernel.store.messages("loop")] == ["user"]
    with sqlite3.connect(kernel.store.path) as conn:
        assert conn.execute("SELECT kind FROM run_events ORDER BY id DESC LIMIT 1").fetchone()[0] == "turn_failed"


def test_session_histories_are_independent_and_restored(settings):
    """Conversation A is retained without appearing in another room's prompt."""
    a = ScriptedBackend(Completion(text="A private response"))
    Kernel(settings, a).turn("Room-A-only phrase", session="a")
    b = ScriptedBackend(Completion(text="B response"))
    Kernel(settings, b).turn("Room-B-only phrase", session="b")
    assert "Room-A-only" not in json.dumps(b.requests[0]["messages"])
    again = ScriptedBackend(Completion(text="A continuation"))
    Kernel(settings, again).turn("Continue here", session="a")
    context = json.dumps(again.requests[0]["messages"])
    assert "Room-A-only phrase" in context and "A private response" in context
    assert "Room-B-only phrase" not in context


def test_project_submission_is_queued_only_after_exact_approval(settings, tmp_path):
    """A task acknowledgment represents durable queueing, not completed coding."""
    workspace = tmp_path / "orchid-project"
    workspace.mkdir()
    backend = ScriptedBackend(action("project_submit", {"project": "orchid", "goal": "Create a README"}))
    kernel = Kernel(settings, backend)
    kernel.projects.register("orchid", workspace)
    paused = kernel.turn("Create a README in Orchid", session="room-a")
    assert kernel.store.runs() == []
    result = kernel.approve(paused.approvals[0], session="room-a")
    queued = json.loads(result.text)
    assert queued["status"] == "queued" and queued["session"] == "room-a"
    assert len(kernel.store.runs()) == 1


def test_legacy_llm_uses_selected_runtime_backend_without_api_calls(settings, monkeypatch):
    """Retained personal skills can use Codex through their existing LLM interface."""
    from assistant.platform import llm
    from assistant.platform.runtime import backends
    backend = ScriptedBackend(Completion(text='{"answer":"preserved"}'))
    selected = []
    monkeypatch.setattr(backends, "build_backend", lambda config: selected.append(config) or backend)
    route = settings.model_copy(update={"runtime_backend": "codex"})
    client = llm.LLM(route)
    result = client.complete_json("Return JSON", system="Preserve the workflow", max_tokens=123)
    assert result == {"answer": "preserved"}
    assert selected[0].kind == "codex" and client.client is None
    assert backend.requests[0]["max_tokens"] == 123
    assert backend.requests[0]["system"] == "Preserve the workflow"


def test_pasted_model_and_channel_credentials_are_not_durable(settings):
    """Conversation audit storage must redact API and Slack keys as memory does."""
    key, slack = "sk-proj-" + "A" * 40, "xoxb-" + "B" * 40
    kernel = Kernel(settings, ScriptedBackend(Completion(text="Use secure configuration for credentials.")))
    kernel.turn(f"My credentials are {key} and {slack}", session="room-a")
    durable = json.dumps(kernel.store.messages("room-a"))
    assert key not in durable and slack not in durable


@pytest.mark.parametrize("contents", [
    '[backends.claude]\nallow_unrestricted = "false"\n',
    '[channels.slack]\nenabled = "false"\n',
])
def test_configuration_permission_switches_require_real_booleans(settings, tmp_path, contents):
    """A string spelling false must not enable unrestricted work or a channel."""
    from assistant.platform.runtime.config import load_runtime_config
    path = tmp_path / "runtime.toml"
    path.write_text(contents)
    with pytest.raises(ValueError, match="boolean"):
        load_runtime_config(settings, path=path)
