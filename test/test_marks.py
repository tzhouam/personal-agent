"""Website marks sync: browser-queued done/unrelated clicks land in the
private marks repo; collect_marks pulls and applies them idempotently."""

import base64
import json
from datetime import date

import httpx

from assistant.agent.events_store import EventsStore
from assistant.agent.marks import collect_marks
from assistant.agent.todo_store import ReadingList, TodoStore
from assistant.agent.website import render_site

PROFILE = {"identity": {"name": "T", "github": "t", "emails": ["me@example.com"],
                        "affiliations": []},
           "skills": [], "interests": [], "projects": [],
           "education": [], "experience": []}


def _b64(marks):
    return base64.b64encode(json.dumps(marks).encode()).decode()


_RealClient = httpx.Client  # patched tests must not recurse into themselves


def _client(files):
    def handler(request):
        if request.url.path.endswith("/contents/marks"):
            return httpx.Response(200, json=[
                {"type": "file", "path": f"marks/{name}",
                 "url": f"https://api.github.com/blob/{name}"}
                for name in files])
        name = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json={"content": _b64(files[name])})
    return _RealClient(transport=httpx.MockTransport(handler))


def test_collect_marks_applies_and_is_idempotent(settings, monkeypatch):
    marked = settings.model_copy(update={"marks_repo": "t/agent-marks"})
    todos = TodoStore(settings.profile_dir)
    reading = ReadingList(settings.profile_dir)
    todos.upsert("k1", title="A todo", source="manual")
    reading.upsert("p1", title="Paper one")
    reading.upsert("p2", title="Paper two")

    files = {"a.json": [{"id": "r1", "action": "done"},
                        {"id": "r2", "action": "unrelated"},
                        {"id": "t1", "action": "done"},
                        {"id": "t9", "action": "done"}],   # unknown id → skipped
             "b.json": "not a list"}                        # corrupt → tolerated
    monkeypatch.setattr(httpx, "Client", lambda **kw: _client(files))

    events = EventsStore(settings.events_db)
    result = collect_marks(marked, events)
    assert result == {"applied": 3, "files": 2}
    assert todos.open_items() == []
    statuses = {i["id"]: i["status"] for i in reading.load()["items"]}
    assert statuses == {"r1": "done", "r2": "unrelated"}

    # second pass: same files already seen → nothing reprocessed
    assert collect_marks(marked, events) == {"applied": 0, "files": 0}
    events.close()


def test_collect_marks_disabled_and_empty(settings, monkeypatch):
    events = EventsStore(settings.events_db)
    assert collect_marks(settings, events) == {"applied": 0, "files": 0}  # no repo set
    marked = settings.model_copy(update={"marks_repo": "t/agent-marks"})
    monkeypatch.setattr(httpx, "Client", lambda **kw: _RealClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404))))
    assert collect_marks(marked, events) == {"applied": 0, "files": 0}  # repo empty
    events.close()


def test_marks_token_ships_only_inside_ciphertext():
    reading = [{"id": "r1", "title": "P", "url": "u", "why": "", "source": "arxiv",
                "created": "2026-07-09", "status": "open"}]
    cfg = {"repo": "t/agent-marks", "token": "SECRET-TOKEN"}

    # with a password: the token must not appear anywhere in plaintext output
    files = render_site(PROFILE, [], today=date(2026, 7, 9), reading=reading,
                        password="pw", marks_cfg=cfg)
    for content in files.values():
        assert "SECRET-TOKEN" not in content
    assert "data-ct=" in files["reading.html"]  # encrypted payload present

    # without a password: config is dropped entirely, never shipped plaintext
    files = render_site(PROFILE, [], today=date(2026, 7, 9), reading=reading,
                        marks_cfg=cfg)
    for content in files.values():
        assert "SECRET-TOKEN" not in content
    assert "id='marks-cfg'" not in files["reading.html"]