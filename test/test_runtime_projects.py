"""Scratch-only journeys for durable projects, scoped approvals and real verification."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread
import hashlib
import json
import os
import signal
import sys
import time

import pytest

from assistant.agent.runtime.projects import ProjectManager, ProjectCancelled
from assistant.platform.runtime.contracts import ChannelEvent, Completion, ToolCall
from assistant.platform.runtime.store import RuntimeStore


class NativeBackend:
    """A controllable native coding boundary; all host storage and checks are real."""

    kind = "fixture-codex"
    capabilities = frozenset({"native_agent"})

    def __init__(self, action=None):
        self.action = action
        self.calls = []

    def complete(self, request):
        """Capture the workspace contract and optionally change scratch artifacts."""
        self.calls.append(request)
        if self.action:
            return self.action(request)
        return Completion(text="No changes needed.", session_id="backend-session")


def workspace(tmp_path, name="project"):
    """Create a project outside the assistant's private scratch data directory."""
    path = tmp_path / name
    path.mkdir()
    return path


def test_runtime_messages_and_events_survive_reopen(tmp_path):
    store = RuntimeStore(tmp_path)
    for index in range(5):
        store.add_message("room-a", "user", str(index), tool_call_id=f"t{index}")
    store.add_message("room-b", "user", "private")
    store.record_event("run-1", "started", {"attempt": 1})
    store.record_event("run-1", "done", {"verified": False})
    reopened = RuntimeStore(tmp_path)
    assert [row["content"] for row in reopened.messages("room-a", limit=2)] == ["3", "4"]
    assert reopened.messages("room-a")[-1]["metadata"] == {"tool_call_id": "t4"}
    assert [row["kind"] for row in reopened.events("run-1")] == ["started", "done"]
    assert reopened.messages("missing") == []
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_durable_messages_mask_pasted_tokens(tmp_path):
    store = RuntimeStore(tmp_path)
    token = "ghp_" + "a" * 36
    store.add_message("s", "user", token, source=token)
    store.record_event("r", "error", {"text": token})
    assert token not in json.dumps(store.messages("s"))
    assert token not in json.dumps(store.events("r"))


def test_runtime_database_refuses_a_symlink_before_opening_it(tmp_path):
    """A substituted DB path must not write runtime data into another owner's file."""
    target = tmp_path / "outside.sqlite3"
    target.write_text("untouched")
    data = tmp_path / "private"
    data.mkdir()
    (data / "runtime.sqlite3").symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        RuntimeStore(data)
    assert target.read_text() == "untouched"


def test_channel_dedup_is_atomic_durable_and_account_scoped(tmp_path):
    store = RuntimeStore(tmp_path / "owner-a")
    event = ChannelEvent("slack", "account-1", "event-7", "u", "room", "hello")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.accept_event(event), range(24)))
    assert results.count(True) == 1
    assert not RuntimeStore(tmp_path / "owner-a").accept_event(event)
    assert store.accept_event(replace(event, account_id="account-2"))
    assert RuntimeStore(tmp_path / "owner-b").accept_event(event)
    pending = store.pending_events()
    assert pending[0]["envelope"]["text"] == "hello"
    store.mark_event(event.dedup_key, "unknown", "model reply saved, send ambiguous")
    assert len(store.pending_events()) == 1


def test_approval_exact_scope_arguments_expiry_and_atomic_single_use(tmp_path, monkeypatch):
    from assistant.platform.runtime import store as module

    clock = [1000.0]
    monkeypatch.setattr(module.time, "time", lambda: clock[0])
    store = RuntimeStore(tmp_path)
    grant = store.create_approval("owner-room", "publish", {"repo": "one", "overwrite": False}, ttl=10)
    assert grant["status"] == "pending" and not grant["consumed"]
    assert not store.consume_approval(grant["id"], "other-room", "publish", grant["args"])
    assert not store.consume_approval(grant["id"], "owner-room", "delete", grant["args"])
    assert not store.consume_approval(grant["id"], "owner-room", "publish", {"repo": "two"})
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.consume_approval(
            grant["id"], "owner-room", "publish", {"overwrite": False, "repo": "one"}), range(16)))
    assert results.count(True) == 1
    assert store.get_approval(grant["id"])["consumed"]
    expiring = store.create_approval("s", "send", {}, ttl=1)
    clock[0] += 2
    assert not store.consume_approval(expiring["id"], "s", "send", {})
    assert store.get_approval(expiring["id"])["status"] == "expired"
    assert store.get_approval("missing") is None


def test_approval_does_not_cross_owner_data_directories(tmp_path):
    owner_a, owner_b = RuntimeStore(tmp_path / "a"), RuntimeStore(tmp_path / "b")
    grant = owner_a.create_approval("same-room", "website", {"repo": "a/site"})
    assert not owner_b.consume_approval(grant["id"], "same-room", "website", grant["args"])


def test_project_registration_is_explicit_and_snapshots_workspace(settings, tmp_path):
    manager = ProjectManager(settings, NativeBackend())
    first, second = workspace(tmp_path, "first"), workspace(tmp_path, "second")
    with pytest.raises(KeyError):
        manager.submit("unregistered", "edit files")
    with pytest.raises(ValueError):
        manager.register("../bad", first)
    settings.data_dir.mkdir(exist_ok=True)
    with pytest.raises(ValueError):
        manager.register("private", settings.data_dir)
    child = settings.data_dir / "child"
    child.mkdir()
    with pytest.raises(ValueError, match="overlap"):
        manager.register("private-child", child)
    with pytest.raises(ValueError, match="overlap"):
        manager.register("private-parent", settings.data_dir.parent)
    manager.register("demo", first)
    task = manager.submit("demo", "write a greeting", "channel-session")
    manager.register("demo", second)
    assert manager.status(task["id"])["workspace"] == str(first)
    assert manager.list()[0]["path"] == str(second)
    with pytest.raises(ValueError):
        manager.register("alias", second)
    with pytest.raises(ValueError):
        manager.register("bad-command", first, verification=["python -m pytest"])


def test_project_configured_roots_refuse_untrusted_locations(settings, tmp_path):
    allowed, outside = workspace(tmp_path, "allowed"), workspace(tmp_path, "outside")
    scoped = settings.model_copy(update={"runtime_project_roots": [allowed]})
    manager = ProjectManager(scoped, NativeBackend())
    manager.register("allowed", allowed)
    with pytest.raises(ValueError, match="project roots"):
        manager.register("outside", outside)


def test_native_project_edits_verifies_records_artifacts_and_survives_restart(settings, tmp_path):
    target = workspace(tmp_path)

    def implement(request):
        """Only the provider boundary is fake; a real subprocess verifies the artifact."""
        assert request.cwd == target and request.allow_write and not request.tools
        (request.cwd / "greet.py").write_text("print('HELLO_PROJECT')\n")
        return Completion(text="Created greet.py.", session_id="thread-exact")

    backend = NativeBackend(implement)
    manager = ProjectManager(settings, backend)
    manager.register("demo", target, verification=[[sys.executable, "greet.py"]])
    task = manager.submit("demo", "create a greeting script")
    assert manager.next_queued()["id"] == task["id"]
    result = manager.run(task["id"])
    assert result["status"] == "done" and result["verified"]
    assert result["backend_session_id"] == "thread-exact"
    assert result["evidence"][0]["returncode"] == 0
    assert "HELLO_PROJECT" in result["evidence"][0]["stdout"]
    artifact = next(item for item in result["artifacts"] if item["path"] == "greet.py")
    assert artifact["sha256"] == hashlib.sha256((target / "greet.py").read_bytes()).hexdigest()
    evidence = Path(result["evidence_path"])
    assert evidence.is_relative_to(settings.data_dir)
    assert evidence.stat().st_mode & 0o777 == 0o600
    payload = json.loads(evidence.read_text())
    assert payload["verified"] and payload["checks"][0]["returncode"] == 0
    assert payload["verification_isolation"] == "trusted-host"
    assert payload["checks"][0]["isolation"] == "trusted-host"
    reopened = ProjectManager(settings, NativeBackend())
    assert reopened.status(task["id"])["verified"]
    assert reopened.run(task["id"])["status"] == "done"
    assert len(backend.calls) == 1


def test_model_claim_without_independent_verification_is_not_verified(settings, tmp_path):
    manager = ProjectManager(settings, NativeBackend(lambda _: Completion(text="All tests pass!")))
    manager.register("demo", workspace(tmp_path))
    result = manager.run(manager.submit("demo", "inspect the project")["id"])
    assert result["status"] == "done" and not result["verified"]
    assert result["evidence_path"] == "" and result["evidence"] == []


def test_failed_verification_is_failed_even_when_backend_claims_success(settings, tmp_path):
    manager = ProjectManager(settings, NativeBackend(lambda _: Completion(text="Everything works.")))
    manager.register("demo", workspace(tmp_path), verification=[[sys.executable, "-c", "raise SystemExit(9)"]])
    result = manager.run(manager.submit("demo", "finish it")["id"])
    assert result["status"] == "failed" and not result["verified"]
    assert result["evidence"][0]["returncode"] == 9
    assert not result["evidence_path"]


def test_artifact_hashes_describe_files_after_verification(settings, tmp_path):
    target = workspace(tmp_path)
    (target / "result.txt").write_text("old")

    def implement(request):
        """The check intentionally changes the backend's original artifact."""
        (request.cwd / "result.txt").write_text("unformatted")
        return Completion(text="Changed result.txt")

    manager = ProjectManager(settings, NativeBackend(implement))
    registered = manager.register("demo", target, verification=[[
        sys.executable, "-c", "from pathlib import Path; Path('result.txt').write_text('formatted')"]])
    assert registered["verification_isolation"] == "trusted-host"
    assert "outside" in registered["warning"]
    result = manager.run(manager.submit("demo", "write the result")["id"])
    artifact = next(item for item in result["artifacts"] if item["path"] == "result.txt")
    assert result["verified"] and (target / "result.txt").read_text() == "formatted"
    assert artifact["sha256"] == hashlib.sha256(b"formatted").hexdigest()


def test_nonterminal_backend_result_cannot_finish_project(settings, tmp_path):
    manager = ProjectManager(settings, NativeBackend(lambda _: Completion(text="Working...", finish_reason="length")))
    manager.register("demo", workspace(tmp_path))
    result = manager.run(manager.submit("demo", "finish it")["id"])
    assert result["status"] == "failed"
    assert "did not complete" in result["error"]


def test_job_claim_is_atomic_and_workspace_tasks_serialize(settings, tmp_path):
    store = RuntimeStore(settings.data_dir)
    store.register_project("demo", workspace(tmp_path))
    first, second = store.create_run("demo", "first"), store.create_run("demo", "second")
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _: store.claim_run(first["id"]), range(16)))
    assert claims.count(True) == 1
    assert not store.claim_run(second["id"])
    store.update_run(first["id"], status="done")
    assert store.claim_run(second["id"])


def test_restart_marks_running_work_interrupted_without_replaying(settings, tmp_path):
    manager = ProjectManager(settings, NativeBackend())
    manager.register("demo", workspace(tmp_path))
    running = manager.submit("demo", "may have changed files already")
    queued = manager.submit("demo", "not started")
    assert manager.store.claim_run(running["id"])
    restarted = ProjectManager(settings, NativeBackend())
    assert restarted.recover() == 1
    assert restarted.status(running["id"])["status"] == "interrupted"
    assert restarted.status(queued["id"])["status"] == "queued"
    assert not restarted.backend.calls


def test_cancel_queued_and_running_jobs_is_durable(settings, tmp_path):
    started = Event()

    def blocking(request):
        """A real cancellation event crosses from another manager through SQLite."""
        started.set()
        assert request.cancel.wait(4)
        return Completion(text="Interrupted")

    manager = ProjectManager(settings, NativeBackend(blocking))
    manager.register("demo", workspace(tmp_path))
    queued = manager.submit("demo", "cancel before start")
    assert manager.cancel(queued["id"])["status"] == "cancelled"
    assert manager.run(queued["id"])["status"] == "cancelled"
    task = manager.submit("demo", "long work")
    worker = Thread(target=manager.run, args=(task["id"],))
    worker.start()
    assert started.wait(2)
    other = ProjectManager(settings, NativeBackend())
    assert other.cancel(task["id"])["cancel_requested"]
    worker.join(5)
    assert not worker.is_alive()
    assert other.status(task["id"])["status"] == "cancelled"
    assert not other.status(task["id"])["verified"]


class ToolBackend:
    """Scripted API decisions exercise the real host workspace tools."""

    kind = "fixture-api"
    capabilities = frozenset({"tools"})

    def __init__(self, replies):
        self.replies, self.requests = iter(replies), []

    def complete(self, request):
        """Retain tool feedback so the test can inspect refusals and real file reads."""
        self.requests.append(request)
        return next(self.replies)


def test_api_project_uses_real_file_tools_and_independent_verification(settings, tmp_path):
    target = workspace(tmp_path)
    backend = ToolBackend([
        Completion(tool_calls=[ToolCall("one", "write_file", {"path": "greet.py", "content": "print('API_PROJECT')\n"})]),
        Completion(tool_calls=[ToolCall("two", "read_file", {"path": "greet.py"})]),
        Completion(text="Created and inspected greet.py."),
    ])
    manager = ProjectManager(settings, backend)
    manager.register("demo", target, verification=[[sys.executable, "greet.py"]])
    result = manager.run(manager.submit("demo", "write a script")["id"])
    assert result["status"] == "done" and result["verified"]
    assert "API_PROJECT" in result["evidence"][0]["stdout"]
    assert any("API_PROJECT" in item["content"] for item in backend.requests[-1].messages if item["role"] == "tool")
    assert all(request.tools for request in backend.requests)


def test_api_tools_confine_paths_and_deny_unrestricted_commands(settings, tmp_path):
    target = workspace(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    (target / "link").symlink_to(outside)
    (target / ".git").mkdir()
    (target / ".git" / "config").write_text("git metadata")
    (target / "alias").symlink_to(target / ".git", target_is_directory=True)
    manager = ProjectManager(settings, ToolBackend([]))
    cancel = Event()
    for name, arguments in [
        ("read_file", {"path": "../outside.txt"}),
        ("read_file", {"path": "link"}),
        ("write_file", {"path": "/tmp/escape", "content": "bad"}),
        ("write_file", {"path": ".git/config", "content": "bad"}),
        ("write_file", {"path": "alias/config", "content": "bad"}),
        ("read_file", {"path": "alias/config"}),
        ("run_command", {"argv": [sys.executable, "-c", "print('no')"]}),
    ]:
        with pytest.raises(ValueError):
            manager._tool(target, name, arguments, cancel)
    assert outside.read_text() == "private"
    assert (target / ".git" / "config").read_text() == "git metadata"


def test_api_tool_denial_returns_feedback_and_step_budget_is_enforced(settings, tmp_path):
    backend = ToolBackend([
        Completion(tool_calls=[ToolCall("one", "read_file", {"path": "../private"})]),
        Completion(tool_calls=[ToolCall("two", "not_a_tool", {})]),
    ])
    limited = settings.model_copy(update={"runtime_project_max_steps": 2})
    manager = ProjectManager(limited, backend)
    manager.register("demo", workspace(tmp_path))
    result = manager.run(manager.submit("demo", "try tools")["id"])
    assert result["status"] == "failed" and "step limit" in result["error"]
    feedback = [item for item in backend.requests[1].messages if item["role"] == "tool"]
    assert feedback and not json.loads(feedback[0]["content"])["ok"]


def test_registered_command_runner_has_timeout_and_cancellation(settings, tmp_path):
    manager = ProjectManager(settings, NativeBackend())
    target = workspace(tmp_path)
    with pytest.raises(RuntimeError, match="timeout"):
        manager._command(target, [sys.executable, "-c", "import time; time.sleep(5)"], Event(), 0.1)
    cancel = Event()
    cancel.set()
    with pytest.raises(ProjectCancelled):
        manager._command(target, [sys.executable, "-c", "import time; time.sleep(5)"], cancel, 5)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group regression")
def test_cancellation_kills_child_even_when_leader_exits_on_sigterm(settings, tmp_path):
    manager = ProjectManager(settings, NativeBackend())
    target = workspace(tmp_path)
    child_script = ("import os, signal, time; from pathlib import Path; "
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    "Path('child.pid').write_text(str(os.getpid())); "
                    "time.sleep(0.7); Path('late.txt').write_text('escaped cancellation')")
    parent_script = ("import subprocess, sys, time; "
                     f"subprocess.Popen([sys.executable, '-c', {child_script!r}]); time.sleep(10)")
    cancel, stopped = Event(), Event()
    failures = []

    def invoke():
        """Capture the expected cancellation instead of a background thread traceback."""
        try:
            manager._command(target, [sys.executable, "-c", parent_script], cancel, 5)
        except ProjectCancelled:
            stopped.set()
        except BaseException as exc:
            failures.append(exc)

    runner = Thread(target=invoke)
    runner.start()
    deadline = time.monotonic() + 3
    while not (target / "child.pid").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert (target / "child.pid").exists()
    child_pid = int((target / "child.pid").read_text())
    try:
        cancel.set()
        runner.join(3)
        assert stopped.is_set() and not runner.is_alive() and not failures
        time.sleep(0.8)
        assert not (target / "late.txt").exists()
    finally:
        cancel.set()
        runner.join(3)
        try:
            os.kill(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
