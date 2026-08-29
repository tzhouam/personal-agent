from datetime import date, timedelta

from assistant.agent.deliver.email import render_html
from assistant.agent.events_store import EventsStore
from assistant.agent.metrics import EXTRACTORS, build_health, render_health_html
from assistant.agent.todo_store import ReadingList, TodoStore


def test_record_and_window(settings):
    events = EventsStore(settings.events_db)
    events.record_metrics("r1", "collect", {"observations": 42, "bogus": "text"})
    events.record_metrics("r1", "profile", {"ops_applied": 5})
    rows = events.metrics_window(days=1)
    assert [(r["step"], r["name"], r["value"]) for r in rows] == [
        ("collect", "observations", 42.0), ("profile", "ops_applied", 5.0)]
    events.close()


def test_extractors_cover_all_phases():
    out = {
        "observations": [{"source": "github"}, {"source": "github"}, {"source": "gmail"}],
        "notifications": [{}],
        "profile_ops": [{}, {}],
        "digest": {"sections": {"red": [{}], "yellow": [], "white": [{}, {}]},
                   "suppressed_seen": 4, "llm_requested": 5, "llm_triaged": 3,
                   "fallback_count": 2, "degraded": True,
                   "fallback_reason_code": "partial_response"},
        "todos": {"open_count": 7, "added": ["t1"], "closed": [{}]},
        # production shape: feed rows are tagged ok:/FAILED, the rest are notes
        "research": {"papers": [{}, {}], "industry": [{}],
                     "score_requested": 9, "score_completed": 7,
                     "score_fallback": 2, "summary_requested": 4,
                     "summary_completed": 3, "summary_fallback": 1,
                     "degraded": True,
                     "source_health": {"OpenAI Blog": "ok: 15 items",
                                       "机器之心": "FAILED: HTTPStatusError",
                                       "arxiv": "39 candidates from 5 queries",
                                       "paper quota": "paper quota 10→2: …"}},
        "website": {"status": "pushed"},
        "email_sent": True,
        "curated": {"decayed": [{}]},
    }
    assert EXTRACTORS["collect"](out) == {"observations": 3, "notifications": 1,
                                          "obs_github": 2, "obs_gmail": 1}
    assert EXTRACTORS["profile"](out) == {"ops_applied": 2}
    assert EXTRACTORS["digest"](out) == {
        "red": 1, "yellow": 0, "white": 2, "suppressed": 4,
        "llm_requested": 5, "llm_triaged": 3, "fallback_count": 2,
        "degraded": 1,
    }
    assert EXTRACTORS["todos"](out) == {"wip": 7, "added": 1, "auto_closed": 1}
    # only the ok:/FAILED rows count as sources — the free-text arxiv and quota
    # notes used to inflate `sources_total` while `sources_ok` sat at 0 forever
    assert EXTRACTORS["research"](out) == {
        "papers": 2, "paper_quota": 0, "industry": 1,
        "sources_ok": 1, "sources_total": 2,
        "score_requested": 9, "score_completed": 7, "score_fallback": 2,
        "summary_requested": 4, "summary_completed": 3, "summary_fallback": 1,
        "degraded": 1,
    }
    assert EXTRACTORS["website"](out) == {"pushed": 1}
    assert EXTRACTORS["deliver"](out) == {"email_sent": 1}
    assert EXTRACTORS["curate"](out) == {"decayed": 1}
    # a failed publish counts as not-pushed
    assert EXTRACTORS["website"]({"website": {"status": "failed"}}) == {"pushed": 0}


def test_build_health_and_render(settings):
    events = EventsStore(settings.events_db)
    for run in ("r1", "r2"):
        events.record_metrics(run, "run", {"duration_s": 120, "errors": 0})
        events.record_metrics(run, "collect", {"observations": 30, "errors": 0})
        events.record_metrics(run, "profile", {"ops_applied": 4, "ops_rejected": 1})
        events.record_metrics(run, "digest", {"red": 2, "suppressed": 5})
        events.record_metrics(run, "website", {"pushed": 1})
        events.record_metrics(run, "deliver", {"email_sent": 1})
    events.record_metrics("r2", "research", {"errors": 2})

    today = date.today()
    todos = TodoStore(settings.profile_dir)
    todos.upsert("k1", title="Open one", source="github", priority="red")
    # an acted-on red from 10 days ago and an ignored one → 1/2 action rate
    data = todos.load()
    for key, status in (("k-acted", "done"), ("k-ignored", "open")):
        data["items"].append({"id": f"tx{key}", "key": key, "status": status,
                              "title": key, "source": "github", "priority": "red",
                              "created": (today - timedelta(days=10)).isoformat(),
                              **({"done_at": today.isoformat()} if status == "done" else {})})
    todos._save(data, "seed")
    reading = ReadingList(settings.profile_dir)
    reading.upsert("arxiv:1", title="Paper", url="http://x")

    lines = dict(build_health(events, settings.profile_dir))
    assert lines["runs (7d)"].startswith("2")
    assert "research×1" in lines["steps with errors"]
    assert lines["profile ops acceptance"] == "8/10 (80%)"
    assert lines["digest reds / suppressed"] == "4 / 10"
    assert lines["red action rate (7-30d)"] == "1/2 (50%)"
    assert lines["reading surfaced / read (7d)"] == "1 / 0"
    assert lines["website publishes / emails"] == "2/2 (100%) / 2/2 (100%)"

    section = render_health_html(build_health(events, settings.profile_dir))
    assert "Health (7 days)" in section and "red action rate" in section
    assert render_health_html([]) == ""
    events.close()


def test_health_section_lands_in_email():
    body = render_html("2026-07-09", {}, {}, {}, {}, [], {}, "", [], {"run": "x"},
                       health_html="<h3>📈 Health (7 days)</h3>")
    assert "Health (7 days)" in body
    assert "Health" not in render_html("2026-07-09", {}, {}, {}, {}, [], {}, "", [],
                                       {"run": "x"})


def test_health_includes_chat_outcome_line(settings):
    events = EventsStore(settings.events_db)
    # no labeled chat rows → line absent
    assert "chat turns (7d)" not in dict(build_health(events, settings.profile_dir))
    for i, label in enumerate(("success", "success", "fail", "neutral")):
        events.record_metrics(f"chat-x{i}", "chat_turn", {
            "success": int(label == "success"), "fail": int(label == "fail"),
            "neutral": int(label == "neutral"), "repaired": int(i == 0),
            "prev_satisfied": 0, "prev_dissatisfied": int(i == 2)})
    line = dict(build_health(events, settings.profile_dir))["chat turns (7d)"]
    assert line.startswith("2/3 (66%) success")
    assert "1 neutral" in line and "1 dissatisfied" in line and "1 repaired" in line
    events.close()
