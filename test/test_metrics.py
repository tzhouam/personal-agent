from datetime import date, timedelta

from assistant.deliver.email import render_html
from assistant.events_store import EventsStore
from assistant.metrics import EXTRACTORS, build_health, render_health_html
from assistant.todo_store import ReadingList, TodoStore


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
                   "suppressed_seen": 4},
        "todos": {"open_count": 7, "added": ["t1"], "closed": [{}]},
        "research": {"papers": [{}, {}], "industry": [{}],
                     "source_health": {"a": "ok", "b": "ok (3 items)", "c": "error"}},
        "website": {"status": "pushed"},
        "email_sent": True,
        "curated": {"decayed": [{}]},
    }
    assert EXTRACTORS["collect"](out) == {"observations": 3, "notifications": 1,
                                          "obs_github": 2, "obs_gmail": 1}
    assert EXTRACTORS["profile"](out) == {"ops_applied": 2}
    assert EXTRACTORS["digest"](out) == {"red": 1, "yellow": 0, "white": 2, "suppressed": 4}
    assert EXTRACTORS["todos"](out) == {"wip": 7, "added": 1, "auto_closed": 1}
    assert EXTRACTORS["research"](out) == {"papers": 2, "paper_quota": 0, "industry": 1,
                                           "sources_ok": 2, "sources_total": 3}
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
