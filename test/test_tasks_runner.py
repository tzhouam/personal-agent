"""Agentic task runner: loop mechanics, failure adaptation, budgets,
exclusions, persistence, and the background-spawn handler."""

import json

from assistant.actions import run_action
from assistant.task_runner import run_task
from assistant.todo_store import TodoStore


class ScriptedLLM:
    def __init__(self, moves):
        self.moves = list(moves)
        self.prompts = []

    def complete_json(self, prompt, system=None, **kw):
        self.prompts.append(prompt)
        return self.moves.pop(0)


# every run_task now starts with one tier-assessment call (adaptive depth);
# scripts prepend its response
_SIMPLE = {"tier": "simple", "flags": {}}


def test_run_task_happy_path(settings):
    llm = ScriptedLLM([
        _SIMPLE,
        {"thought": "track the follow-up", "action":
            {"type": "add_todo", "title": "对比A100报价"}},
        {"thought": "done", "finish": "已创建跟进事项，报价对比明天给你。"},
    ])
    record = run_task("帮我对比A100报价并跟进", settings, llm=llm, notify=False)
    assert record["status"] == "done"
    assert record["report"].startswith("已创建跟进事项")
    assert record["steps"][0]["outcome"] == "added todo t1: 对比A100报价"
    assert [t["title"] for t in TodoStore(settings.profile_dir).open_items()] \
        == ["对比A100报价"]
    # step outcomes were visible to the next turn
    assert "added todo t1" in llm.prompts[2]
    # persisted artifact
    artifact = json.loads((settings.data_dir / "tasks"
                           / f"{record['id']}.json").read_text())
    assert artifact["status"] == "done" and len(artifact["steps"]) == 2


def test_run_task_adapts_after_failure(settings):
    llm = ScriptedLLM([
        _SIMPLE,
        {"thought": "log it", "action":
            {"type": "log_transaction", "kind": "spend", "amount": 45}},  # invalid kind
        {"thought": "fix the kind", "action":
            {"type": "log_transaction", "kind": "expense", "amount": 45}},
        {"thought": "done", "finish": "已记录45元支出。"},
    ])
    record = run_task("记一笔45", settings, llm=llm, notify=False)
    assert record["status"] == "done"
    assert "rejected" in record["steps"][0]["outcome"]
    assert record["steps"][1]["outcome"].startswith("logged f1")
    assert "rejected" in llm.prompts[2]  # saw the failure before adapting


def test_run_task_budgets_and_exclusions(settings):
    # excluded action + junk moves → 3 consecutive failures → aborted
    llm = ScriptedLLM([
        _SIMPLE,
        {"thought": "recurse!", "action": {"type": "execute_task", "request": "x"}},
        {"thought": "?", "action": {"type": "nonexistent_action"}},
        {"thought": "??", "action": None},
        {"thought": "never reached", "finish": "nope"},
    ])
    record = run_task("weird task", settings, llm=llm, notify=False)
    assert record["status"] == "aborted"
    assert "not available inside a task" in record["steps"][0]["outcome"]
    assert len(record["steps"]) == 3
    # turn budget: model never finishes (simple tier caps at 3 turns)
    llm = ScriptedLLM([_SIMPLE] + [{"thought": "todo", "action":
                       {"type": "add_todo", "title": f"t{i}"}} for i in range(5)])
    record = run_task("loop forever", settings, llm=llm, max_turns=4, notify=False)
    assert record["status"] == "aborted" and "budget" in record["report"]


def test_execute_task_handler_spawns_detached(settings, monkeypatch):
    from assistant.actions import handlers as handlers_mod

    spawned = {}

    def fake_popen(cmd, **kw):
        spawned["cmd"] = cmd
        spawned["detached"] = kw.get("start_new_session")

    monkeypatch.setattr(handlers_mod.subprocess, "Popen", fake_popen)
    out = run_action("execute_task", {"request": "查一下明天深圳天气"}, settings)
    assert "task started in the background" in out
    assert spawned["cmd"][-2:] == ["task", "查一下明天深圳天气"]
    assert spawned["detached"] is True
    import pytest
    with pytest.raises(ValueError, match="missing required 'request'"):
        run_action("execute_task", {"request": ""}, settings)


def test_run_task_cancel_check_aborts_between_steps(settings):
    """Cooperative cancellation (§6): `cancel_check` fires at the top of every
    turn, so a cancel flagged after step 1 stops the task before step 2's LLM
    call — and the raise propagates to the caller (the job worker)."""
    import pytest

    from assistant.worker import Cancelled

    llm = ScriptedLLM([
        _SIMPLE,
        {"thought": "step 1", "action": {"type": "add_todo", "title": "第一步"}},
        {"thought": "step 2", "action": {"type": "add_todo", "title": "第二步"}},
    ])
    checks = {"n": 0}

    def check():
        checks["n"] += 1
        if checks["n"] > 1:               # cancel lands after the first turn
            raise Cancelled()

    with pytest.raises(Cancelled):
        run_task("多步任务", settings, llm=llm, notify=False, cancel_check=check)
    # exactly one turn ran (assessment + step 1); step 2's LLM call never happened
    assert len(llm.prompts) == 2
    assert [t["title"] for t in TodoStore(settings.profile_dir).open_items()] \
        == ["第一步"]
