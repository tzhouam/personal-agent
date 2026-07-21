"""Digest email rendering (src/assistant/deliver/email.py): todo grouping and
the no-red-section rule (red notifications already are the todo list)."""

from assistant.agent.deliver.email import render_html
from assistant.agent.todo_store import group_todos


def _render(todos=None, digest=None):
    return render_html("2026-07-21", digest or {}, {}, {}, todos or {}, [], {},
                       "", [], {"run": "x"})


def _open_todos():
    return [
        {"id": "t1", "title": "Review streaming PR", "type": "PullRequest",
         "source": "github", "priority": "red", "created": "2026-07-15"},
        {"id": "t2", "title": "Comment on KV cache RFC", "type": "Issue",
         "source": "github", "priority": "red", "created": "2026-07-15"},
        {"id": "t3", "title": "Fix pre-commit CI failure", "type": "CheckSuite",
         "source": "github", "priority": "red", "created": "2026-07-15"},
        {"id": "t4", "title": "喝水", "source": "chat", "priority": "yellow",
         "created": "2026-07-15"},
    ]


def test_group_todos_splits_by_kind_and_drops_empty_groups():
    groups = group_todos(_open_todos())
    assert [(label, [t["id"] for t in items]) for label, items in groups] == [
        ("🔍 PR reviews", ["t1"]),
        ("💬 Issues / RFCs", ["t2"]),
        ("⚙️ CI failures", ["t3"]),
        ("📌 Personal / other", ["t4"]),
    ]
    # only the catch-all group when nothing is GitHub-typed
    assert [label for label, _ in group_todos([_open_todos()[3]])] \
        == ["📌 Personal / other"]


def test_email_renders_grouped_todo_sections():
    body = _render(todos={"open": _open_todos(), "added": ["t1"], "closed": []})
    assert "🔍 PR reviews (1)" in body
    assert "💬 Issues / RFCs (1)" in body
    assert "⚙️ CI failures (1)" in body
    assert "📌 Personal / other (1)" in body
    assert "Review streaming PR" in body and "喝水" in body


def test_email_omits_red_notification_section():
    digest = {"sections": {
        "red": [{"id": "1", "summary": "review requested on the KV PR",
                 "repo": "o/r", "url": "http://x", "type": "PullRequest"}],
        "yellow": [{"id": "2", "summary": "a merged PR worth knowing",
                    "repo": "o/r", "url": "http://y", "type": "PullRequest"}],
        "white": []}, "total": 2}
    body = _render(digest=digest)
    # red items are the todo list — never rendered as their own section
    assert "🔴 Action needed" not in body
    assert "review requested on the KV PR" not in body
    assert "🟡 Worth knowing (1)" in body
    assert "a merged PR worth knowing" in body
    # red-only day: no section, but also no false "no notifications" cheer
    body = _render(digest={"sections": {"red": digest["sections"]["red"],
                                        "yellow": [], "white": []}, "total": 1})
    assert "🔴 Action needed" not in body
    assert "No GitHub notifications" not in body
    # a truly empty day still cheers
    assert "No GitHub notifications" in _render(
        digest={"sections": {"red": [], "yellow": [], "white": []}, "total": 0})
