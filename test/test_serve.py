import json
import threading

import httpx
import pytest

from assistant.serve import SessionStore, make_server
from assistant.state import persist_state


class FakeLLM:
    def __init__(self, result=None):
        self.result = result or {"reply": "ok", "actions": []}
        self.prompts = []

    def complete_json(self, prompt, system=None, **kw):
        self.prompts.append(prompt)
        return self.result


@pytest.fixture
def server(settings):
    llm = FakeLLM()
    srv = make_server(settings_factory=lambda: settings,
                      llm_factory=lambda s: llm, port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield base, llm, settings
    srv.shutdown()


def test_healthz_and_status(server):
    base, _, _ = server
    assert httpx.get(f"{base}/healthz").json() == {"ok": True}
    status = httpx.get(f"{base}/status").json()["status"]
    assert status == "no runs yet"


def test_actions_roundtrip_and_errors(server):
    base, _, _ = server
    r = httpx.post(f"{base}/actions/add_todo", json={"title": "Buy GPU"})
    assert r.status_code == 200 and r.json()["result"] == "added todo t1: Buy GPU"
    assert "[t1] Buy GPU" in httpx.post(f"{base}/actions/list_todos", json={}).json()["result"]

    assert httpx.post(f"{base}/actions/rm_rf", json={}).status_code == 404
    r = httpx.post(f"{base}/actions/add_todo", json={})
    assert r.status_code == 400 and "missing required 'title'" in r.json()["error"]
    r = httpx.post(f"{base}/actions/add_todo", content=b"not json",
                   headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_chat_keeps_session_history(server):
    base, llm, settings = server
    r = httpx.post(f"{base}/chat", json={"session": "wechat:me", "text": "first question"})
    assert r.json()["reply"] == "ok"
    llm.result = {"reply": "second answer", "actions": []}
    r = httpx.post(f"{base}/chat", json={"session": "wechat:me", "text": "and the second one?"})
    assert r.json()["reply"] == "second answer"
    # second prompt carried the first exchange
    assert "Recent conversation" in llm.prompts[1]
    assert "first question" in llm.prompts[1]
    # a different session sees none of it
    httpx.post(f"{base}/chat", json={"session": "email:me", "text": "hello"})
    assert "first question" not in llm.prompts[2]
    # history spilled to disk
    store = SessionStore(settings.data_dir)
    assert [t["owner"] for t in store.history("wechat:me")] \
        == ["first question", "and the second one?"]
    assert httpx.post(f"{base}/chat", json={"session": "x", "text": ""}).status_code == 400


def test_run_endpoint_respects_run_guard(server):
    base, _, settings = server
    persist_state(settings.state_file, run_id="run-z", phase="deliver")
    result = httpx.post(f"{base}/run", json={}).json()["result"]
    assert result == "a run is already in progress (run-z)"


def test_bearer_token_enforced(settings):
    settings_locked = settings.model_copy(update={"serve_token": "s3cret"})
    srv = make_server(settings_factory=lambda: settings_locked,
                      llm_factory=lambda s: FakeLLM(), port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        assert httpx.get(f"{base}/healthz").status_code == 200  # liveness stays open
        assert httpx.get(f"{base}/status").status_code == 401
        assert httpx.post(f"{base}/actions/list_todos", json={}).status_code == 401
        ok = httpx.get(f"{base}/status",
                       headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200
    finally:
        srv.shutdown()


def test_session_store_trims_and_survives_corruption(settings):
    store = SessionStore(settings.data_dir, keep=3)
    for i in range(5):
        store.append("s", f"q{i}", f"a{i}")
    assert [t["owner"] for t in store.history("s")] == ["q2", "q3", "q4"]
    assert all(t.get("ts") for t in store.history("s"))  # turns are timestamped
    store._path("s").write_text("{corrupt")
    assert store.history("s") == []


def test_session_turns_expire_after_48h(settings):
    import json
    from datetime import datetime, timedelta, timezone

    store = SessionStore(settings.data_dir, max_age_hours=48)
    store.append("s", "fresh question", "fresh answer")
    store.append("other", "old only", "old answer")
    # age one turn in each file past the cutoff
    for sid, make_all_old in (("s", False), ("other", True)):
        path = store._path(sid)
        data = json.loads(path.read_text())
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()
        if make_all_old:
            for t in data["turns"]:
                t["ts"] = old_ts
        else:
            data["turns"].insert(0, {"ts": old_ts, "owner": "stale q", "assistant": "stale a"})
        path.write_text(json.dumps(data))

    # read-time filter: stale turns never reach a prompt
    assert [t["owner"] for t in store.history("s")] == ["fresh question"]
    assert store.history("other") == []
    # legacy turns without ts are treated as expired
    store._path("legacy").parent.mkdir(parents=True, exist_ok=True)
    store._path("legacy").write_text(json.dumps(
        {"session": "legacy", "turns": [{"owner": "x", "assistant": "y"}]}))
    assert store.history("legacy") == []

    # daily prune: stale turns leave disk, empty sessions are deleted
    pruned = store.prune()
    assert pruned == {"turns": 3, "files": 2}  # 1 stale turn + 2 all-stale files
    assert store._path("s").exists()
    assert not store._path("other").exists() and not store._path("legacy").exists()
    assert store.prune() == {"turns": 0, "files": 0}  # idempotent
