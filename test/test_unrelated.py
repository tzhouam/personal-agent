"""Negative reading feedback: 🚫 Unrelated → store → research scorer bias."""

from datetime import date

from assistant.actions import run_action
from assistant.events_store import EventsStore
from assistant.research import pipeline as pipeline_mod
from assistant.research.pipeline import run_research
from assistant.todo_store import ReadingList
from assistant.website import render_site

PROFILE = {"identity": {"name": "T", "github": "t", "emails": ["me@example.com"]},
           "skills": [], "interests": [], "projects": [],
           "education": [], "experience": []}


def test_mark_unrelated_and_titles(settings):
    store = ReadingList(settings.profile_dir)
    store.upsert("a1", title="Quantum finance paper", url="u1")
    store.upsert("a2", title="LLM serving paper", url="u2")
    assert store.mark_unrelated("r1")
    assert not store.mark_unrelated("r1")          # already marked
    assert not store.mark_unrelated("r99")
    assert [r["id"] for r in store.open_items()] == ["r2"]   # gone from open
    assert store.unrelated_titles() == ["Quantum finance paper"]
    unrel = next(i for i in store.load()["items"] if i["id"] == "r1")
    assert unrel["status"] == "unrelated" and unrel["unrelated_at"]


def test_unrelated_action(settings):
    ReadingList(settings.profile_dir).upsert("a1", title="Meh paper")
    result = run_action("unrelated_reading", {"id": "r1"}, settings)
    assert "marked unrelated" in result and "avoid similar" in result
    assert "no reading item" in run_action("unrelated_reading", {"id": "r9"}, settings)


def test_research_scorer_sees_negatives(settings, monkeypatch):
    ReadingList(settings.profile_dir).upsert("a1", title="Quantum finance arbitrage")
    ReadingList(settings.profile_dir).mark_unrelated("r1")

    monkeypatch.setattr(pipeline_mod, "_gather_papers",
                        lambda llm, p, ps, s, h: [{"title": "Some paper", "abstract": "x",
                                                   "seen_id": "arxiv-1", "url": "u"}])
    monkeypatch.setattr(pipeline_mod, "_gather_feed_items", lambda s, h: [])
    prompts = []

    class FakeLLM:
        cheap_model = "cheap"

        def complete_json(self, prompt, system=None, **kw):
            prompts.append((system or "") + "\n" + prompt)
            if "Score each item" in (system or ""):
                return [{"idx": 0, "score": 9}]
            return {"papers": [], "items": []}  # the summary call wants a dict

    events = EventsStore(settings.events_db)
    result = run_research(FakeLLM(), PROFILE, events, settings)
    events.close()
    assert result["papers"]
    scoring = prompts[0]
    assert "Rejected as unrelated by the owner recently" in scoring
    assert "Quantum finance arbitrage" in scoring
    assert "score anything topically similar to them 0-2" in scoring


def test_reading_page_unrelated_button(settings):
    reading = [{"id": "r1", "title": "Paper", "url": "u", "why": "", "source": "arxiv",
                "created": "2026-07-09", "status": "open"}]
    files = render_site(PROFILE, [], today=date(2026, 7, 9), reading=reading)
    page = files["reading.html"]
    assert "b-unrel" in page and "🚫 Unrelated" in page
    assert "data-agent-mail='me@example.com'" in page
    # todos page never shows the unrelated button
    todos = [{"id": "t1", "title": "T", "source": "manual",
              "created": "2026-07-09", "status": "open"}]
    assert "b-unrel" not in render_site(PROFILE, todos,
                                        today=date(2026, 7, 9))["todos.html"]
    # the JS carries the unrelated storage + mail bridge
    js = files["agent-site.js"]
    assert "agent-reading-unrelated" in js and "reading unrelated" in js
