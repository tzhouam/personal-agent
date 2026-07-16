"""In multi_tenant, background actions enqueue on the durable queue for the
caller's own uid instead of spawning a detached CLI (multi-user §6, §A.5)."""
import pytest

from assistant.actions import run_action
from assistant.config import Settings
from assistant.jobs import JobQueue


@pytest.fixture
def mt_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "multi_tenant")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return Settings.for_user("alice1")


def _no_popen(monkeypatch):
    from assistant.actions import handlers
    monkeypatch.setattr(handlers.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("multi_tenant must not spawn a CLI"))


def test_trigger_run_enqueues_for_the_caller(mt_settings, monkeypatch):
    _no_popen(monkeypatch)
    msg = run_action("trigger_run", {"resume": True}, mt_settings)
    assert "queued" in msg
    q = JobQueue(mt_settings.shared_dir)
    job = q.claim()
    assert job["uid"] == "alice1" and job["kind"] == "run"
    assert job["args"] == {"resume": True}


def test_execute_task_enqueues(mt_settings, monkeypatch):
    _no_popen(monkeypatch)
    run_action("execute_task", {"request": "book a table"}, mt_settings)
    job = JobQueue(mt_settings.shared_dir).claim()
    assert job["kind"] == "task" and job["args"] == {"request": "book a table"}


def test_run_phase_enqueues_slow_phase(mt_settings, monkeypatch):
    _no_popen(monkeypatch)
    run_action("run_phase", {"phase": "research"}, mt_settings)
    job = JobQueue(mt_settings.shared_dir).claim()
    assert job["kind"] == "run_phase" and job["args"] == {"phase": "research"}


def test_shared_dir_is_deployment_global_not_per_user(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "multi_tenant")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    a = Settings.for_user("alice1")
    b = Settings.for_user("bob123")
    # different per-user data dirs, but ONE shared queue dir
    assert a.data_dir != b.data_dir
    assert a.shared_dir == b.shared_dir == tmp_path / "shared"
