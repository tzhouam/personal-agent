"""Focused test for the relocated agent-side job dispatch.

The worker suite injects fake dispatch maps and never runs the real handlers, so
the relocation from `worker.py` to `agent/dispatch.py` is otherwise unverified.
This exercises `build_dispatch()` directly: every handler's lazy import must
resolve at the new package depth, and the handler must call its agent entry
point. This is the test that catches a missing `import os` or a wrong
`__file__` depth.
"""

import types

import pytest

from assistant.agent.dispatch import build_dispatch

KINDS = {"run", "run_phase", "task", "build_website", "evolve", "global_evolve", "self_improve"}


class _Token:
    """Stub CancelToken: check() is a no-op so handlers run to completion."""

    def check(self):
        pass


def test_build_dispatch_exposes_exactly_the_expected_kinds():
    assert set(build_dispatch()) == KINDS


def test_run_handler_invokes_orchestrator(monkeypatch):
    called = {}
    import assistant.agent.orchestrator as orch
    monkeypatch.setattr(orch, "run",
                        lambda settings, **kw: called.update(settings=settings, kw=kw))
    build_dispatch()["run"]("S", {"resume": False}, _Token())
    assert called["settings"] == "S"
    assert called["kw"]["resume"] is False


def test_run_phase_handler_invokes_cmd_run_phase(monkeypatch):
    seen = {}
    import assistant.cli.commands as cmds
    monkeypatch.setattr(cmds, "cmd_run_phase",
                        lambda settings, phase: seen.update(settings=settings, phase=phase))
    build_dispatch()["run_phase"]("S", {"phase": "research"}, _Token())
    assert seen == {"settings": "S", "phase": "research"}


def test_task_handler_invokes_run_task(monkeypatch):
    seen = {}
    import assistant.agent.task_runner as tr
    monkeypatch.setattr(tr, "run_task",
                        lambda request, settings, **kw: seen.update(request=request, kw=kw))
    build_dispatch()["task"]("S", {"request": "do a thing"}, _Token())
    assert seen["request"] == "do a thing"


def test_evolve_and_global_evolve_handlers(monkeypatch):
    import assistant.agent.tasks.evolve as ev
    import assistant.agent.tasks.global_evolve as gev
    hits = []
    monkeypatch.setattr(ev, "evolve", lambda settings: hits.append(("evolve", settings)))
    monkeypatch.setattr(gev, "global_evolve", lambda settings: hits.append(("global", settings)))
    d = build_dispatch()
    d["evolve"]("S1", {}, _Token())
    d["global_evolve"]("S2", {}, _Token())
    assert hits == [("evolve", "S1"), ("global", "S2")]


def test_self_improve_resolves_script_path_and_runs(monkeypatch):
    """Proves `import os` is present and the repo-root depth (`parents[3]`) is
    right — a bad path would point under src/ and the assertion would catch it."""
    captured = {}

    def fake_run(cmd, env=None, **kw):
        captured["script"] = cmd[1]
        captured["env"] = env
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    build_dispatch()["self_improve"](None, {"days": 3}, _Token())
    assert captured["script"].endswith("scripts/self-improve.sh")
    # repo root, not somewhere under src/assistant/
    assert "/src/" not in captured["script"]
    assert captured["env"]["SELF_IMPROVE_DAYS"] == "3"


def test_self_improve_raises_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr("subprocess.run",
                        lambda *a, **k: types.SimpleNamespace(returncode=1, stdout="x", stderr="y"))
    with pytest.raises(RuntimeError):
        build_dispatch()["self_improve"](None, {}, _Token())


# ── exit-code propagation (the pipeline job's failure signal) ──────────────
#
# `_dispatch_run` used to call `run(...)` and discard its return value, so the
# worker marked every daily run `done` regardless of outcome. Because the same
# handler defaults resume=True, a run stuck at `deliver` was then resumed the
# next day — jumping straight to deliver/curate with yesterday's artifacts, so
# day N+1 collected nothing either. One deliver failure ate two days silently.


def test_run_handler_raises_when_the_pipeline_stops_short(monkeypatch, tmp_path):
    """rc=1 (graph never reached `done`) must raise, so the queue's retry
    ladder runs and the failure can reach the owner."""
    import assistant.agent.orchestrator as orch
    import assistant.agent.state as state_mod

    monkeypatch.setattr(orch, "run", lambda settings, **kw: 1)
    monkeypatch.setattr(state_mod, "load_state", lambda f: {"phase": "deliver"})
    settings = types.SimpleNamespace(state_file=tmp_path / "state.json")

    with pytest.raises(RuntimeError, match="deliver"):
        build_dispatch()["run"](settings, {}, _Token())


def test_run_handler_treats_lock_conflict_as_a_clean_skip(monkeypatch):
    """rc=3 means another run already holds run.lock — it did this job's work.
    Raising would burn the retry ladder and push a false failure note."""
    import assistant.agent.orchestrator as orch

    monkeypatch.setattr(orch, "run", lambda settings, **kw: 3)
    build_dispatch()["run"]("S", {}, _Token())      # must not raise


def test_run_phase_handler_raises_on_nonzero_exit(monkeypatch):
    import assistant.cli.commands as cmds

    monkeypatch.setattr(cmds, "cmd_run_phase", lambda settings, phase: 1)
    with pytest.raises(RuntimeError, match="bogus"):
        build_dispatch()["run_phase"]("S", {"phase": "bogus"}, _Token())
