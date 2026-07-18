"""Owner-authored reusable workflows: store semantics (fail-closed,
never-delete, idempotent run accounting), the five registry actions, the
record-first execution path with per-action approval, routine binding, and
tenant isolation."""

import json
import threading

import pytest

import assistant.notify as notify
from assistant.actions import run_action
from assistant.actions.handlers import _approve_task
from assistant.routines import RoutineStore, fire_due
from assistant.task_runner import TASK_ID_RE, run_task
from assistant.workflow_store import WorkflowStore, WorkflowStoreError, valid_steps


class ScriptedLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete_json(self, prompt, system=None, **kw):
        self.calls.append({"prompt": prompt, "system": system, **kw})
        assert self.responses, "LLM called more times than scripted"
        return self.responses.pop(0)


@pytest.fixture
def sent(monkeypatch):
    box = []
    monkeypatch.setattr(notify, "send_wechat", lambda s, text: box.append(text) or "sent")
    return box


def _mk(settings, name="周报", steps=("collect notes", "write summary")):
    status, wf = WorkflowStore(settings.profile_dir).create(
        name, "weekly report procedure", list(steps), verify="report has numbers")
    assert status == "created"
    return wf


# ── store semantics ──────────────────────────────────────────────────

def test_store_lifecycle_caps_and_name_rules(settings):
    store = WorkflowStore(settings.profile_dir)
    wf = _mk(settings)
    assert wf["id"] == "wf1" and store.get("wf1")["name"] == "周报"
    # duplicate active name (case-insensitive) returns the existing one
    assert store.create("周报", "again", ["x"])[0] == "duplicate"
    # invalid steps shapes rejected
    assert store.create("n2", "d", [])[0] == "invalid"
    assert store.create("n2", "d", ["ok", ""])[0] == "invalid"
    assert store.create("n2", "d", "not-a-list")[0] == "invalid"
    assert store.create("n2", "d", ["s"] * 7)[0] == "invalid"
    # update + rename conflict
    other = store.create("其他", "d", ["a"])[1]
    assert store.update(other["id"], name="周报")[0] == "conflict"
    assert store.update("wf1", steps=["one", "two", "three"])[0] == "updated"
    assert len(store.get("wf1")["steps"]) == 3
    # retire: never deleted, name becomes reusable, get() hides, get_any keeps
    assert store.retire("wf1")
    assert store.get("wf1") is None and store.get_any("wf1")["status"] == "retired"
    assert store.create("周报", "new gen", ["a"])[0] == "created"
    assert store.update("wf1", name="x")[0] == "missing"   # retired not editable


def test_store_fails_closed_on_bad_yaml(settings):
    store = WorkflowStore(settings.profile_dir)
    _mk(settings)
    store.path.write_text("workflows: [1, 2\nnext_id: oops")   # unparseable
    with pytest.raises(WorkflowStoreError):
        store.load()
    with pytest.raises(WorkflowStoreError):
        store.create("n", "d", ["s"])
    assert "workflows: [1, 2" in store.path.read_text()   # file preserved
    # valid YAML, wrong shape → also closed
    store.path.write_text("- just\n- a list\n")
    with pytest.raises(WorkflowStoreError):
        store.load()


def test_mark_ran_idempotent_and_concurrent(settings):
    store = WorkflowStore(settings.profile_dir)
    wf = _mk(settings)
    assert store.mark_ran(wf["id"], "done", "task-20260718-000000-abc001")
    assert store.mark_ran(wf["id"], "done", "task-20260718-000000-abc001")  # replay
    assert store.get(wf["id"])["run_count"] == 1          # counted once
    threads = [threading.Thread(target=store.mark_ran,
                                args=(wf["id"], "partial", f"task-20260718-00000{i}-abc00{i}"))
               for i in range(2, 5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert store.get(wf["id"])["run_count"] == 4          # serialized, no loss
    assert store.get(wf["id"])["last_status"] == "partial"
    assert valid_steps(wf["steps"])                       # unchanged by runs


# ── actions ──────────────────────────────────────────────────────────

def test_create_and_show_actions(settings):
    out = run_action("create_workflow", {
        "name": "报价跟进", "description": "对比报价并跟进",
        "steps": ["search suppliers", "compare quotes", "add todo"],
        "verify": "todo exists"}, settings)
    assert "workflow saved" in out and "wf1" in out
    # steps must be a real list — repair-round-friendly outcome otherwise
    bad = run_action("create_workflow",
                     {"name": "x", "description": "d", "steps": "step one"}, settings)
    assert "rejected" in bad and "steps" in bad
    shown = run_action("show_workflow", {"id": "wf1"}, settings)
    assert "compare quotes" in shown and "verify: todo exists" in shown
    assert "no workflow" in run_action("show_workflow", {"id": "wf99"}, settings)


def test_run_workflow_mints_queued_record_and_dispatches(settings, monkeypatch):
    wf = _mk(settings)
    spawns = []
    monkeypatch.setattr("assistant.actions.handlers.subprocess.Popen",
                        lambda *a, **k: spawns.append(a[0]))
    out = run_action("run_workflow", {"id": wf["id"]}, settings)
    assert "started" in out and len(spawns) == 1
    assert "--approved-id" in spawns[0]
    task_id = spawns[0][spawns[0].index("--approved-id") + 1]
    assert TASK_ID_RE.match(task_id)
    record = json.loads((settings.data_dir / "tasks" / f"{task_id}.json").read_text())
    assert record["status"] == "queued" and record["workflow_id"] == wf["id"]
    assert [m["step"] for m in record["plan"]["milestones"]] == list(wf["steps"])
    # traversal/unknown ids refused before any path is built
    assert "needs the workflow id" in run_action(
        "run_workflow", {"id": "../evil"}, settings)
    assert "no active workflow" in run_action(
        "run_workflow", {"id": "wf99"}, settings)


def test_resume_executes_preset_plan_without_drafting(settings, monkeypatch, sent):
    wf = _mk(settings, steps=("add a todo", "finish up"))
    spawns = []
    monkeypatch.setattr("assistant.actions.handlers.subprocess.Popen",
                        lambda *a, **k: spawns.append(a[0]))
    run_action("run_workflow", {"id": wf["id"]}, settings)
    task_id = spawns[0][spawns[0].index("--approved-id") + 1]

    llm = ScriptedLLM([
        {"tier": "simple", "flags": {}},   # assessment (clamped to medium below)
        {"thought": "step 1", "action": {"type": "add_todo", "title": "notes"},
         "milestone_done": 1},
        {"thought": "wrap", "finish": "report ready", "milestone_done": 2},
    ])
    record = run_task("", settings, llm=llm, approved_task_id=task_id, notify=False)
    assert record["status"] == "done" and record["completion"] == "full"
    assert record["tier"] == "medium"                    # workflow clamp persisted
    assert record["assessment"]["tier"] == "medium"
    # exactly 3 LLM calls: assess + 2 turns — NO plan-drafting call
    assert len(llm.calls) == 3
    ran = WorkflowStore(settings.profile_dir).get(wf["id"])
    assert ran["run_count"] == 1 and ran["last_status"] == "done"
    assert ran["last_task_id"] == task_id


def test_workflow_risky_step_pauses_each_time(settings, monkeypatch, sent):
    """Per-action approval: two risky steps pause twice; each approval
    releases exactly one."""
    from assistant.actions.base import Action
    from assistant.actions.registry import ACTIONS

    fired = []
    monkeypatch.setitem(ACTIONS, "publish_test", Action(
        name="publish_test", description="t", llm=True, risky=True,
        handler=lambda s, p: fired.append(dict(p)) or "published!",
        prompt_example='{"type": "publish_test"}'))
    spawns = []
    monkeypatch.setattr("assistant.actions.handlers.subprocess.Popen",
                        lambda *a, **k: spawns.append(a[0]))
    wf = _mk(settings, steps=("publish once", "publish twice"))
    run_action("run_workflow", {"id": wf["id"]}, settings)
    task_id = spawns[0][spawns[0].index("--approved-id") + 1]

    llm1 = ScriptedLLM([{"tier": "medium", "flags": {}},
                        {"thought": "p1", "action": {"type": "publish_test", "n": 1}}])
    paused = run_task("", settings, llm=llm1, approved_task_id=task_id, notify=True)
    assert paused["status"] == "awaiting_approval" and fired == []

    assert "approved" in _approve_task(settings, {"id": task_id})
    llm2 = ScriptedLLM([
        {"thought": "p2", "action": {"type": "publish_test", "n": 2}},  # 2nd risky
    ])
    paused2 = run_task("", settings, llm=llm2, approved_task_id=task_id, notify=True)
    assert fired == [{"type": "publish_test", "n": 1}]    # exactly the approved one
    assert paused2["status"] == "awaiting_approval"       # pauses AGAIN
    assert paused2["pending_action"]["n"] == 2

    assert "approved" in _approve_task(settings, {"id": task_id})
    llm3 = ScriptedLLM([
        {"thought": "done", "finish": "all published", "milestone_done": 1},
        # first finish is nudged (milestone 2 unticked) — finish again
        {"thought": "explained", "finish": "published; step 2 covered by step 1"},
    ])
    final = run_task("", settings, llm=llm3, approved_task_id=task_id, notify=False)
    assert [f["n"] for f in fired] == [1, 2]
    assert final["status"] == "done" and final["completion"] == "partial"


def test_finish_nudge_once_for_unticked_milestones(settings, monkeypatch, sent):
    wf = _mk(settings, steps=("s1", "s2", "s3"))
    spawns = []
    monkeypatch.setattr("assistant.actions.handlers.subprocess.Popen",
                        lambda *a, **k: spawns.append(a[0]))
    run_action("run_workflow", {"id": wf["id"]}, settings)
    task_id = spawns[0][spawns[0].index("--approved-id") + 1]
    llm = ScriptedLLM([
        {"tier": "medium", "flags": {}},
        {"thought": "lazy", "finish": "done!"},              # nudged
        {"thought": "ok fine", "finish": "done, s2/s3 not needed because X"},
    ])
    record = run_task("", settings, llm=llm, approved_task_id=task_id, notify=False)
    assert record["status"] == "done" and record["completion"] == "partial"
    assert any("finish rejected once" in str(s.get("outcome"))
               for s in record["steps"])
    assert WorkflowStore(settings.profile_dir).get(wf["id"])["last_status"] == "partial"


def test_retired_workflow_task_cancels_and_crash_recovery_resumes(settings, monkeypatch):
    wf = _mk(settings)
    spawns = []
    monkeypatch.setattr("assistant.actions.handlers.subprocess.Popen",
                        lambda *a, **k: spawns.append(a[0]))
    run_action("run_workflow", {"id": wf["id"]}, settings)
    task_id = spawns[0][spawns[0].index("--approved-id") + 1]
    run_action("retire_workflow", {"id": wf["id"]}, settings)
    record = run_task("", settings, llm=ScriptedLLM([]),
                      approved_task_id=task_id, notify=False)
    assert record["status"] == "cancelled" and "retired" in record["report"]

    # crash recovery: a record left `running` resumes only with force_resume
    wf2 = _mk(settings, name="second")
    run_action("run_workflow", {"id": wf2["id"]}, settings)
    task2 = spawns[1][spawns[1].index("--approved-id") + 1]
    path = settings.data_dir / "tasks" / f"{task2}.json"
    rec = json.loads(path.read_text())
    rec["status"] = "running"                              # dead-worker leftover
    path.write_text(json.dumps(rec))
    refused = run_task("", settings, llm=ScriptedLLM([]),
                       approved_task_id=task2, notify=False)
    assert refused["status"] == "error"                    # normal path refuses
    llm = ScriptedLLM([{"tier": "medium", "flags": {}},
                       {"thought": "done", "finish": "ok", "milestone_done": 1},
                       {"thought": "again", "finish": "ok (step 2 explained)"}])
    resumed = run_task("", settings, llm=llm, approved_task_id=task2,
                       notify=False, force_resume=True)
    assert resumed["status"] == "done"


def test_routine_binding_fires_deterministically(settings, monkeypatch, sent):
    wf = _mk(settings)
    spawns = []
    monkeypatch.setattr("assistant.actions.handlers.subprocess.Popen",
                        lambda *a, **k: spawns.append(a[0]))
    out = run_action("create_routine", {"task": "", "time": "00:00",
                                        "days": "daily", "workflow": wf["id"]}, settings)
    assert "routine rt1 created" in out
    routine = RoutineStore(settings.data_dir).active()[0]
    assert routine["workflow"] == wf["id"] and "run workflow" in routine["task"]

    from datetime import datetime
    fired = fire_due(settings, now=datetime.now().replace(hour=23, minute=59))
    assert fired and fired[0]["fired"] and wf["id"] in fired[0]["note"]
    assert len(spawns) == 1                                # dispatched, not chatted
    # retire cancels the bound routine
    out = run_action("retire_workflow", {"id": wf["id"]}, settings)
    assert "rt1" in out
    assert RoutineStore(settings.data_dir).active() == []
    # invalid workflow id on create_routine rejected
    assert "couldn't create" in run_action(
        "create_routine", {"task": "x", "time": "08:00", "workflow": "nope"}, settings)


def test_two_tenant_isolation(tmp_path):
    from assistant.config import Settings

    a = Settings(_env_file=None, data_dir=tmp_path / "users" / "aa11aa11")
    b = Settings(_env_file=None, data_dir=tmp_path / "users" / "bb22bb22")
    wa = WorkflowStore(a.profile_dir).create("mine", "a's", ["s1"])[1]
    wb = WorkflowStore(b.profile_dir).create("mine", "b's", ["s1", "s2"])[1]
    assert wa["id"] == wb["id"] == "wf1"                   # same id, separate stores
    assert WorkflowStore(a.profile_dir).get("wf1")["description"] == "a's"
    assert WorkflowStore(b.profile_dir).get("wf1")["description"] == "b's"
    assert WorkflowStore(a.profile_dir).retire("wf1")
    assert WorkflowStore(b.profile_dir).get("wf1") is not None   # untouched


def test_workflow_actions_excluded_inside_task_loop(settings):
    llm = ScriptedLLM([
        {"tier": "simple", "flags": {}},
        {"thought": "sneaky", "action": {"type": "run_workflow", "id": "wf1"}},
        {"thought": "ok", "finish": "gave up"},
    ])
    record = run_task("try workflows from inside", settings, llm=llm, notify=False)
    assert any("not available inside a task" in str(s.get("outcome"))
               for s in record["steps"])


def test_context_lists_saved_workflows(settings):
    from assistant.chat.agent import build_context

    assert "Saved workflows" not in build_context(settings)   # only when non-empty
    _mk(settings)
    ctx = build_context(settings)
    assert "Saved workflows" in ctx and "[wf1] 周报" in ctx