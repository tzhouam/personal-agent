"""GitHub history backfill (`assistant enrich-profile --since`) — collector
sweeps, repo context, commit summaries, and the chronological batch flow."""

import argparse
import base64
import json

import httpx
import pytest

import assistant.agent.collectors.github as github_mod
from assistant.cli import cmd_enrich_profile
from assistant.agent.collectors.github import GitHubCollector, summarize_commits
from assistant.agent.events_store import EventsStore


@pytest.fixture(autouse=True)
def _no_search_delay(monkeypatch):
    monkeypatch.setattr(github_mod, "_SEARCH_PAGE_DELAY", 0)


def _mocked_collector(settings, handler):
    collector = GitHubCollector(settings)
    collector.client = httpx.Client(
        transport=httpx.MockTransport(handler),
        headers=collector.client.headers)
    return collector


def _search_item(n, repo="o/r", pr=True, body="body text"):
    item = {"title": f"Item {n}", "state": "open", "number": n, "body": body,
            "repository_url": f"https://api.github.com/repos/{repo}",
            "html_url": f"https://github.com/{repo}/pull/{n}",
            "updated_at": f"2025-{7 + n % 3:02d}-01T00:00:00Z", "labels": []}
    if pr:
        item["pull_request"] = {}
    return item


# ── search pagination + sweeps ───────────────────────────────────────

def test_search_pagination_full_sweep(settings):
    pages = {1: [_search_item(i) for i in range(100)],
             2: [_search_item(100 + i) for i in range(53)], 3: []}
    queries = []

    def handler(request):
        assert request.url.path == "/search/issues"
        queries.append(request.url.params["q"])
        page = int(request.url.params["page"])
        return httpx.Response(200, json={"items": pages.get(page, [])})

    from datetime import datetime, timezone
    gh = _mocked_collector(settings, handler)
    items = gh.fetch_authored_items(
        since=datetime(2025, 7, 1, tzinfo=timezone.utc), max_items=None)
    assert len(items) == 153
    assert queries[0] == "author:tester updated:>=2025-07-01"
    # default caps stay in place for the daily path
    assert len(gh.fetch_authored_items()) == 100


def test_reviewed_sweep_query_and_mapping(settings):
    def handler(request):
        assert request.url.params["q"] == "is:pr reviewed-by:tester -author:tester"
        if int(request.url.params["page"]) > 1:
            return httpx.Response(200, json={"items": []})
        return httpx.Response(200, json={"items": [
            _search_item(1, body="  lots\r\n of   whitespace  ")]})

    obs = _mocked_collector(settings, handler).fetch_reviewed_items()
    assert len(obs) == 1
    assert obs[0]["kind"] == "pr_reviewed"
    assert obs[0]["title"] == "Reviewed PR in o/r: Item 1 — lots of whitespace"
    assert obs[0]["entities"] == ["o/r"]


def test_commented_sweep_distinguishes_prs_and_issues(settings):
    def handler(request):
        assert "-reviewed-by:tester" in request.url.params["q"]
        if int(request.url.params["page"]) > 1:
            return httpx.Response(200, json={"items": []})
        return httpx.Response(200, json={"items": [
            _search_item(1, pr=True), _search_item(2, pr=False)]})

    obs = _mocked_collector(settings, handler).fetch_commented_items()
    assert [o["kind"] for o in obs] == ["pr_commented", "issue_commented"]
    assert obs[1]["title"].startswith("Commented on issue in o/r:")


# ── repo context + commits ───────────────────────────────────────────

def test_repo_context_readme_caps_and_absence(settings):
    readme = base64.b64encode(("word " * 200).encode()).decode()

    def handler(request):
        if request.url.path == "/repos/o/r":
            return httpx.Response(200, json={
                "description": "A test repo", "topics": ["llm"], "language": "Python"})
        if request.url.path == "/repos/o/r/readme":
            return httpx.Response(200, json={"content": readme})
        if request.url.path == "/repos/o/bare":
            return httpx.Response(200, json={"description": None, "topics": []})
        return httpx.Response(404)

    gh = _mocked_collector(settings, handler)
    ctx = gh.fetch_repo_context("o/r")
    assert ctx["description"] == "A test repo" and ctx["topics"] == ["llm"]
    assert len(ctx["readme"]) <= 400 and "  " not in ctx["readme"]
    bare = gh.fetch_repo_context("o/bare")  # README 404 → empty string
    assert bare["readme"] == "" and bare["description"] == ""
    assert gh.fetch_repo_context("o/private") is None  # repo 404 → None


def test_repo_commits_status_handling(settings):
    from datetime import datetime, timezone
    since = datetime(2025, 7, 1, tzinfo=timezone.utc)

    def handler(request):
        if "/repos/o/private/" in request.url.path:
            return httpx.Response(404)
        if "/repos/o/empty/" in request.url.path:
            return httpx.Response(409)
        page = int(request.url.params["page"])
        assert request.url.params["author"] == "tester"
        return httpx.Response(200, json=[] if page > 1 else [
            {"commit": {"author": {"date": "2025-08-01T10:00:00Z"}, "message": "m"}}])

    gh = _mocked_collector(settings, handler)
    assert gh.fetch_repo_commits("o/private", since) is None
    assert gh.fetch_repo_commits("o/empty", since) == []
    assert len(gh.fetch_repo_commits("o/r", since)) == 1


def test_summarize_commits_groups_by_month():
    commits = [
        {"commit": {"author": {"date": f"2025-07-{d:02d}T00:00:00Z"},
                    "message": f"july {d}\n\nbody"}} for d in (1, 2, 3, 4)
    ] + [{"commit": {"author": {"date": "2025-08-05T00:00:00Z"}, "message": "aug work"}}]
    obs = summarize_commits("o/r", commits)
    assert len(obs) == 2 and [o["raw"]["month"] for o in obs] == ["2025-07", "2025-08"]
    july = obs[0]
    assert july["kind"] == "commits_summary" and july["raw"]["count"] == 4
    assert july["title"].startswith("Pushed 4 commit(s) to o/r in 2025-07: july 4; july 3; july 2")
    assert july["ts"] == "2025-07-04T00:00:00Z"  # latest commit in the month
    assert summarize_commits("o/r", []) == []


# ── the end-to-end enrich flow ───────────────────────────────────────

class RecordingLLM:
    def __init__(self):
        self.prompts = []

    def complete_json(self, prompt, system=None, **kw):
        self.prompts.append(prompt)
        return {"ops": [], "notes": ""}


def _args(**over):
    defaults = {"since": "2025-07", "include_comments": False, "no_consolidate": False}
    defaults.update(over)
    return argparse.Namespace(**defaults)


def test_enrich_flow_chronological_batches_and_consolidation(settings, monkeypatch):
    from assistant.agent.profile_store import ProfileStore

    store = ProfileStore(settings.profile_dir)
    store.ensure_repo()
    store.save({"identity": {"name": "T"}, "skills": [], "interests": [],
                "projects": [], "education": [], "experience": []}, "seed")

    # 70 authored spread over months (forces 2 batches) + 5 reviewed
    authored = [{"source": "github", "kind": "pr_authored", "url": f"https://x/{i}",
                 "title": f"PR {i}", "entities": ["o/main"],
                 "ts": f"2025-{7 + i % 12:02d}-01T00:00:00Z" if i % 12 < 6
                 else f"2026-{1 + i % 6:02d}-01T00:00:00Z"} for i in range(70)]
    reviewed = [{"source": "github", "kind": "pr_reviewed", "url": f"https://r/{i}",
                 "title": f"Reviewed {i}", "entities": ["o/main"],
                 "ts": "2026-06-01T00:00:00Z"} for i in range(5)]

    monkeypatch.setattr(GitHubCollector, "__init__", lambda self, s: None)
    monkeypatch.setattr(GitHubCollector, "fetch_authored_items",
                        lambda self, since=None, max_items=None: authored)
    monkeypatch.setattr(GitHubCollector, "fetch_reviewed_items",
                        lambda self, since=None, max_items=None: reviewed)
    monkeypatch.setattr(GitHubCollector, "fetch_recent_repos",
                        lambda self, limit=100: [
                            {"full_name": "tester/mine", "fork": False,
                             "pushed_at": "2026-07-01T00:00:00Z"}])
    monkeypatch.setattr(GitHubCollector, "fetch_repo_context",
                        lambda self, name: None if "private" in name else
                        {"repo": name, "description": "desc", "topics": [],
                         "language": "", "readme": "readme text"})
    monkeypatch.setattr(
        GitHubCollector, "fetch_repo_commits",
        lambda self, name, since: [
            {"commit": {"author": {"date": "2026-07-02T00:00:00Z"}, "message": "direct push"}}])

    llm = RecordingLLM()
    monkeypatch.setattr("assistant.platform.llm.LLM.__init__", lambda self, s: None)
    monkeypatch.setattr("assistant.platform.llm.LLM.complete_json",
                        lambda self, prompt, system=None, **kw: llm.complete_json(prompt, system))

    consolidated = []
    import assistant.agent.tasks.profile_consolidate as pc
    monkeypatch.setattr(pc, "consolidate_profile",
                        lambda *a, **k: consolidated.append(1) or
                        {"applied": [], "rejected": [], "notes": "", "diff": "", "emailed": False})

    assert cmd_enrich_profile(settings, _args()) == 0

    # 76 observations → 2 batches, chronologically ascending
    assert len(llm.prompts) == 2
    assert "2025-07-01" in llm.prompts[0] and "Reviewed 0" in llm.prompts[1]
    assert "2026-07-02" in llm.prompts[1]  # commit summary landed in the last batch
    # repo context block in EVERY batch
    for prompt in llm.prompts:
        assert "## Repo context" in prompt and "readme text" in prompt
    assert consolidated == [1]

    # evidence layer persisted with dedup — re-run adds nothing
    events = EventsStore(settings.events_db)
    count = events.conn.execute("SELECT count(*) FROM observations").fetchone()[0]
    assert count == 76
    assert cmd_enrich_profile(settings, _args(no_consolidate=True)) == 0
    assert events.conn.execute("SELECT count(*) FROM observations").fetchone()[0] == 76
    events.close()


def test_enrich_rejects_bad_since(settings):
    assert cmd_enrich_profile(settings, _args(since="2025-7")) == 1
    assert cmd_enrich_profile(settings, _args(since="last year")) == 1


def test_update_profile_context_block(settings):
    from assistant.agent.profile_store import ProfileStore
    from assistant.agent.tasks.profile_update import update_profile

    store = ProfileStore(settings.profile_dir)
    store.ensure_repo()
    store.save({"identity": {}, "skills": [], "interests": [], "projects": []}, "seed")
    llm = RecordingLLM()
    update_profile(llm, store, [], context="- o/r: my repo background")
    assert "## Repo context" in llm.prompts[0]
    assert "never cite as evidence" in llm.prompts[0]
    update_profile(llm, store, [])
    assert "## Repo context" not in llm.prompts[1]
    # backfill framing: old observations must not be dismissed as stale
    update_profile(llm, store, [], backfill=True)
    assert "HISTORY BACKFILL" in llm.prompts[2]
    assert "Today's observations" not in llm.prompts[2]


def test_events_store_dedupe(settings):
    events = EventsStore(settings.events_db)
    obs = {"source": "github", "kind": "pr", "title": "t", "url": "u"}
    assert len(events.add_observations("r1", [obs, obs], dedupe=True)) == 1
    assert len(events.add_observations("r2", [obs], dedupe=True)) == 0
    assert len(events.add_observations("r3", [obs])) == 1  # default appends
    events.close()
