from datetime import date

from assistant.tasks.todos import update_todos
from assistant.todo_store import ReadingList, TodoStore
from assistant.website import render_site, sync_website


def test_todo_upsert_dedupe_done(tmp_path):
    store = TodoStore(tmp_path)
    a = store.upsert("url1", title="Review PR", source="github", priority="red")
    assert a["id"] == "t1"
    assert store.upsert("url1", title="Review PR again") is None  # open dup blocked
    b = store.upsert("url2", title="Other", source="manual")
    assert b["id"] == "t2"
    assert [i["id"] for i in store.open_items()] == ["t1", "t2"]
    assert store.mark_done("t1") and not store.mark_done("t1")
    # once done, the same key may recur (e.g. a new review round)
    assert store.upsert("url1", title="Review PR round 2")["id"] == "t3"


def test_reading_list_is_separate_store(tmp_path):
    todos, reading = TodoStore(tmp_path), ReadingList(tmp_path)
    todos.upsert("k", title="todo")
    r = reading.upsert("arxiv-1", title="paper")
    assert r["id"] == "r1"
    assert len(todos.open_items()) == 1 and len(reading.open_items()) == 1
    assert todos.path.name == "todos.yaml" and reading.path.name == "reading_list.yaml"


class FakeGitHub:
    """Deterministic stand-in for context enrichment + completion monitoring."""

    def __init__(self):
        self.finished_urls = {}

    def fetch_item_context(self, url):
        return "PR by alice · 3 files (+40/−5) · opened 2026-07-01. Fixes the scheduler race."

    def check_finished(self, url):
        return self.finished_urls.get(url, (False, ""))


def test_update_todos_from_digest_and_resume(tmp_path):
    store = TodoStore(tmp_path)
    github = FakeGitHub()
    digest = {"sections": {"red": [
        {"id": "1", "url": "https://github.com/o/r/pull/5",
         "summary": "You were requested to review a PR that fixes the scheduler.",
         "todo": "Review scheduler fix PR", "action": "review"},
    ], "yellow": [], "white": []}}
    result = update_todos(store, digest, {"status": "pending_approval"}, github=github)
    assert result["open_count"] == 2  # review todo + resume-approval todo
    review = next(t for t in result["open"] if t.get("source") == "github")
    assert review["title"] == "Review scheduler fix PR"  # short label as the title
    assert review["detail"].startswith("You were requested")  # long sentence kept
    assert "PR by alice · 3 files (+40/−5)" in review["detail"]  # enriched context

    # resume approved → its todo auto-closes; review todo not duplicated
    result = update_todos(store, digest, {"status": "no_change"}, github=github)
    assert result["open_count"] == 1
    assert result["open"][0]["url"] == "https://github.com/o/r/pull/5"
    assert not result["added"]

    # monitor pass: PR merged → todo auto-closes and is reported
    github.finished_urls["https://github.com/o/r/pull/5"] = (True, "PR merged")
    result = update_todos(store, {"sections": {"red": []}}, {"status": "no_change"},
                          github=github)
    assert result["open_count"] == 0
    assert result["closed"] == [{"id": "t1", "title": "Review scheduler fix PR",
                                 "reason": "PR merged"}]


PROFILE = {
    "identity": {"name": "Jane Doe", "github": "janedoe",
                 "emails": ["t@example.com"], "affiliations": ["ExampleU"],
                 "links": ["https://github.com/tzhouam"]},
    "skills": [{"name": "Python", "status": "active"},
               {"name": "Matlab", "status": "dormant"}],
    "experience": [{"title": "Engineer", "org": "Huawei",
                    "period": {"start": "2025-01", "end": None}, "highlights": ["built X"]}],
    "education": [{"school": "ExampleU", "degree": "BSc", "period": "2015-2019"}],
    "projects": [{"name": "vllm-omni", "role": "contributor", "status": "active",
                  "highlights": ["rebase automation"],
                  "evidence": ["https://github.com/vllm-project/vllm-omni"]}],
}


def test_render_site_pages_and_calendar():
    today = date(2026, 7, 2)
    todos = [
        {"id": "t1", "title": "Review scheduler PR", "source": "github",
         "url": "https://github.com/o/r/pull/5", "detail": "You were asked to review the scheduler fix.",
         "created": "2026-07-01", "due": "2026-07-15", "status": "open"},
        {"id": "t2", "title": "No due date", "source": "manual",
         "created": "2026-07-01", "status": "open"},
    ]
    files = render_site(PROFILE, todos, today=today)

    # one page per section, plus shared assets
    for page_name in ("index.html", "experience.html", "education.html",
                      "projects.html", "todos.html", "agent-site.css", "agent-site.js"):
        assert page_name in files

    home = files["index.html"]
    assert "Jane Doe" in home and "ExampleU" in home
    assert "Python" in home and "Matlab" not in home        # dormant skill hidden
    # sections live on their own pages now, not on the home page
    assert "Huawei" not in home and "rebase automation" not in home
    assert files["experience.html"].count("Engineer") and "Huawei" in files["experience.html"]
    assert "ExampleU" in files["education.html"] and "BSc" in files["education.html"]
    assert "vllm-omni" in files["projects.html"] and "rebase automation" in files["projects.html"]

    # every page carries the nav with its own entry marked active
    for fn in ("index.html", "experience.html", "education.html", "projects.html", "todos.html"):
        assert all(f"href='{other}'" in files[fn] for other, _ in
                   [("index.html", 0), ("experience.html", 0), ("education.html", 0),
                    ("projects.html", 0), ("todos.html", 0)])
        assert f"<a href='{fn}' class=active" in files[fn]
    # section pages get the compact banner; home keeps the full hero
    assert "hero compact" not in home and "avatar" in home
    assert "hero compact" in files["projects.html"]

    page = files["todos.html"]
    assert "July 2026" in page
    # calendar: due-dated todo on its due day (red), undated todo on created day
    assert page.count("Review scheduler PR"[:40]) == 2      # calendar chip + list entry
    assert "class='todo due'" in page                       # due chip styled distinctly
    assert page.count("No due date") == 2                   # created-date chip + list entry
    # list entries carry the short link label, the description, and pin/done buttons
    assert "[PR #5]</a>" in page
    assert "You were asked to review the scheduler fix." in page
    assert "data-tid='t1'" in page and "b-pin" in page and "b-done" in page


def test_sync_website_not_configured(settings):
    assert sync_website(settings, PROFILE, [])["status"] == "not_configured"
