"""Regression coverage for evidence-first autonomous task execution."""

import hashlib
import json

from assistant.agent.actions import execute_results
from assistant.agent.personal_search import search_personal_data
from assistant.agent.task_runner import run_task
from assistant.agent.todo_store import TodoStore
from assistant.platform.config import Settings


class ScriptedLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete_json(self, prompt, system=None, **kwargs):
        self.calls.append({"prompt": prompt, "system": system, **kwargs})
        assert self.responses, "LLM called more times than scripted"
        return self.responses.pop(0)


def _session(settings, owner, assistant="noted", stamp="2026-06-01T10:00:00+00:00"):
    path = settings.data_dir / "sessions" / "session-a" / "2026-06-01.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"turns": [
        {"ts": stamp, "owner": owner, "assistant": assistant}
    ]}, ensure_ascii=False))


def test_personal_search_is_stable_and_tenant_scoped(tmp_path):
    alice = Settings(_env_file=None, data_dir=tmp_path / "alice")
    bob = Settings(_env_file=None, data_dir=tmp_path / "bob")
    _session(alice, "Project Calypso interview", "room 123 at 10:00")
    _session(bob, "Project Calypso private", "must never cross tenants")

    first = search_personal_data(alice, "Calypso", sources="sessions")
    second = search_personal_data(alice, "Calypso", sources="sessions")
    assert first and first[0]["id"].startswith("P-")
    assert [hit["id"] for hit in first] == [hit["id"] for hit in second]
    assert "room 123" in first[0]["text"]
    assert all("never cross tenants" not in hit["text"] for hit in first)


def test_personal_history_route_retrieves_before_one_controller_call(settings):
    _session(settings, "Project Phoenix interview", "July 19, room 555-010-999")
    evidence_id = search_personal_data(
        settings, "历史对话 Project Phoenix", sources="sessions")[0]["id"]
    llm = ScriptedLLM([{
        "thought": "retrieval contains the requested record",
        "finish": f"The interview was July 19 in room 555-010-999 [{evidence_id}].",
        "completion": "full",
        "milestone_done": 2,
    }])

    record = run_task("从历史对话查找 Project Phoenix 面试", settings,
                      llm=llm, notify=False)

    assert record["status"] == "done" and record["completion"] == "full"
    assert record["plan"]["route"] == "personal_history"
    assert record["steps"][0]["action"]["type"] == "search_personal_data"
    assert record["steps"][0]["result"]["ok"] is True
    assert evidence_id in record["evidence_ids"]
    assert len(llm.calls) == 1  # no classification, planning, or retrieval-control call
    available = llm.calls[0]["system"].split("Respond with ONLY JSON", 1)[0]
    assert '"type": "web_research"' not in available


def test_validated_read_only_plan_bootstraps_batched_web_research(
        settings, monkeypatch):
    queries = []

    def search(query, max_results=8, settings=None):
        queries.append(query)
        return {"answer": "", "results": [{
            "title": "Official schedule",
            "url": "https://example.org/schedule",
            "snippet": "The event is in 2026.",
        }]}

    monkeypatch.setattr("assistant.platform.search.web_search_answer", search)
    evidence_id = "W-" + hashlib.sha1(
        b"https://example.org/schedule").hexdigest()[:8]
    llm = ScriptedLLM([
        {"tier": "medium", "flags": {}},
        {"requirements": ["find the official year"], "steps": [
            {"step": "research official schedule", "action": {
                "type": "web_research", "queries": ["official schedule", "official schedule"]},
             "produces": ["dated official source"]},
            {"step": "report the verified year"},
        ], "verify": "cite the official source", "risks": "stale snippets"},
        {"thought": "verified", "finish": f"The event is in 2026 [{evidence_id}].",
         "completion": "full", "milestone_done": 2},
    ])

    record = run_task("find and report the official event year", settings,
                      llm=llm, notify=False)

    assert record["status"] == "done"
    assert queries == ["official schedule"]  # deduped and executed once
    assert record["steps"][0]["action"]["type"] == "web_research"
    assert evidence_id in record["evidence_ids"]
    assert len(llm.calls) == 3  # assess + plan + final synthesis
    assert llm.calls[-1]["mixture"] is False


def test_duplicate_successful_action_is_not_executed_twice(settings):
    llm = ScriptedLLM([
        {"tier": "simple", "flags": {}},
        {"thought": "add it", "action": {"type": "add_todo", "title": "one item"}},
        {"thought": "repeat", "action": {"type": "add_todo", "title": "one item"}},
        {"thought": "done", "finish": "The one requested item exists.",
         "completion": "full"},
    ])
    record = run_task("add one item", settings, llm=llm, notify=False)
    assert record["status"] == "done"
    assert "duplicate action skipped" in record["steps"][1]["outcome"]
    assert len(TodoStore(settings.profile_dir).open_items()) == 1


def test_plan_validation_surfaces_unavailable_action_to_controller(settings):
    llm = ScriptedLLM([
        {"tier": "medium", "flags": {}},
        {"requirements": ["complete safely"], "steps": [{
            "step": "recursively start another task",
            "action": {"type": "execute_task", "request": "nested"},
        }], "verify": "check completion", "risks": "recursion"},
        {"thought": "cannot use it", "finish": "The required capability is unavailable.",
         "completion": "blocked"},
        {"thought": "confirmed", "finish": "The required capability is unavailable.",
         "completion": "blocked"},
    ])
    record = run_task("attempt a nested operation", settings, llm=llm, notify=False)
    assert record["status"] == "blocked"
    assert "unavailable action execute_task" in record["plan"]["capability_errors"][0]
    assert "Planner validation rejected" in llm.calls[2]["prompt"]


def test_web_research_returns_structured_exact_sources(settings, monkeypatch):
    monkeypatch.setattr("assistant.platform.search.web_search_answer", lambda q, **kwargs: {
        "answer": "", "results": [{"title": q, "url": f"https://example.com/{q}",
                                     "snippet": f"evidence for {q}"}]})
    result = execute_results([{"type": "web_research", "queries": ["a", "b", "a"]}],
                             settings)[0]
    assert result.ok and len(result.data["queries"]) == 2
    assert len(result.provenance) == 2
    assert all(source["url"] in result.text for source in result.provenance)
    assert all(source["id"].startswith("W-") for source in result.provenance)


def test_finish_cannot_infer_year_from_retrieval_timestamp(settings, monkeypatch):
    url = "https://example.com/undated-event"
    evidence_id = "W-" + hashlib.sha1(url.encode()).hexdigest()[:8]
    monkeypatch.setattr("assistant.platform.search.web_search_answer", lambda q, **kwargs: {
        "answer": "", "results": [{"title": "Event seven", "url": url,
                                     "snippet": "The seventh event was held in March."}]})
    llm = ScriptedLLM([
        {"tier": "medium", "flags": {}},
        {"requirements": ["identify the year"], "steps": [
            {"step": "research it", "action": {"type": "web_research", "query": "event seven"}},
            {"step": "report the year"},
        ], "verify": "cite dated evidence", "risks": "source may omit the year"},
        {"thought": "guess", "finish": f"It happened in 2026 at "
                                         f"https://invented.example/event [{evidence_id}].",
         "completion": "full", "milestone_done": 2},
        {"thought": "correct", "finish": f"The retrieved source does not state a year "
                                             f"[{evidence_id}].",
         "completion": "partial"},
    ])
    record = run_task("identify the year of event seven", settings,
                      llm=llm, notify=False)
    assert record["status"] == "partial"
    assert "does not state a year" in record["report"]
    assert any("introduces year(s) absent" in str(step.get("outcome"))
               for step in record["steps"])
    assert record["unsupported_url_corrections"] == 1
