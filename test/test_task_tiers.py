"""Adaptive task execution depth: tier assessment, per-tier planning/MoA
behavior, the action-level approval gate, and the atomic approve→resume
lifecycle (task_runner.py; DESIGN §5)."""

import json

import pytest

import assistant.notify as notify
from assistant.actions.base import Action
from assistant.actions.handlers import _approve_task
from assistant.actions.registry import ACTIONS, is_risky
from assistant.task_runner import TASK_ID_RE, run_task


class ScriptedLLM:
    """complete_json returns the scripted responses in order and records every
    call's kwargs, so tests can assert single-model (mixture=False) usage."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete_json(self, prompt, system=None, **kw):
        self.calls.append({"prompt": prompt, "system": system, **kw})
        assert self.responses, "LLM called more times than scripted"
        return self.responses.pop(0)


@pytest.fixture
def sent(monkeypatch):
    """Capture WeChat pushes."""
    box = []
    monkeypatch.setattr(notify, "send_wechat", lambda s, text: box.append(text) or "sent")
    return box


@pytest.fixture
def risky_action(monkeypatch):
    """A synthetic outward action so tests never run a real publish/reboot."""
    fired = []
    action = Action(name="publish_test", description="test publish",
                    handler=lambda s, p: fired.append(p) or "published!",
                    llm=True, prompt_example='{"type": "publish_test"}', risky=True)
    monkeypatch.setitem(ACTIONS, "publish_test", action)
    return fired


def _assess(tier, **flags):
    base = {"external_side_effects": False, "mutates_finance_or_health": False,
            "publishes": False, "ambiguous": False, "long_running": False}
    base.update(flags)
    return {"tier": tier, "flags": base}


def test_simple_tier_no_plan_no_moa_small_budget(settings, sent):
    llm = ScriptedLLM([
        _assess("simple"),
        {"thought": "add it", "action": {"type": "add_todo", "title": "call mom"}},
        {"thought": "done", "finish": "added the todo"},
    ])
    record = run_task("help me remember to call mom", settings, llm=llm, notify=False)
    assert record["status"] == "done" and record["tier"] == "simple"
    assert record.get("plan") is None                       # no plan drafted
    assert all(c.get("mixture") is False for c in llm.calls)  # never pays MoA
    assert TASK_ID_RE.match(record["id"])                   # collision-safe id shape


def test_medium_tier_drafts_single_model_plan(settings):
    llm = ScriptedLLM([
        _assess("medium"),
        {"steps": ["search options", "add a todo"], "verify": "todo exists",
         "risks": "none"},
        {"thought": "done", "finish": "planned and done"},
    ])
    record = run_task("compare two options and track the winner", settings,
                      llm=llm, notify=False)
    assert record["tier"] == "medium"
    assert [m["step"] for m in record["plan"]["milestones"]] == \
        ["search options", "add a todo"]
    assert all(c.get("mixture") is False for c in llm.calls)  # plan + loop single-model


def test_complex_tier_moa_plan_and_milestone_tracking(settings, tmp_path):
    llm = ScriptedLLM([
        _assess("complex"),
        {"steps": ["research", "summarize"], "verify": "report cites findings",
         "risks": "stale info"},
        {"thought": "research", "action": {"type": "add_todo", "title": "notes"},
         "milestone_done": 1},
        {"thought": "wrap", "finish": "summary...", "milestone_done": 2},
    ])
    record = run_task("multi step effort", settings, llm=llm, notify=False)
    assert record["status"] == "done" and record["tier"] == "complex"
    # the complex plan call runs on the configured task role — MoA allowed
    plan_call = llm.calls[1]
    assert plan_call.get("mixture") is not False
    # milestones ticked and persisted
    saved = json.loads((settings.data_dir / "tasks" / f"{record['id']}.json").read_text())
    assert [m["done"] for m in saved["plan"]["milestones"]] == [True, True]
    assert "Verify before finishing" in llm.calls[3]["prompt"]


def test_risky_action_pauses_even_simple_tier(settings, sent, risky_action):
    """The dispatch gate is the boundary: a risky action pauses an unapproved
    task at ANY tier — nothing executes, the owner is asked."""
    llm = ScriptedLLM([
        _assess("simple"),
        {"thought": "publish it", "action": {"type": "publish_test"}},
    ])
    record = run_task("do the thing", settings, llm=llm, notify=True)
    assert record["status"] == "awaiting_approval"
    assert record["pending_action"] == {"type": "publish_test"}
    assert risky_action == []                              # never executed
    assert sent and "批准任务 " + record["id"] in sent[0]


def test_keyword_clamp_forces_complex_and_prepause(settings, sent):
    """Deterministic clamp: publishing markers raise the tier and pause before
    the first step, whatever the model claimed."""
    llm = ScriptedLLM([
        _assess("simple"),                                  # model lowballs it
        {"steps": ["render", "publish 网站"], "verify": "site updated", "risks": ""},
    ])
    record = run_task("帮我发布网站更新", settings, llm=llm, notify=True)
    assert record["tier"] == "complex"
    assert record["status"] == "awaiting_approval"
    assert record["steps"] == []                            # nothing ran
    assert sent and "批准任务" in sent[0]


def test_approve_then_resume_runs_pending_action_once(settings, sent, risky_action,
                                                      monkeypatch):
    llm = ScriptedLLM([
        _assess("simple"),
        {"thought": "publish", "action": {"type": "publish_test"}},
    ])
    paused = run_task("do the thing", settings, llm=llm, notify=False)
    task_id = paused["id"]

    spawns = []
    monkeypatch.setattr("assistant.actions.handlers.subprocess.Popen",
                        lambda *a, **k: spawns.append(a))
    out = _approve_task(settings, {"id": task_id})
    assert "approved" in out and len(spawns) == 1
    # a second approval re-dispatches (orphan rescue) — the locked
    # queued→running transition in _load_approved makes the loser a no-op,
    # so a double-RUN still can't happen (covered below by the resume test)
    assert "approved" in _approve_task(settings, {"id": task_id})
    assert len(spawns) == 2

    resume_llm = ScriptedLLM([{"thought": "done", "finish": "published and done"}])
    record = run_task("", settings, llm=resume_llm, approved_task_id=task_id,
                      notify=False)
    assert record["status"] == "done"
    assert risky_action == [{"type": "publish_test"}]       # executed exactly once
    assert any(s.get("outcome") == "published!" for s in record["steps"])


def test_approve_rejects_malformed_and_unknown_ids(settings):
    assert "no task" in _approve_task(settings, {"id": "../../etc/passwd"})
    assert "no task" in _approve_task(settings, {"id": "task-20260101-000000-abcdef"})


def test_terminal_task_never_replays(settings, sent):
    llm = ScriptedLLM([_assess("simple"),
                       {"thought": "done", "finish": "all good"}])
    record = run_task("quick one", settings, llm=llm, notify=False)
    assert record["status"] == "done"
    replay = run_task("", settings, llm=ScriptedLLM([]),
                      approved_task_id=record["id"], notify=False)
    assert replay["status"] == "error"                      # replayed job refuses


def test_approve_task_excluded_inside_task_loop(settings):
    llm = ScriptedLLM([
        _assess("simple"),
        {"thought": "sneaky", "action": {"type": "approve_task",
                                         "id": "task-20260101-000000-abcdef"}},
        {"thought": "ok", "finish": "gave up"},
    ])
    record = run_task("approve my own task", settings, llm=llm, notify=False)
    assert any("not available inside a task" in str(s.get("outcome"))
               for s in record["steps"])


def test_is_risky_metadata():
    assert is_risky("reboot", {})
    assert is_risky("run_phase", {"phase": "website"})
    assert not is_risky("run_phase", {"phase": "research"})
    assert not is_risky("add_todo", {"title": "x"})
    assert not is_risky("nonexistent", {})