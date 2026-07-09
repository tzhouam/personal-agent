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
        return {"meta": "PR by alice · 3 files (+40/−5) · opened 2026-07-01",
                "body": "## Purpose This PR fixes the scheduler race. ## Test Plan unit tests"}

    def check_finished(self, url):
        return self.finished_urls.get(url, (False, ""))


class FakeLLM:
    """Returns a written detail for every task it is asked to summarize."""

    cheap_model = "fake"

    def complete_json(self, prompt, system=None, **kw):
        ids = [line.split("id=", 1)[1] for line in prompt.splitlines()
               if line.startswith("id=")]
        return [{"id": i, "detail": "Review a small PR that fixes the scheduler race."}
                for i in ids]


def test_update_todos_from_digest_and_resume(tmp_path):
    store = TodoStore(tmp_path)
    github = FakeGitHub()
    digest = {"sections": {"red": [
        {"id": "1", "url": "https://github.com/o/r/pull/5",
         "summary": "You were requested to review a PR that fixes the scheduler.",
         "todo": "Review scheduler fix PR", "action": "review"},
    ], "yellow": [], "white": []}}
    result = update_todos(store, digest, {"status": "pending_approval"}, github=github,
                          llm=FakeLLM())
    assert result["open_count"] == 2  # review todo + resume-approval todo
    review = next(t for t in result["open"] if t.get("source") == "github")
    assert review["title"] == "Review scheduler fix PR"  # short label as the title
    # detail is the LLM-written task summary + meta line — never the raw body
    assert review["detail"].startswith("Review a small PR that fixes the scheduler race.")
    assert "PR by alice · 3 files (+40/−5)" in review["detail"]
    assert "## Purpose" not in review["detail"]

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


def test_todo_detail_fallback_without_llm(tmp_path):
    """No LLM → the notification sentence + meta line; the raw body is dropped."""
    store = TodoStore(tmp_path)
    digest = {"sections": {"red": [
        {"id": "9", "url": "https://github.com/o/r/pull/9",
         "summary": "You were asked to review X.", "todo": "Review X"},
    ]}}
    result = update_todos(store, digest, {"status": "no_change"}, github=FakeGitHub())
    detail = result["open"][0]["detail"]
    assert detail.startswith("You were asked to review X.")
    assert "PR by alice · 3 files (+40/−5)" in detail
    assert "## Purpose" not in detail


PROFILE = {
    "identity": {"name": "Jane Doe", "github": "janedoe",
                 "emails": ["t@example.com"], "affiliations": ["ExampleU"],
                 "links": ["https://github.com/tzhouam"],
                 "bio": "Engineer at Huawei working on vLLM-Omni.\nWorld-model research on the side."},
    "skills": [{"name": "Python", "status": "active"},
               {"name": "Matlab", "status": "dormant"}],
    "experience": [{"title": "Engineer", "org": "Huawei",
                    "period": {"start": "2025-01", "end": None}, "highlights": ["built X"]}],
    "education": [{"school": "ExampleU", "degree": "BSc", "period": "2015-2019"}],
    "projects": [{"name": "vllm-omni", "role": "contributor", "status": "active",
                  "highlights": ["rebase automation"],
                  "evidence": ["https://github.com/vllm-project/vllm-omni"]}],
}


def test_about_fallback_without_bio():
    profile = {k: v for k, v in PROFILE.items()}
    profile["identity"] = {k: v for k, v in PROFILE["identity"].items() if k != "bio"}
    home = render_site(profile, [], today=date(2026, 7, 3))["index.html"]
    # deterministic fallback composed from profile facts only
    assert "<h2>About</h2>" in home
    assert "Engineer at Huawei" in home and "vllm-omni" in home


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
    # self-introduction: identity.bio rendered as About paragraphs
    assert "<h2>About</h2>" in home
    assert "Engineer at Huawei working on vLLM-Omni." in home
    assert "<p class='bio'>World-model research on the side.</p>" in home
    # sections live on their own pages now, not on the home page
    assert "<h2>Experience</h2>" not in home and "rebase automation" not in home
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
    # calendar shows only important todos: due-dated chip present…
    assert page.count("Review scheduler PR"[:40]) == 2      # calendar chip + list entry
    assert "class='todo due'" in page                       # due chip styled distinctly
    # …but an undated, non-red todo is list-only (no calendar chip)
    assert page.count("No due date") == 1
    # the list is a scroll container
    assert "class='todo-scroll'" in page
    # list entries carry the short link label, the description, and pin/done buttons
    assert "[PR #5]</a>" in page
    assert "You were asked to review the scheduler fix." in page
    assert "data-tid='t1'" in page and "b-pin" in page and "b-done" in page


def test_calendar_importance_cap_and_list_order():
    today = date(2026, 7, 2)
    todos = (
        # 4 important items on the same due day → capped at 3 + "+1 more"
        [{"id": f"t{i}", "title": f"Due item {i}", "source": "github", "priority": "red",
          "created": "2026-07-01", "due": "2026-07-10", "status": "open"} for i in range(4)]
        + [{"id": "t8", "title": "Later due", "source": "github",
            "created": "2026-06-01", "due": "2026-07-20", "status": "open"},
           {"id": "t9", "title": "Old undated", "source": "manual",
            "created": "2026-06-10", "status": "open"},
           {"id": "t10", "title": "New undated", "source": "manual",
            "created": "2026-07-01", "status": "open"}]
    )
    page = render_site(PROFILE, todos, today=today)["todos.html"]
    assert "+1 more" in page
    assert "Key items only" in page
    # scroll list ordered by date: due items soonest-first, then undated newest-first
    order = [page.rindex(t)
             for t in ("Due item 0", "Later due", "New undated", "Old undated")]
    assert order == sorted(order)
    # undated items never earn calendar chips: single occurrence each
    assert page.count("Old undated") == 1 and page.count("New undated") == 1
    # same-day todos embed into one collapsible group: 4 due days collapse to
    # one <details> with a count, one group per distinct day overall
    assert page.count("<details class='t-day'") == 4
    assert "2026-07-10 · Fri · due <span class='t-count'>(4)</span>" in page
    # nearest groups start open; each group holds its own ul for the pin JS
    assert page.count("<details class='t-day' open>") == 4
    assert page.count("<ul class='todos'>") == 4


def test_reading_page_like_todos():
    today = date(2026, 7, 9)
    reading = [
        {"id": "r1", "title": "Fresh paper", "url": "https://arxiv.org/abs/1",
         "why": "relates to your KV-cache work", "source": "arxiv",
         "created": "2026-07-08", "status": "open"},
        {"id": "r2", "title": "Old survey", "url": "https://arxiv.org/abs/2",
         "why": "", "source": "arxiv", "created": "2026-06-10", "status": "open"},
    ]
    files = render_site(PROFILE, [], today=today, reading=reading)
    assert "reading.html" in files
    page = files["reading.html"]
    # nav on every page includes Reading, and the page marks itself active
    assert "<a href='reading.html' class=active" in page
    assert "href='reading.html'" in files["index.html"]
    # day-grouped scroll list, newest day first, with the todo buttons
    assert page.index("2026-07-08") < page.index("2026-06-10")
    # the reading list gets the tall scroll variant (far more items than todos)
    assert "class='todo-scroll tall'" in page and "<details class='t-day'" in page
    assert "data-tid='r1'" in page and "b-done" in page
    assert "relates to your KV-cache work" in page
    # reading items never show the todo staleness badge
    assert "going stale" not in page
    # empty state renders a placeholder, not a broken section
    assert "Nothing unread" in render_site(PROFILE, [], today=today)["reading.html"]


def test_routines_page():
    routines = [{"id": "rt1", "task": "check Shenzhen storm warnings", "time": "22:00",
                 "days": "workdays", "condition": "深圳市发布暴雨或台风预警",
                 "last_checked": "2026-07-09"}]
    reminders = [{"id": "m4", "message": "submit the report", "due_at": "2026-07-10 09:00"}]
    files = render_site(PROFILE, [], today=date(2026, 7, 9),
                        routines=routines, reminders=reminders)
    page = files["routines.html"]
    assert "<a href='routines.html' class=active" in page
    assert "🔁 workdays 22:00" in page and "check Shenzhen storm warnings" in page
    assert "if: 深圳市发布暴雨或台风预警" in page and "last checked 2026-07-09" in page
    assert "⏰ 2026-07-10 09:00" in page and "submit the report" in page
    # nav includes Routines on other pages too; empty state is friendly
    assert "href='routines.html'" in files["index.html"]
    empty = render_site(PROFILE, [], today=date(2026, 7, 9))["routines.html"]
    assert "No routines yet" in empty


def test_todo_expiry_after_a_month(tmp_path):
    store = TodoStore(tmp_path)
    store.upsert("k-old", title="Stale item", source="github")
    store.upsert("k-new", title="Fresh item", source="github")
    store.upsert("k-due", title="Old but scheduled", source="manual", due="2026-08-01")
    data = store.load()  # age two items past the cutoff
    for item in data["items"]:
        if item["key"] in ("k-old", "k-due"):
            item["created"] = "2026-05-01"
    store._save(data, "age items for test")

    expired = store.expire_stale(days=30, today=date(2026, 7, 2))
    assert [t["title"] for t in expired] == ["Stale item"]
    remaining = store.open_items()
    assert {t["title"] for t in remaining} == {"Fresh item", "Old but scheduled"}
    stale = next(i for i in store.load()["items"] if i["key"] == "k-old")
    assert stale["status"] == "outdated" and stale["outdated_at"] == "2026-07-02"
    # second pass is a no-op
    assert store.expire_stale(days=30, today=date(2026, 7, 2)) == []
    # two weeks past due the scheduled item is still within its 30-day grace
    # (the fresh undated item has decayed out by then)
    expired = store.expire_stale(days=30, today=date(2026, 8, 15))
    assert {t["title"] for t in expired} == {"Fresh item"}
    # a month past due, even scheduled work is dead
    expired = store.expire_stale(days=30, today=date(2026, 9, 5))
    assert {t["title"] for t in expired} == {"Old but scheduled"}


def test_update_todos_reports_expired_as_closed(tmp_path):
    store = TodoStore(tmp_path)
    store.upsert("k-old", title="Ancient todo", source="github")
    data = store.load()
    data["items"][0]["created"] = "2020-01-01"
    store._save(data, "age")
    result = update_todos(store, digest={}, resume={})
    assert result["open_count"] == 0
    assert result["closed"] == [{"id": "t1", "title": "Ancient todo",
                                 "reason": "outdated (open >30 days)"}]


def test_sync_website_not_configured(settings):
    assert sync_website(settings, PROFILE, [])["status"] == "not_configured"
