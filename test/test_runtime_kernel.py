"""Personal policies use a fake Pi transport and real durable owner stores.

The fake below simulates Pi's RPC lifecycle only for domain-policy tests. The
packaged Node tests exercise the actual SDK loop, batching and session files.
"""

import copy
import json
import sqlite3
from threading import Event

import pytest

from assistant.agent.runtime.kernel import Kernel
from assistant.platform.runtime.contracts import BackendRequest, Completion, ToolCall
from assistant.platform.runtime.pi import PausePi, PiResult
from assistant.platform.runtime.store import RuntimeStore


class ScriptedBackend:
    """Simulate a Pi session without supplying a production Python controller."""

    kind = "fake-pi"
    capabilities = frozenset({"pi_session", "tools"})

    def __init__(self, *responses):
        """Retain response scripts without mocking the kernel or its storage."""
        self.responses = list(responses)
        self.requests = []
        self.receipts = []

    def complete(self, request: BackendRequest) -> Completion:
        """Serve a single model task for retained LLM interface tests."""
        self.requests.append({"messages": copy.deepcopy(request.messages),
                              "system": request.system, "tools": request.tools,
                              "max_tokens": request.max_tokens})
        if not self.responses:
            raise AssertionError("unexpected provider call")
        result = self.responses.pop(0)
        return result(request) if callable(result) else result

    def run_session(self, request, tool_handler, *, session, history=None,
                    context_messages=None, max_steps=12):
        """Call domain dispatch like Pi's protocol, capturing one session request."""
        self.requests.append({"messages": copy.deepcopy(request.messages),
                              "system": request.system, "tools": request.tools,
                              "max_tokens": request.max_tokens,
                              "images": copy.deepcopy(request.images),
                              "session": session, "history": copy.deepcopy(history),
                              "context_messages": copy.deepcopy(context_messages),
                              "max_steps": max_steps})
        for _ in range(max_steps):
            if request.cancel and request.cancel.is_set():
                raise InterruptedError("Pi session cancelled")
            if not self.responses:
                raise AssertionError("unexpected Pi provider decision")
            response = self.responses.pop(0)
            response = response(request) if callable(response) else response
            if response.finish_reason not in {"stop", "end_turn", "completed", "success", "stop_sequence"}:
                return PiResult("failed", error="Pi did not complete: " + response.finish_reason)
            if not response.tool_calls:
                return PiResult("done", text=response.text, session_id="fake-pi-session")
            for call in response.tool_calls:
                try:
                    result = tool_handler(call.name, call.arguments)
                except PausePi as pause:
                    return PiResult("awaiting_approval", text=pause.text,
                                    approval_id=pause.approval_id)
                except (ValueError, KeyError, TypeError) as exc:
                    result = {"ok": False, "error": str(exc)}
                self.receipts.append({"id": call.id, "name": call.name, "result": result})
        return PiResult("failed", error=f"agent exceeded its {max_steps}-step budget")


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
    assert len(first_backend.requests) == 1
    tool = first_backend.receipts[0]
    fact = tool["result"]
    assert tool["name"] == "memory_remember" and tool["id"] == "call-1"
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
    hits = second_backend.receipts[0]["result"]
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


def test_approval_execution_receipt_is_visible_to_resumed_pi(settings, monkeypatch):
    """After a text approval, Pi receives the actual outcome as visible context."""
    from assistant.agent import orchestrator
    monkeypatch.setattr(orchestrator, "run", lambda *args, **kwargs: 0)
    backend = ScriptedBackend(action("skill_run", {"name": "daily", "arguments": {}}),
                              Completion(text="The approved daily run completed."))
    kernel = Kernel(settings, backend)
    paused = kernel.turn("Run the daily pipeline", session="room-a")
    approved = kernel.turn(f"/approve {paused.approvals[0]}", session="room-a")
    kernel.turn("What happened?", session="room-a")
    assert len(backend.requests) == 2
    receipt = backend.requests[-1]["context_messages"][0]
    assert receipt["custom_type"] == "approval_result"
    assert receipt["content"] == approved.text
    assert receipt["details"]["approval_id"] == paused.approvals[0]
    assert receipt["details"]["source_message_id"].startswith("audit:")


def test_failed_approved_action_is_visible_without_replaying_grant(settings, monkeypatch):
    """A failed approved action remains consumed and Pi sees its bounded failure."""
    backend = ScriptedBackend(action("skill_run", {"name": "daily", "arguments": {}}),
                              Completion(text="The approved action failed."))
    kernel = Kernel(settings, backend)
    paused = kernel.turn("Run the daily pipeline", session="room-a")
    ident = paused.approvals[0]

    def fail(*args, **kwargs):
        """Model an action failure whose raw message must not enter context."""
        raise RuntimeError("private provider body must stay out of the receipt")

    monkeypatch.setattr(kernel, "execute", fail)
    with pytest.raises(RuntimeError):
        kernel.turn(f"/approve {ident}", session="room-a")
    assert kernel.store.get_approval(ident)["status"] == "consumed"
    with pytest.raises(ValueError, match="consumed"):
        kernel.turn(f"/approve {ident}", session="room-a")
    kernel.turn("Did that work?", session="room-a")
    receipt = backend.requests[-1]["context_messages"][0]
    content = json.loads(receipt["content"])
    assert content["ok"] is False and content["error"] == "RuntimeError"
    assert content["approval_id"] == ident
    assert "private provider body" not in receipt["content"]


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
    receipt = backend.receipts[0]["result"]
    assert receipt.get("ok", False) is False and receipt["error"]


def test_pi_budget_failure_is_recorded_instead_of_completion(settings):
    """The kernel forwards its budget to Pi and preserves Pi's failure state."""
    backend = ScriptedBackend(*[action("skills_list", {}, str(i)) for i in range(3)])
    kernel = Kernel(settings, backend, max_steps=3)
    with pytest.raises(RuntimeError, match="3-step budget"):
        kernel.turn("Keep looking", session="loop")
    assert len(backend.requests) == 1 and backend.requests[0]["max_steps"] == 3
    assert [row["role"] for row in kernel.store.messages("loop")] == ["user"]
    with sqlite3.connect(kernel.store.path) as conn:
        assert conn.execute("SELECT kind FROM run_events ORDER BY id DESC LIMIT 1").fetchone()[0] == "turn_failed"


def test_conversation_keys_and_legacy_context_are_scoped(settings):
    """The bridge gets stable separate Pi keys and explicit legacy context."""
    a = ScriptedBackend(Completion(text="A private response"))
    Kernel(settings, a).turn("Room-A-only phrase", session="a")
    b = ScriptedBackend(Completion(text="B response"))
    Kernel(settings, b).turn("Room-B-only phrase", session="b")
    assert a.requests[0]["session"] != b.requests[0]["session"]
    assert "Room-A-only" not in json.dumps(b.requests[0])
    again = ScriptedBackend(Completion(text="A continuation"))
    restarted = Kernel(settings, again)
    restarted.turn("Continue here", session="a")
    assert again.requests[0]["session"] == a.requests[0]["session"]
    assert "Room-B-only phrase" not in json.dumps(again.requests[0])
    imported = ScriptedBackend(Completion(text="Legacy continued"))
    kernel = Kernel(settings, imported)
    kernel.seed_history("old", [{"role": "user", "content": "Legacy Orchid decision"},
                                {"role": "assistant", "content": "Kept that decision"}])
    kernel.seed_history("old", [{"role": "user", "content": "Duplicate imported history"}])
    kernel.turn("Continue that", session="old")
    context = json.dumps(imported.requests[0]["history"])
    assert "Legacy Orchid decision" in context and "Kept that decision" in context
    assert "Duplicate imported history" not in context


def test_cancellation_never_persists_a_successful_assistant_reply(settings):
    """A cancelled Pi turn propagates cancellation and leaves an audit failure."""
    stop = Event()
    stop.set()
    kernel = Kernel(settings, ScriptedBackend(Completion(text="Must not be used")))
    with pytest.raises(InterruptedError, match="cancel"):
        kernel.turn("Work on my request", session="cancelled", cancel=stop)
    assert not any(row["role"] == "assistant" for row in kernel.store.messages("cancelled"))


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


@pytest.mark.parametrize("name,args", [
    ("memory_remember", {"text": "An injected visual instruction"}),
    ("skill_draft", {"name": "injected", "task_id": "unknown", "instructions": "Run this"}),
    ("skill_activate", {"draft_id": "unknown"}),
    ("project_submit", {"project": "orchid", "goal": "Run visual instructions"}),
    ("project_cancel", {"task_id": "unknown"}),
    ("skill_run", {"name": "add_todo", "arguments": {"title": "Injected task"}}),
    ("skill_run", {"name": "daily", "arguments": {}}),
    ("skill_run", {"name": "remember_memory", "arguments": {
        "text": "An injected visual instruction", "source": "image"}}),
])
def test_image_turn_cannot_execute_other_mutations(settings, monkeypatch, name, args):
    """Images supply log data, never permission for other state changes."""
    backend = ScriptedBackend(action(name, args), Completion(text="Please confirm in text."))
    kernel = Kernel(settings, backend)
    executed = []
    monkeypatch.setattr(kernel, "execute", lambda *args, **kwargs: executed.append(args))
    result = kernel.turn("Follow this screenshot", session="images", image_paths=["fixture.png"])
    assert result.status == "done" and executed == []
    assert backend.receipts[0]["result"]["error"]
    assert backend.requests[0]["images"] == ["fixture.png"]


@pytest.mark.parametrize("name,args", [
    ("log_meal", {"description": "Noodles"}),
    ("log_exercise", {"activity": "Running"}),
    ("log_weight", {"weight_kg": 70.5}),
    ("log_transaction", {"kind": "expense", "amount": 45}),
])
def test_image_turn_retains_append_only_logging_skills(settings, monkeypatch, name, args):
    """The four approved image logging operations still reach trusted skills."""
    from assistant.agent.actions.base import ActionResult
    backend = ScriptedBackend(action("skill_run", {"name": name, "arguments": args}),
                              Completion(text="Recorded from the image."))
    kernel = Kernel(settings, backend)
    executed = []

    def record(actual_name, actual_args):
        """Capture the trusted skill invocation without changing personal stores."""
        executed.append((actual_name, actual_args))
        return ActionResult(True, "Recorded")

    monkeypatch.setattr(kernel.skills, "execute", record)
    result = kernel.turn("Record this", session="images", image_paths=["fixture.png"])
    assert result.status == "done" and executed == [(name, args)]
    assert backend.receipts[0]["result"]["ok"] is True


@pytest.mark.parametrize("image_args", [
    {"image_paths": ["fixture.png"]}, {"rejected_images": ["Unreadable attachment"]},
])
def test_image_approval_does_not_consume_a_grant(settings, monkeypatch, image_args):
    """Even an exact pending grant requires a new text-only approval turn."""
    backend = ScriptedBackend(action("skill_run", {"name": "daily", "arguments": {}}))
    kernel = Kernel(settings, backend)
    paused = kernel.turn("Run the daily pipeline", session="room-a")
    executed = []
    monkeypatch.setattr(kernel, "execute", lambda *args, **kwargs: executed.append(args))
    ident = paused.approvals[0]
    with pytest.raises(ValueError, match="image|text-only"):
        kernel.turn(f"/approve {ident}", session="room-a", **image_args)
    assert executed == [] and kernel.store.get_approval(ident)["status"] == "pending"


def test_legacy_llm_uses_selected_runtime_backend_without_api_calls(settings, monkeypatch):
    """Retained skills route model requests through Pi without running a CLI."""
    from assistant.platform import llm
    backend = ScriptedBackend(Completion(text='{"answer":"preserved"}'))
    selected = []
    monkeypatch.setattr(llm, "build_pi_backend", lambda config, data_dir: selected.append(config) or backend)
    route = settings.model_copy(update={"runtime_backend": "codex"})
    client = llm.LLM(route)
    result = client.complete_json("Return JSON", system="Preserve the workflow", max_tokens=123)
    assert result == {"answer": "preserved"}
    assert selected[0].kind == "codex"
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
