import json
import threading

import httpx
import pytest

from assistant.platform.serve import SessionStore, make_server
from assistant.agent.state import persist_state


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


def _put_shard(store, sid, day, turns):
    """Write turns directly into a session's day-shard (test seam)."""
    import json
    store._sdir(sid).mkdir(parents=True, exist_ok=True)
    (store._sdir(sid) / f"{day}.json").write_text(
        json.dumps({"session": sid, "turns": turns}))


def test_session_store_trims_and_survives_corruption(settings):
    from datetime import datetime

    store = SessionStore(settings.data_dir, keep=3)
    for i in range(5):
        store.append("s", f"q{i}", f"a{i}")
    assert [t["owner"] for t in store.history("s")] == ["q2", "q3", "q4"]
    assert all(t.get("ts") for t in store.history("s"))  # turns are timestamped
    # corrupt the (single, today) shard → tolerated, history empty
    today = datetime.now().astimezone().date().isoformat()
    (store._sdir("s") / f"{today}.json").write_text("{corrupt")
    assert store.history("s") == []


def test_context_window_excludes_but_retention_keeps(settings):
    # a turn past the ~2-day context window never reaches a prompt, but stays
    # on disk (retained ~1 month, in its own day-shard) and survives appends.
    from datetime import datetime, timedelta, timezone

    store = SessionStore(settings.data_dir, context_hours=48, retention_days=30)
    store.append("s", "fresh question", "fresh answer")
    old = datetime.now(timezone.utc) - timedelta(hours=49)  # >2d, <30d
    _put_shard(store, "s", old.astimezone().date().isoformat(),
               [{"ts": old.isoformat(), "owner": "day-old q", "assistant": "a"}])
    # prompt context: only the in-window turn
    assert [t["owner"] for t in store.history("s")] == ["fresh question"]
    # retention: the day-old turn is still on disk (across shards, oldest first)
    assert [t["owner"] for t in store._all("s")] == ["day-old q", "fresh question"]
    # ...and an append preserves it
    store.append("s", "q3", "a3")
    assert [t["owner"] for t in store._all("s")] == ["day-old q", "fresh question", "q3"]
    # legacy turns without a ts → the unknown shard, expired for prompts
    _put_shard(store, "legacy", "unknown", [{"owner": "x", "assistant": "y"}])
    assert store.history("legacy") == []


def test_prune_uses_retention_not_context(settings):
    # prune drops whole shards past the retention window (~30d), NOT the context
    # window; a one-day-old shard is kept even though out of the prompt window.
    from datetime import datetime, timedelta, timezone

    store = SessionStore(settings.data_dir, context_hours=48, retention_days=30)
    now = datetime.now(timezone.utc)
    today = now.astimezone().date().isoformat()
    d40, d31 = now - timedelta(days=40), now - timedelta(days=31)
    # 's': a recent shard + an ancient (40d) shard; 'old': entirely 31d ago
    _put_shard(store, "s", today, [{"ts": now.isoformat(), "owner": "recent", "assistant": "a"}])
    _put_shard(store, "s", d40.astimezone().date().isoformat(),
               [{"ts": d40.isoformat(), "owner": "ancient", "assistant": "a"}])
    _put_shard(store, "old", d31.astimezone().date().isoformat(),
               [{"ts": d31.isoformat(), "owner": "gone", "assistant": "a"}])

    pruned = store.prune()
    assert pruned == {"turns": 2, "files": 2}  # ancient shard in 's' + old's shard
    assert store._sdir("s").exists() and not store._sdir("old").exists()
    assert [t["owner"] for t in store._all("s")] == ["recent"]  # in-window shard kept
    assert store.prune() == {"turns": 0, "files": 0}  # idempotent


def test_chat_accepts_image_paths(server, tmp_path, monkeypatch):
    base, llm, _ = server
    pic = tmp_path / "shot.png"
    pic.write_bytes(b"\x89PNG fake")
    monkeypatch.setattr("assistant.platform.vision.describe_images",
                        lambda s, p: ["a build log full of errors"])
    r = httpx.post(f"{base}/chat", json={"session": "s1", "text": "看看这个",
                                         "image_paths": [str(pic)]},
                   timeout=10)
    assert r.status_code == 200
    assert "## Attached images" in llm.prompts[-1]
    assert "build log" in llm.prompts[-1]


def test_chat_accepts_base64_images_and_image_only(server, monkeypatch):
    base, llm, settings = server
    import base64 as b64
    monkeypatch.setattr("assistant.platform.vision.describe_images", lambda s, p: ["a chart"])
    body = {"session": "s2", "text": "",
            "images": [{"media_type": "image/png",
                        "data": b64.b64encode(b"\x89PNG fake").decode()}]}
    r = httpx.post(f"{base}/chat", json=body, timeout=10)
    assert r.status_code == 200
    assert "a chart" in llm.prompts[-1]
    # decoded file staged under DATA_DIR/media
    assert list((settings.data_dir / "media").glob("chat-*.png"))
    # neither text nor images → still a 400
    assert httpx.post(f"{base}/chat", json={"text": ""}).status_code == 400


# ── per-turn outcome labels persisted in the session store ──────────────────

def test_chat_persists_outcome_and_owner_verdict(server):
    base, llm, settings = server
    llm.result = {"reply": "记好了", "actions": [], "self_check": "success"}
    httpx.post(f"{base}/chat", json={"session": "wechat:me", "text": "帮我记一笔45"})
    store = SessionStore(settings.data_dir)
    turns = store.history("wechat:me")
    assert turns[-1]["outcome"] == "success" and turns[-1].get("self") is True

    # the owner's correction flips the PREVIOUS turn to fail (original kept)
    llm.result = {"reply": "改好了", "actions": [], "self_check": "success"}
    httpx.post(f"{base}/chat", json={"session": "wechat:me", "text": "不对，是54"})
    turns = store.history("wechat:me")
    prev, cur = turns[-2], turns[-1]
    assert prev["owner_verdict"] == "dissatisfied"
    assert prev["outcome"] == "fail" and prev["outcome_initial"] == "success"
    assert cur["outcome"] == "success"


def test_session_store_verdict_and_legacy_turns(settings):
    store = SessionStore(settings.data_dir)
    # legacy signature still works; old turns have no label keys
    store.append("s", "hi", "hello")
    assert "outcome" not in store.history("s")[-1]
    # satisfied confirms a provisional neutral up to success
    store.append("s", "谢谢", "不客气", outcome="neutral", prev_verdict="satisfied")
    turns = store.history("s")
    assert turns[0]["owner_verdict"] == "satisfied"
    assert turns[0].get("outcome") is None or turns[0].get("outcome") == "success"
    # a code-observed fail is never upgraded by a satisfied verdict
    store.append("s2", "do it", "boom", outcome="fail")
    store.append("s2", "谢谢", "ok", outcome="neutral", prev_verdict="satisfied")
    prev = store.history("s2")[0]
    assert prev["outcome"] == "fail" and "outcome_initial" not in prev


# ── per-day session sharding: migration, cross-shard verdict, reverse ──────

def test_session_migration_splits_legacy_flat_file(settings):
    import hashlib
    import json
    from datetime import datetime, timedelta, timezone

    store = SessionStore(settings.data_dir)
    now = datetime.now(timezone.utc)
    h = hashlib.sha1("s".encode()).hexdigest()[:16]
    store.dir.mkdir(parents=True, exist_ok=True)
    (store.dir / f"{h}.json").write_text(json.dumps({"session": "s", "turns": [
        {"ts": (now - timedelta(days=2)).isoformat(), "owner": "old", "assistant": "a"},
        {"ts": now.isoformat(), "owner": "new", "assistant": "b"},
        {"owner": "legacy-no-ts", "assistant": "c"}]}))
    # a public call migrates: two dated shards + an unknown shard, legacy → .bak
    store._ensure_migrated()
    assert [t["owner"] for t in store._all("s")] == ["legacy-no-ts", "old", "new"]
    assert (store.dir / h / "unknown.json").exists()
    assert (store.dir / f"{h}.json.bak").exists()
    assert not (store.dir / f"{h}.json").exists()


def test_prev_verdict_finalizes_turn_in_earlier_shard(settings):
    from datetime import datetime, timedelta, timezone

    store = SessionStore(settings.data_dir, retention_days=30)
    y = datetime.now(timezone.utc) - timedelta(days=1)
    _put_shard(store, "s", y.astimezone().date().isoformat(),
               [{"ts": y.isoformat(), "owner": "帮我记45", "assistant": "记好了",
                 "outcome": "success"}])
    # a correction TODAY dissatisfies yesterday's turn → its (earlier) shard flips
    store.append("s", "不对是54", "改好了", outcome="success", prev_verdict="dissatisfied")
    prev = store._all("s")[0]
    assert prev["owner_verdict"] == "dissatisfied"
    assert prev["outcome"] == "fail" and prev["outcome_initial"] == "success"


def test_prev_verdict_ignores_turn_older_than_retention(settings):
    from datetime import datetime, timedelta, timezone

    store = SessionStore(settings.data_dir, retention_days=30)
    old = datetime.now(timezone.utc) - timedelta(days=40)   # beyond retention
    _put_shard(store, "s", old.astimezone().date().isoformat(),
               [{"ts": old.isoformat(), "owner": "ancient", "assistant": "a",
                 "outcome": "success"}])
    store.append("s", "谢谢", "不客气", prev_verdict="dissatisfied")
    stale = store._all("s")[0]
    assert "owner_verdict" not in stale and stale["outcome"] == "success"  # untouched


def test_max_turns_enforced_across_shards(settings):
    from datetime import datetime, timezone

    store = SessionStore(settings.data_dir, max_turns=3)
    now = datetime.now(timezone.utc)
    _put_shard(store, "s", "2026-07-18", [{"ts": "2026-07-18T00:00:00+00:00",
                                           "owner": f"a{i}", "assistant": "x"} for i in range(3)])
    store.append("s", "newest", "y")   # total would be 4 > max_turns=3 → oldest dropped
    owners = [t["owner"] for t in store._all("s")]
    assert len(owners) == 3 and owners[-1] == "newest" and "a0" not in owners


def test_session_reverse_migration(settings):
    from datetime import datetime, timezone

    store = SessionStore(settings.data_dir)
    store.append("s", "q1", "a1")
    store.append("s", "q2", "a2")
    n = store.to_flat_files()
    import hashlib
    import json
    h = hashlib.sha1("s".encode()).hexdigest()[:16]
    assert n == 1 and not (store.dir / h).exists()   # shards folded back
    flat = json.loads((store.dir / f"{h}.json").read_text())
    assert [t["owner"] for t in flat["turns"]] == ["q1", "q2"]
