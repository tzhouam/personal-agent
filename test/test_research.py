import re
import threading
from contextvars import ContextVar
from datetime import datetime, timezone

from assistant.agent.research.arxiv import parse_feed as parse_arxiv
from assistant.agent.research.feeds import parse_feed as parse_rss
from assistant.agent.research.pipeline import _score, _summarize

ARXIV_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2501.12345v2</id>
    <title>Efficient  LLM
      Serving</title>
    <summary>We present a method.</summary>
    <published>2026-07-01T00:00:00Z</published>
    <author><name>A. Author</name></author>
    <category term="cs.LG"/>
    <category term="cs.DC"/>
  </entry>
</feed>"""

RSS2 = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>机器之心</title>
<item><title>大模型新进展</title><link>https://example.com/a</link>
<description>&lt;p&gt;正文摘要&lt;/p&gt;</description><pubDate>Wed, 01 Jul 2026 08:00:00 GMT</pubDate></item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Blog</title>
<entry><title>Post One</title>
<link rel="alternate" href="https://example.com/p1"/>
<updated>2026-07-01T00:00:00Z</updated><summary>Hello &lt;b&gt;world&lt;/b&gt;</summary></entry>
</feed>"""


def test_parse_arxiv_atom():
    papers = parse_arxiv(ARXIV_ATOM)
    assert len(papers) == 1
    p = papers[0]
    assert p["id"] == "2501.12345v2"
    assert p["title"] == "Efficient LLM Serving"  # whitespace collapsed
    assert p["url"] == "https://arxiv.org/abs/2501.12345"  # version stripped
    assert p["categories"] == ["cs.LG", "cs.DC"]


def test_parse_rss2():
    items = parse_rss(RSS2)
    assert items == [{
        "title": "大模型新进展",
        "url": "https://example.com/a",
        "published": "Wed, 01 Jul 2026 08:00:00 GMT",
        "summary": "正文摘要",
    }]


def test_parse_atom():
    items = parse_rss(ATOM)
    assert items[0]["title"] == "Post One"
    assert items[0]["url"] == "https://example.com/p1"
    assert "world" in items[0]["summary"] and "<b>" not in items[0]["summary"]


def test_missing_sources_file_is_reported_not_silent(settings):
    """`load_sources` degrades to [] for an absent file, so a misresolved
    `sources_file` looked exactly like a quiet news day — three days of empty
    industry/中文 sections (2026-07-22→24) raised nothing anywhere."""
    from assistant.agent.research.pipeline import _gather_feed_items

    settings.sources_file = settings.data_dir / "nope" / "sources.yaml"
    health = {}
    assert _gather_feed_items(settings, health) == []
    assert health["sources"].startswith("FAILED: sources file missing")


def test_empty_research_has_coherent_zero_accounting(settings, monkeypatch):
    from assistant.agent.events_store import EventsStore
    from assistant.agent.research import pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod, "_gather_papers",
                        lambda llm, profile, summary, cfg, health: [])
    monkeypatch.setattr(pipeline_mod, "_gather_feed_items",
                        lambda cfg, health: [])
    events = EventsStore(settings.events_db)
    try:
        result = pipeline_mod.run_research(
            object(), {"identity": {}, "skills": [], "interests": [],
                       "projects": [], "education": [], "experience": []},
            events, settings)
    finally:
        events.close()

    assert {key: result[key] for key in (
        "score_requested", "score_completed", "score_fallback",
        "summary_requested", "summary_completed", "summary_fallback",
    )} == {
        "score_requested": 0, "score_completed": 0, "score_fallback": 0,
        "summary_requested": 0, "summary_completed": 0, "summary_fallback": 0,
    }
    assert result["degraded"] is False


def _accounting():
    return {
        "score_requested": 0, "score_completed": 0, "score_fallback": 0,
        "summary_requested": 0, "summary_completed": 0, "summary_fallback": 0,
    }


def _score_prompt_indices(prompt):
    return [int(value) for value in re.findall(r"^\[(\d+)\]", prompt, re.MULTILINE)]


def test_arxiv_query_generation_uses_bounded_non_mixture_json(settings, monkeypatch):
    from assistant.agent.research import pipeline as pipeline_mod

    calls = []

    class QueryLLM:
        def complete_json(self, prompt, **kwargs):
            calls.append((prompt, kwargs))
            return {"queries": ["bounded inference"]}

    fetched = []

    def fetch_recent(queries, lookback_days, max_per_query):
        fetched.append((queries, lookback_days, max_per_query))
        return []

    monkeypatch.setattr(pipeline_mod.arxiv, "fetch_recent", fetch_recent)
    health = {}
    assert pipeline_mod._gather_papers(
        QueryLLM(), {}, "profile", settings, health,
    ) == []

    assert len(calls) == 1
    assert calls[0][1]["max_tokens"] == 4000
    assert calls[0][1]["mixture"] is False
    assert fetched == [(["bounded inference"], settings.arxiv_lookback_days,
                        settings.arxiv_max_per_query)]


def test_score_batches_preserve_global_indices_order_and_two_call_concurrency():
    class BatchingLLM:
        def __init__(self):
            self.calls = []
            self.active = 0
            self.max_active = 0
            self.lock = threading.Lock()
            self.first_pair = threading.Barrier(2)

        def complete_json(self, prompt, **kwargs):
            indices = _score_prompt_indices(prompt)
            with self.lock:
                call_number = len(self.calls)
                self.calls.append((indices, kwargs))
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                if call_number < 2:
                    self.first_pair.wait(timeout=2)
                return [{"idx": index, "score": 10 - index % 3}
                        for index in reversed(indices)]
            finally:
                with self.lock:
                    self.active -= 1

    llm = BatchingLLM()
    pool = [{"position": i, "title": f"item-{i}"} for i in range(45)]
    accounting = _accounting()
    ranked = _score(llm, "profile", pool, lambda item: item["title"], accounting)

    calls = sorted(llm.calls, key=lambda call: call[0][0])
    assert [indices for indices, _ in calls] == [
        list(range(0, 20)), list(range(20, 40)), list(range(40, 45)),
    ]
    assert all(kwargs["max_tokens"] == 8000 and kwargs["mixture"] is False
               for _, kwargs in calls)
    assert llm.max_active == 2
    assert accounting["score_requested"] == 45
    assert accounting["score_completed"] == 45
    assert accounting["score_fallback"] == 0
    assert [item["position"] for item in ranked] == sorted(
        range(45), key=lambda index: (index % 3, index),
    )


def test_score_workers_inherit_frozen_context_without_mutating_caller():
    from assistant.platform.timeutil import frozen_now, local_today

    marker = ContextVar("score_worker_test_marker", default="outside")
    barrier = threading.Barrier(2)
    observed = []
    lock = threading.Lock()

    class ContextLLM:
        def complete_json(self, prompt, **kwargs):
            indices = _score_prompt_indices(prompt)
            with lock:
                observed.append((marker.get(), local_today(), indices))
            # A worker-local mutation must never escape its copied Context or
            # bleed into the sibling batch.
            marker.set(f"worker-{indices[0]}")
            barrier.wait(timeout=2)
            return [{"idx": index, "score": 8} for index in indices]

    token = marker.set("caller")
    try:
        outer = datetime(1999, 12, 31, 12, 0, tzinfo=timezone.utc)
        inner = datetime(2000, 1, 2, 23, 59, tzinfo=timezone.utc)
        with frozen_now(outer):
            with frozen_now(inner):
                pool = [{"title": f"item-{index}"} for index in range(21)]
                ranked = _score(
                    ContextLLM(), "profile", pool, lambda item: item["title"])
                assert marker.get() == "caller"
                assert local_today().isoformat() == "2000-01-02"
            assert marker.get() == "caller"
            assert local_today().isoformat() == "1999-12-31"
    finally:
        marker.reset(token)

    assert marker.get() == "outside"
    assert len(ranked) == 21
    assert sorted(indices[0] for _value, _day, indices in observed) == [0, 20]
    assert all(value == "caller" for value, _day, _indices in observed)
    assert all(day.isoformat() == "2000-01-02"
               for _value, day, _indices in observed)


def test_score_failed_batch_falls_back_without_poisoning_other_batches():
    class OneBrokenBatchLLM:
        def complete_json(self, prompt, **kwargs):
            indices = _score_prompt_indices(prompt)
            if indices[0] == 20:
                raise RuntimeError("middle batch failed")
            return [{"idx": index, "score": 9} for index in indices]

    pool = [{"position": i, "title": f"item-{i}"} for i in range(45)]
    accounting = _accounting()
    ranked = _score(OneBrokenBatchLLM(), "profile", pool,
                    lambda item: item["title"], accounting)
    by_position = {item["position"]: item["_score"] for item in ranked}

    assert accounting["score_requested"] == 45
    assert accounting["score_completed"] == 25
    assert accounting["score_fallback"] == 20
    assert all(by_position[index] == 9 for index in [*range(20), *range(40, 45)])
    assert all(by_position[index] == 6 for index in range(20, 40))


def test_score_accounts_partial_response_without_changing_fallback_order():
    class PartialLLM:
        def complete_json(self, *a, **k):
            return [
                {"idx": 0, "score": 9},
                {"idx": 0, "score": 1},  # duplicate must not overwrite first
                {"idx": 2, "score": 7},
                {"idx": 99, "score": 10},  # another batch/item cannot leak in
            ]

    pool = [{"title": "a"}, {"title": "b"}, {"title": "c"}]
    accounting = _accounting()
    ranked = _score(PartialLLM(), "profile", pool, lambda item: item["title"],
                    accounting)

    assert accounting == {
        "score_requested": 3, "score_completed": 2, "score_fallback": 1,
        "summary_requested": 0, "summary_completed": 0, "summary_fallback": 0,
    }
    assert [(item["title"], item["_score"]) for item in ranked] == [
        ("a", 9), ("c", 7), ("b", 0),
    ]


def test_score_rejects_bool_float_and_out_of_range_rows_item_locally():
    class MalformedRowsLLM:
        def complete_json(self, *a, **k):
            return [
                {"idx": 0, "score": True},
                {"idx": "0", "score": "8"},  # integer strings stay compatible
                {"idx": True, "score": 10},
                {"idx": 1, "score": 11},
                {"idx": 2, "score": -1},
                {"idx": 3, "score": 7.5},
                {"idx": "4", "score": "10"},
            ]

    pool = [{"position": i} for i in range(5)]
    accounting = _accounting()
    ranked = _score(MalformedRowsLLM(), "profile", pool,
                    lambda item: str(item["position"]), accounting)

    assert accounting["score_completed"] == 2
    assert accounting["score_fallback"] == 3
    assert [(item["position"], item["_score"]) for item in ranked] == [
        (4, 10), (0, 8), (1, 0), (2, 0), (3, 0),
    ]


def test_summary_accounts_partial_and_total_fallbacks():
    papers = [
        {"seen_id": "p1", "title": "P1", "abstract": "A1"},
        {"seen_id": "p2", "title": "P2", "abstract": "A2"},
    ]
    items = [{"seen_id": "f1", "source": "Feed", "title": "F1",
              "summary": "S1", "lang": "en"}]

    class PartialLLM:
        def complete_json(self, *a, **k):
            return {
                "papers": [
                    {"id": "p1", "summary": "summary 1", "why": "why 1"},
                    {"id": "p2", "summary": "summary 2"},
                ],
                "items": [{"id": "f1", "takeaway": "takeaway 1"}],
            }

    accounting = _accounting()
    _summarize(PartialLLM(), "profile", papers, items, accounting)
    assert accounting["summary_requested"] == 3
    assert accounting["summary_completed"] == 2
    assert accounting["summary_fallback"] == 1
    assert papers[1]["summary"] == "summary 2" and papers[1]["why"] == ""
    assert items[0]["takeaway"] == "takeaway 1"

    class BrokenLLM:
        def complete_json(self, *a, **k):
            raise RuntimeError("down")

    fallback_paper = {"seen_id": "pf", "title": "PF", "abstract": "AF"}
    failed = _accounting()
    _summarize(BrokenLLM(), "profile", [fallback_paper], [], failed)
    assert failed["summary_requested"] == 1
    assert failed["summary_completed"] == 0
    assert failed["summary_fallback"] == 1
    assert fallback_paper["summary"] == "AF" and fallback_paper["why"] == ""


def test_summary_null_fields_are_fallback_not_completion():
    papers = [{"seen_id": "p1", "title": "P1", "abstract": "fallback abstract"}]
    items = [{"seen_id": "f1", "source": "Feed", "title": "F1",
              "summary": "fallback feed", "lang": "en"}]

    class NullLLM:
        def complete_json(self, *a, **k):
            return {"papers": [{"id": "p1", "summary": None, "why": ""}],
                    "items": [{"id": "f1", "takeaway": None}]}

    accounting = _accounting()
    _summarize(NullLLM(), "profile", papers, items, accounting)
    assert accounting["summary_requested"] == 2
    assert accounting["summary_completed"] == 0
    assert accounting["summary_fallback"] == 2
    assert papers[0]["summary"] == "fallback abstract"
    assert papers[0]["why"] == ""
    assert items[0]["takeaway"] == "fallback feed"


def test_summary_parseable_malformed_shape_falls_back_cleanly():
    papers = [{"seen_id": "p1", "title": "P1", "abstract": "fallback abstract"}]

    class MalformedLLM:
        def complete_json(self, *a, **k):
            return {"papers": None, "items": []}

    accounting = _accounting()
    _summarize(MalformedLLM(), "profile", papers, [], accounting)
    assert accounting["summary_requested"] == 1
    assert accounting["summary_completed"] == 0
    assert accounting["summary_fallback"] == 1
    assert papers[0]["summary"] == "fallback abstract"


def test_summary_nested_malformed_ids_fall_back_cleanly():
    papers = [{"seen_id": "p1", "title": "P1", "abstract": "paper fallback"}]
    items = [{"seen_id": "f1", "source": "Feed", "title": "F1",
              "summary": "feed fallback", "lang": "en"}]

    class MalformedLLM:
        def complete_json(self, *a, **k):
            return {
                "papers": [{"id": [], "summary": "bad", "why": "bad"}],
                "items": [{"id": {}, "takeaway": "bad"}],
            }

    accounting = _accounting()
    _summarize(MalformedLLM(), "profile", papers, items, accounting)
    assert accounting["summary_completed"] == 0
    assert accounting["summary_fallback"] == 2
    assert papers[0]["summary"] == "paper fallback"
    assert items[0]["takeaway"] == "feed fallback"
