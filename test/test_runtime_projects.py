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

from assistant.agent.runtime.projects import ProjectManager, ProjectCancelled, ProjectTimeout
from assistant.platform.runtime.contracts import BackendConfig, ChannelEvent
from assistant.platform.runtime.pi import PiResult
from assistant.platform.runtime.store import RuntimeStore


class SessionBackend:
    """Fake Pi transport; project storage, tool policy and verification remain real."""

    kind = "pi"
    capabilities = frozenset({"pi_session", "tools"})

    def __init__(self, action=None):
        self.action = action
        self.calls = []
        self.sessions = []
        self.limits = []

    def run_session(self, request, tool_handler, *, session, max_steps):
        """Stand in for one SDK session without simulating a Python model loop."""
        self.calls.append(request)
        self.sessions.append(session)
        self.limits.append(max_steps)
        if self.action:
            return self.action(request, tool_handler)
        return PiResult(status="done", text="No changes needed.", session_id="pi-session")

    def complete(self, request):
        """A project must never bypass Pi by calling the old completion boundary."""
        raise AssertionError("project execution must use a Pi session")


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
    manager = ProjectManager(settings, SessionBackend())
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
    manager = ProjectManager(scoped, SessionBackend())
    manager.register("allowed", allowed)
    with pytest.raises(ValueError, match="project roots"):
        manager.register("outside", outside)


def test_pi_project_edits_verifies_records_artifacts_and_survives_restart(settings, tmp_path):
    target = workspace(tmp_path)

    def implement(request, dispatch):
        """Only Pi transport is fake; workspace writes use the real host dispatcher."""
        assert request.cwd == target and not request.allow_write
        assert {item["function"]["name"] for item in request.tools} == {
            "read_file", "write_file", "list_files", "run_command"}
        assert request.messages == [{"role": "user", "content": "create a greeting script"}]
        assert request.timeout == settings.runtime_project_timeout
        assert dispatch("write_file", {"path": "greet.py", "content": "print('HELLO_PROJECT')\n"})["ok"]
        return PiResult(status="done", text="Created greet.py.", session_id="pi-exact")

    backend = SessionBackend(implement)
    # Old adapter capabilities cannot re-enable a completion/native-agent bypass.
    backend.capabilities = frozenset({"native_agent"})
    manager = ProjectManager(settings, backend)
    manager.register("demo", target, verification=[[sys.executable, "greet.py"]])
    task = manager.submit("demo", "create a greeting script")
    assert manager.next_queued()["id"] == task["id"]
    result = manager.run(task["id"])
    assert result["status"] == "done" and result["verified"]
    assert result["backend_session_id"] == "pi-exact"
    assert backend.sessions == ["project:" + task["id"]]
    assert backend.limits == [settings.runtime_project_max_steps]
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
    reopened = ProjectManager(settings, SessionBackend())
    assert reopened.status(task["id"])["verified"]
    assert reopened.run(task["id"])["status"] == "done"
    assert len(backend.calls) == 1


def test_model_claim_without_independent_verification_is_not_verified(settings, tmp_path):
    manager = ProjectManager(settings, SessionBackend(lambda _request, _dispatch: PiResult(status="done", text="All tests pass!")))
    manager.register("demo", workspace(tmp_path))
    result = manager.run(manager.submit("demo", "inspect the project")["id"])
    assert result["status"] == "done" and not result["verified"]
    assert result["evidence_path"] == "" and result["evidence"] == []


def test_failed_verification_is_failed_even_when_backend_claims_success(settings, tmp_path):
    manager = ProjectManager(settings, SessionBackend(lambda _request, _dispatch: PiResult(status="done", text="Everything works.")))
    manager.register("demo", workspace(tmp_path), verification=[[sys.executable, "-c", "raise SystemExit(9)"]])
    result = manager.run(manager.submit("demo", "finish it")["id"])
    assert result["status"] == "failed" and not result["verified"]
    assert result["evidence"][0]["returncode"] == 9
    assert not result["evidence_path"]


def test_artifact_hashes_describe_files_after_verification(settings, tmp_path):
    target = workspace(tmp_path)
    (target / "result.txt").write_text("old")

    def implement(request, dispatch):
        """The check intentionally changes the backend's original artifact."""
        assert dispatch("write_file", {"path": "result.txt", "content": "unformatted"})["ok"]
        return PiResult(status="done", text="Changed result.txt")

    manager = ProjectManager(settings, SessionBackend(implement))
    registered = manager.register("demo", target, verification=[[
        sys.executable, "-c", "from pathlib import Path; Path('result.txt').write_text('formatted')"]])
    assert registered["verification_isolation"] == "trusted-host"
    assert "outside" in registered["warning"]
    result = manager.run(manager.submit("demo", "write the result")["id"])
    artifact = next(item for item in result["artifacts"] if item["path"] == "result.txt")
    assert result["verified"] and (target / "result.txt").read_text() == "formatted"
    assert artifact["sha256"] == hashlib.sha256(b"formatted").hexdigest()


@pytest.mark.parametrize("status", ["failed", "step_limit", "awaiting_approval", "cancelled"])
def test_nonterminal_pi_result_never_runs_verification(settings, tmp_path, status):
    """A partial session may edit files, but cannot produce successful evidence."""
    target = workspace(tmp_path)

    def partial(request, dispatch):
        """A confident model report cannot override Pi's authoritative turn status."""
        dispatch("write_file", {"path": "partial.txt", "content": "still incomplete"})
        return PiResult(status=status, text="All work and tests completed!")

    backend = SessionBackend(partial)
    manager = ProjectManager(settings, backend)
    manager.register("demo", target, verification=[[
        sys.executable, "-c", "from pathlib import Path; Path('verified.txt').touch()"]])
    task = manager.submit("demo", "finish it")
    result = manager.run(task["id"])
    assert result["status"] == "failed" and not result["verified"]
    assert "did not complete" in result["error"]
    assert (target / "partial.txt").exists() and not (target / "verified.txt").exists()
    assert result["evidence"] == [] and not result["evidence_path"]
    assert len(backend.calls) == 1 and backend.sessions == ["project:" + task["id"]]


def test_pi_completion_without_report_cannot_finish_project(settings, tmp_path):
    """Even a completed Pi turn must return a usable project report."""
    backend = SessionBackend(lambda _request, _dispatch: PiResult(status="done", text=" "))
    manager = ProjectManager(settings, backend)
    manager.register("demo", workspace(tmp_path))
    result = manager.run(manager.submit("demo", "finish it")["id"])
    assert result["status"] == "failed" and "without a project report" in result["error"]


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
    manager = ProjectManager(settings, SessionBackend())
    manager.register("demo", workspace(tmp_path))
    running = manager.submit("demo", "may have changed files already")
    queued = manager.submit("demo", "not started")
    assert manager.store.claim_run(running["id"])
    restarted = ProjectManager(settings, SessionBackend())
    assert restarted.recover() == 1
    assert restarted.status(running["id"])["status"] == "interrupted"
    assert restarted.status(queued["id"])["status"] == "queued"
    assert not restarted.backend.calls


def test_cancel_queued_and_running_jobs_is_durable(settings, tmp_path):
    started = Event()
    target = workspace(tmp_path)

    def blocking(request, dispatch):
        """A real cancellation event crosses from another manager through SQLite."""
        started.set()
        assert request.cancel.wait(4)
        with pytest.raises(ProjectCancelled):
            dispatch("write_file", {"path": "after-cancel.txt", "content": "must not run"})
        return PiResult(status="done", text="Interrupted")

    manager = ProjectManager(settings, SessionBackend(blocking))
    manager.register("demo", target)
    queued = manager.submit("demo", "cancel before start")
    assert manager.cancel(queued["id"])["status"] == "cancelled"
    assert manager.run(queued["id"])["status"] == "cancelled"
    task = manager.submit("demo", "long work")
    worker = Thread(target=manager.run, args=(task["id"],))
    worker.start()
    assert started.wait(2)
    other = ProjectManager(settings, SessionBackend())
    assert other.cancel(task["id"])["cancel_requested"]
    worker.join(5)
    assert not worker.is_alive()
    assert other.status(task["id"])["status"] == "cancelled"
    assert not other.status(task["id"])["verified"]
    assert not (target / "after-cancel.txt").exists()


def test_pi_project_receives_host_file_results_and_independent_verification(settings, tmp_path):
    """Pi receives actual tool outcomes while the host verifies the resulting files."""
    target = workspace(tmp_path)
    receipts = []

    def implement(request, dispatch):
        """Simulate SDK tool RPCs without recreating model-turn control in Python."""
        receipts.append(dispatch("write_file", {"path": "greet.py", "content": "print('PI_PROJECT')\n"}))
        receipts.append(dispatch("read_file", {"path": "greet.py"}))
        return PiResult(status="done", text="Created and inspected greet.py.")

    backend = SessionBackend(implement)
    manager = ProjectManager(settings, backend)
    manager.register("demo", target, verification=[[sys.executable, "greet.py"]])
    result = manager.run(manager.submit("demo", "write a script")["id"])
    assert result["status"] == "done" and result["verified"]
    assert "PI_PROJECT" in result["evidence"][0]["stdout"]
    assert receipts[0]["ok"] and "PI_PROJECT" in receipts[1]["content"]
    assert len(backend.calls) == 1
    assert [item["data"]["name"] for item in manager.store.events(result["id"])
            if item["kind"] == "tool"] == ["write_file", "read_file"]


def test_pi_tools_confine_paths_and_deny_unrestricted_commands(settings, tmp_path):
    target = workspace(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    (target / "link").symlink_to(outside)
    (target / ".git").mkdir()
    (target / ".git" / "config").write_text("git metadata")
    (target / "alias").symlink_to(target / ".git", target_is_directory=True)
    manager = ProjectManager(settings, SessionBackend())
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


def test_pi_tool_denial_returns_feedback_and_step_budget_is_forwarded(settings, tmp_path):
    """Tool refusals reach Pi, whose session owns the configured turn budget."""
    receipts = []

    def refuse(request, dispatch):
        """The transport returns the SDK's failed terminal state after tool denials."""
        receipts.append(dispatch("read_file", {"path": "../private"}))
        receipts.append(dispatch("not_a_tool", {}))
        return PiResult(status="failed", error="Pi reached its step limit")

    backend = SessionBackend(refuse)
    limited = settings.model_copy(update={"runtime_project_max_steps": 2})
    manager = ProjectManager(limited, backend)
    manager.register("demo", workspace(tmp_path))
    result = manager.run(manager.submit("demo", "try tools")["id"])
    assert result["status"] == "failed" and "step limit" in result["error"]
    assert all(not item["ok"] and item["error"] for item in receipts)
    assert backend.limits == [2] and len(backend.calls) == 1


def test_registered_command_runner_has_timeout_and_cancellation(settings, tmp_path):
    manager = ProjectManager(settings, SessionBackend())
    target = workspace(tmp_path)
    with pytest.raises(RuntimeError, match="timeout"):
        manager._command(target, [sys.executable, "-c", "import time; time.sleep(5)"], Event(), 0.1)
    cancel = Event()
    cancel.set()
    with pytest.raises(ProjectCancelled):
        manager._command(target, [sys.executable, "-c", "import time; time.sleep(5)"], cancel, 5)


def test_pi_route_deadline_bounds_host_command_and_blocks_later_effects(settings, tmp_path):
    """A short Pi deadline kills a longer command and cannot become verified work."""
    target = workspace(tmp_path)
    scoped = settings.model_copy(update={"runtime_allow_unrestricted": True})
    timed_out = []

    def implement(request, dispatch):
        """Use a real long-running process, then try to continue after expiration."""
        assert request.timeout == 0.2
        with pytest.raises(ProjectTimeout):
            dispatch("run_command", {"argv": [sys.executable, "-c",
                     "import time; from pathlib import Path; time.sleep(2); "
                     "Path('late-process.txt').write_text('too late')"], "timeout": 120})
        timed_out.append(True)
        with pytest.raises(ProjectTimeout):
            dispatch("write_file", {"path": "late-tool.txt", "content": "too late"})
        return PiResult(status="done", text="This expired session cannot succeed.")

    backend = SessionBackend(implement)
    backend.config = BackendConfig(kind="openai", model="fixture", timeout=0.2)
    manager = ProjectManager(scoped, backend)
    manager.register("demo", target, verification=[[
        sys.executable, "-c", "from pathlib import Path; Path('verified.txt').touch()"]])
    task = manager.submit("demo", "try a long command")
    started = time.monotonic()
    result = manager.run(task["id"])
    assert time.monotonic() - started < 1.5
    assert timed_out == [True]
    assert result["status"] == "failed" and "timed out" in result["error"]
    assert not result["verified"] and not result["evidence"] and not result["evidence_path"]
    assert not any((target / filename).exists() for filename in (
        "late-process.txt", "late-tool.txt", "verified.txt"))


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="named pipes require POSIX")
def test_project_file_reader_rejects_named_pipes_without_opening_them(settings, tmp_path, monkeypatch):
    """A zero-byte FIFO must never block a Pi host callback in read_text()."""
    target = workspace(tmp_path)
    pipe = target / "never-open.fifo"
    os.mkfifo(pipe)
    manager = ProjectManager(settings, SessionBackend())

    def forbidden_open(*_args, **_kwargs):
        """Prove the guard rejects the pipe before any potentially blocking open."""
        raise AssertionError("named pipe opened")

    monkeypatch.setattr(Path, "read_text", forbidden_open)
    with pytest.raises(ValueError, match="regular file"):
        manager._tool(target, "read_file", {"path": pipe.name}, Event())


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group regression")
def test_cancellation_kills_child_even_when_leader_exits_on_sigterm(settings, tmp_path):
    manager = ProjectManager(settings, SessionBackend())
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
