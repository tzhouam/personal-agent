"""Self-evolution: lessons store lifecycle, prompt injection, feedback
capture in chat, and the evolve pass over chat/task evidence."""

import json

from assistant.actions import run_action
from assistant.chat.agent import handle_message, system_prompt
from assistant.lessons_store import MAX_ACTIVE, LessonsStore
from assistant.tasks.evolve import evolve


class FakeLLM:
    def __init__(self, result):
        self.result = result
        self.prompts = []
        self.systems = []

    def complete_json(self, prompt, system=None, **kw):
        self.prompts.append(prompt)
        self.systems.append(system)
        return self.result


def test_store_lifecycle_and_caps(settings):
    store = LessonsStore(settings.profile_dir)
    lesson = store.learn("记账默认用人民币", why="owner said so")
    assert lesson["id"] == "L1" and lesson["source"] == "owner"
    # near-duplicates rejected (containment either way)
    assert store.learn("记账默认用人民币，谢谢") is None
    assert store.retire("L1") and not store.retire("L1")
    assert store.active() == []
    # cap: evolve-sourced lessons evict oldest-evolve first, owner never auto-evicted
    for i in range(MAX_ACTIVE):
        assert store.learn(f"distinct rule {i} ends", source="evolve")
    first_evolve = store.active()[0]["id"]
    assert store.learn("owner rule wins", source="owner")
    active = store.active()
    assert len(active) == MAX_ACTIVE
    assert first_evolve not in [l["id"] for l in active]
    # all-owner full set refuses new evolve lessons rather than evicting owner rules
    store2 = LessonsStore(settings.profile_dir / "x")
    for i in range(MAX_ACTIVE):
        store2.learn(f"owner only rule {i} ends", source="owner")
    assert store2.learn("one more", source="evolve") is None


def test_prompt_injection_changes_behavior_surface(settings):
    assert "[L1]" not in system_prompt(settings)
    LessonsStore(settings.profile_dir).learn("回复我时永远用中文")
    assert "[L1] 回复我时永远用中文" in system_prompt(settings)


def test_chat_captures_direct_feedback(settings):
    llm = FakeLLM({"reply": "记住了，以后都这样。", "actions": [
        {"type": "learn_preference", "rule": "记账时默认使用港币",
         "why": "owner correction"}]})
    reply = handle_message("以后记账默认用港币", settings, llm)
    assert "learned L1: 记账时默认使用港币" in reply
    # the very next turn carries the rule in BOTH injection points
    assert "[L1] 记账时默认使用港币" in system_prompt(settings)
    handle_message("hi", settings, llm)
    assert "## Learned rules" in llm.prompts[-1]
    assert "[L1] 记账时默认使用港币" in llm.prompts[-1]
    assert run_action("retire_preference", {"id": "L1"}, settings) == "lesson L1 retired"
    assert "(no learned rules yet)" in run_action("list_preferences", {}, settings)


def test_evolve_distills_from_sessions_and_tasks(settings):
    sessions = settings.data_dir / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "a.json").write_text(json.dumps({"turns": [
        {"ts": "2026-07-12T10:00:00+00:00", "owner": "记账45",
         "assistant": "记好了\n\n✔ transaction rejected — need kind"},
        {"ts": "2026-07-12T10:01:00+00:00", "owner": "是支出！",
         "assistant": "已记录 (retry) logged f1"},
    ]}))
    tasks = settings.data_dir / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "task-20990101-000000.json").write_text(json.dumps(
        {"id": "task-x", "status": "aborted", "request": "查天气",
         "steps": [{"outcome": "web_search failed: timeout"}]}))
    llm = FakeLLM({"lessons": [
        {"rule": "记账信息不完整时先问清是收入还是支出", "why": "rejected then retried"},
        {"rule": "记账信息不完整时先问清是收入还是支出", "why": "dup"},  # dedup
    ], "note": "reviewed"})
    result = evolve(settings, llm)
    assert [l["rule"] for l in result["learned"]] == ["记账信息不完整时先问清是收入还是支出"]
    assert result["learned"][0]["source"] == "evolve"
    # evidence reached the model with friction markers and existing-lesson gate
    assert "[FRICTION]" in llm.prompts[0]
    assert "task-x" in llm.prompts[0] and "aborted" in llm.prompts[0]
    assert "Existing lessons" in llm.prompts[0]
    # nothing to review → no LLM call path
    empty = type(settings)(_env_file=None, data_dir=settings.data_dir / "empty")
    assert evolve(empty, FakeLLM({}))["reviewed"] == 0


def test_self_evolve_action(settings, monkeypatch):
    monkeypatch.setattr("assistant.tasks.evolve.evolve",
                        lambda s, l: {"reviewed": 100, "proposed": [{}],
                                      "learned": []})
    monkeypatch.setattr("assistant.llm.LLM.__init__", lambda self, s: None)
    out = run_action("self_evolve", {}, settings)
    assert "no new durable lesson" in out
