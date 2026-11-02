"""Exercise the daily Pi skill with real checkpoints and fake external services."""

import json
import subprocess
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest

from assistant.agent import orchestrator
from assistant.agent.events_store import EventsStore
from assistant.agent.state import load_state
from assistant.platform.jobs import DeliveryLedger
from assistant.platform.worker import Cancelled


@pytest.fixture
def offline_workflow(settings, monkeypatch):
    """Keep stores and locks real while replacing every external-work boundary."""
    settings.github_token = ""
    settings.marks_repo = ""
    settings.deployment_mode = "single_user"
    settings.profile_dir.mkdir(parents=True)
    (settings.profile_dir / "profile.yaml").write_text("{}\n")
    calls, rendered, profile_observations = [], [], []
    observation = {"source": "fixture", "kind": "commit", "title": "local work"}
    notification = {"id": "thread-1", "reason": "mention",
                    "updated_at": "2026-11-01T12:00:00Z"}

    class Collector:
        """Produce fixed input without contacting an account or reading live data."""

        def collect(self, since):
            """Record collection so resumed runs can prove they skip completed work."""
            calls.append("collect")
            return [observation]

        def fetch_notifications(self, since):
            """Return one thread whose seen marker must follow successful delivery."""
            return [notification]

    def profile(llm, store, observations):
        """Capture the evidence passed through durable artifacts on resume."""
        calls.append("profile")
        profile_observations.append(observations)
        return {"profile_diff": "updated", "profile_ops": [], "rejected_ops": []}

    def resume(*args, **kwargs):
        """Avoid any resume repository or remote mutation."""
        calls.append("resume")
        return {"status": "unchanged"}

    def digest(llm, profile, notifications, observations):
        """Preserve the real seen-store's input in the generated digest."""
        calls.append("digest")
        return {"sections": {"red": notifications, "yellow": [], "white": []},
                "total": len(notifications)}

    def todos(*args, **kwargs):
        """Avoid model and GitHub auto-close calls."""
        calls.append("todos")
        return {"added": [], "closed": [], "open": [], "open_count": 0}

    def research(*args, **kwargs):
        """Provide a research seen id without reading remote feeds."""
        calls.append("research")
        return {"papers": [], "industry": [], "chinese": [],
                "source_health": {}, "seen_ids": ["paper-1"]}

    def website(*args, **kwargs):
        """Avoid website publication while retaining its phase result."""
        calls.append("website")
        return {"status": "unchanged"}

    def render(*args, **kwargs):
        """Capture the assembled durable inputs and error statistics."""
        rendered.append({"digest": args[1], "stats": args[9]})
        return "<html>offline digest</html>"

    def send(*args, **kwargs):
        """Replace outbound mail with a recorded success."""
        calls.append("deliver")
        return "fixture"

    def curate(store):
        """Record housekeeping without changing profile memory."""
        calls.append("curate")
        return {"decayed": []}

    monkeypatch.setattr(orchestrator, "LLM", lambda settings: object())
    monkeypatch.setattr(orchestrator, "REGISTRY", {"fixture": lambda settings: Collector()})
    monkeypatch.setattr(orchestrator, "update_profile", profile)
    monkeypatch.setattr(orchestrator, "sync_resume", resume)
    monkeypatch.setattr(orchestrator, "build_digest", digest)
    monkeypatch.setattr(orchestrator, "update_todos", todos)
    monkeypatch.setattr(orchestrator, "run_research", research)
    monkeypatch.setattr(orchestrator, "sync_website", website)
    monkeypatch.setattr(orchestrator, "render_html", render)
    monkeypatch.setattr(orchestrator, "build_health", lambda *args: {})
    monkeypatch.setattr(orchestrator, "render_health_html", lambda *args: "")
    monkeypatch.setattr(orchestrator, "send_email", send)
    monkeypatch.setattr(orchestrator, "announce_digest", lambda *args: "disabled")
    monkeypatch.setattr(orchestrator, "curate", curate)
    monkeypatch.setattr("assistant.agent.marks.collect_marks", lambda *args: {"applied": 0})
    return SimpleNamespace(settings=settings, calls=calls, observation=observation,
                           notification=notification, rendered=rendered,
                           profile_observations=profile_observations)


def test_daily_workflow_runs_without_langgraph_installed():
    """Importing the production skill must not require the removed graph package."""
    script = """
import importlib.abc
import sys
class BlockLangGraph(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'langgraph' or fullname.startswith('langgraph.'):
            raise ImportError('LangGraph is unavailable')
sys.meta_path.insert(0, BlockLangGraph())
from assistant.agent.orchestrator import DailyWorkflow
assert DailyWorkflow.__module__ == 'assistant.agent.orchestrator'
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_workflow_preserves_all_phases_and_additive_errors(offline_workflow, monkeypatch):
    """Independent degraded phases retain earlier errors through successful completion."""
    env = offline_workflow

    class BrokenCollector:
        """Fail independently of the successful observation collector."""

        def collect(self, since):
            """Simulate an unavailable input service."""
            raise RuntimeError("feed unavailable")

    def broken_profile(*args):
        """Simulate a model boundary failure, without invoking a model."""
        env.calls.append("profile")
        raise RuntimeError("profile unavailable")

    monkeypatch.setitem(orchestrator.REGISTRY, "broken", lambda settings: BrokenCollector())
    monkeypatch.setattr(orchestrator, "update_profile", broken_profile)
    deps = orchestrator.Deps(env.settings, "offline-phases")
    initial = {"run_id": "offline-phases", "phase": "collect", "dry_run": True,
               "errors": ["earlier error"]}
    try:
        states = list(orchestrator.build_graph(deps).stream(initial))
        final = states[-1]
        assert [state["phase"] for state in states] == [
            "collect", "profile", "resume", "digest", "todos", "research",
            "website", "deliver", "curate", "done"]
        assert final["errors"] == ["earlier error", "collect/broken: feed unavailable",
                                   "profile: profile unavailable"]
        assert initial["errors"] == ["earlier error"]
        assert states[0]["errors"] == ["earlier error"]
        assert env.calls == ["collect", "profile", "resume", "digest", "todos",
                             "research", "website", "curate"]
        assert "profile: profile unavailable" in env.rendered[-1]["stats"]["errors"]
        assert load_state(env.settings.state_file)["phase"] == "done"
        # Non-streaming callers retain the same resumable entry point.
        final = orchestrator.build_graph(deps).invoke({"run_id": "offline-phases",
                                                     "phase": "curate", "errors": []})
        assert final["phase"] == "done"
    finally:
        deps.events.close()


def test_delivery_failure_resumes_only_unsent_work(offline_workflow, monkeypatch):
    """Failed mail releases its claim and seen markers, then resume uses saved inputs."""
    env = offline_workflow
    env.settings.deployment_mode = "multi_tenant"

    def fail_send(*args, **kwargs):
        """Exercise the real delivery failure checkpoint and ledger release path."""
        env.calls.append("deliver")
        raise RuntimeError("mail unavailable")

    monkeypatch.setattr(orchestrator, "send_email", fail_send)
    assert orchestrator.run(env.settings) == 1
    checkpoint = load_state(env.settings.state_file)
    assert checkpoint["phase"] == "deliver"
    assert "curate" not in env.calls
    ledger = DeliveryLedger(env.settings.data_dir)
    assert not ledger.was_delivered("digest", datetime.now().strftime("%Y-%m-%d"))
    events = EventsStore(env.settings.events_db)
    try:
        ids = [orchestrator.seen_key(env.notification), "paper-1"]
        assert events.filter_unseen(ids) == ids
    finally:
        events.close()
    assert (env.settings.runs_dir / checkpoint["run_id"] / "digest.html").exists()

    def succeed_send(*args, **kwargs):
        """Finish the same delivery without running already completed phases."""
        env.calls.append("deliver")
        return "fixture"

    monkeypatch.setattr(orchestrator, "send_email", succeed_send)
    completed_calls = list(env.calls)
    assert orchestrator.run(env.settings, resume=True) == 0
    assert env.calls == [*completed_calls, "deliver", "curate"]
    assert load_state(env.settings.state_file) == {**checkpoint, "phase": "done"}
    assert env.rendered[-1]["stats"]["observations"] == 1
    assert env.rendered[-1]["digest"]["sections"]["red"] == [env.notification]
    events = EventsStore(env.settings.events_db)
    try:
        assert events.filter_unseen(ids) == []
    finally:
        events.close()
    assert ledger.was_delivered("digest", datetime.now().strftime("%Y-%m-%d"))

    # A queued replay after successful delivery must not send a second email.
    checkpoint_path = env.settings.state_file
    checkpoint_path.write_text(json.dumps({**checkpoint, "phase": "deliver"}))
    assert orchestrator.run(env.settings, resume=True) == 0
    assert env.calls.count("deliver") == 2


def test_cancelled_workflow_releases_lock_and_rehydrates_completed_phase(offline_workflow):
    """Cancel after collection, then resume from its artifact without collecting twice."""
    env = offline_workflow
    boundaries = []

    def cancel_after_collect():
        """Cancel at the first completed phase's already persisted checkpoint."""
        boundaries.append(load_state(env.settings.state_file)["phase"])
        if boundaries[-1] == "profile":
            raise Cancelled()

    with pytest.raises(Cancelled):
        orchestrator.run(env.settings, cancel_check=cancel_after_collect)
    checkpoint = load_state(env.settings.state_file)
    assert boundaries == ["collect", "profile"]
    assert checkpoint["phase"] == "profile"
    assert env.calls == ["collect"]
    # The resumed run succeeding also proves the cancelled run released run.lock.
    assert orchestrator.run(env.settings, resume=True, dry_run=True) == 0
    assert env.calls.count("collect") == 1
    assert env.profile_observations == [[env.observation]]
    assert load_state(env.settings.state_file) == {**checkpoint, "phase": "done"}
