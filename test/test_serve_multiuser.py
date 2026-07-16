"""Multi-tenant `serve` — every endpoint resolves to one authenticated user;
accounts are isolated; forged identity without the bridge token is refused
(multi-user §7, Appendix A.2, A.4)."""
import threading

import httpx
import pytest

from assistant.config import Settings
from assistant.registry import UserRegistry
from assistant.serve import SessionStore, make_server


class FakeLLM:
    def __init__(self):
        self.result = {"reply": "ok", "actions": []}
        self.prompts = []
        self.reply_by_prompt = None

    def complete_json(self, prompt, system=None, **kw):
        self.prompts.append(prompt)
        return self.result


BRIDGE = "bridge-secret-token"


@pytest.fixture
def mt_server(tmp_path, monkeypatch):
    """A `multi_tenant` daemon with two accounts wired to two uids.

    Mode/data_dir come from the environment (the shared `.env` in production) so
    the `settings_factory` and the `Settings.for_user()` call inside `_resolve`
    read the *same* config — exactly how a real deployment is wired."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    monkeypatch.setenv("DEPLOYMENT_MODE", "multi_tenant")
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    reg = UserRegistry(data_dir)
    reg.add_user("alice1")
    reg.add_user("bob123")
    reg.bind_channel("alice1", "weixin", "wx-A")
    reg.bind_channel("bob123", "weixin", "wx-B")
    reg.set_bridge_token(BRIDGE)

    llm = FakeLLM()
    srv = make_server(settings_factory=lambda: Settings(_env_file=None),
                      llm_factory=lambda s: llm, port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield base, llm, data_dir
    srv.shutdown()


def _auth(tok=BRIDGE):
    return {"Authorization": f"Bearer {tok}"}


def test_healthz_open_but_everything_else_needs_the_bridge_token(mt_server):
    base, _, _ = mt_server
    assert httpx.get(f"{base}/healthz").json() == {"ok": True}
    # no token → 401 on chat, actions, run, status
    assert httpx.post(f"{base}/chat", json={"account_id": "wx-A", "text": "hi"}).status_code == 401
    assert httpx.post(f"{base}/actions/list_todos", json={"account_id": "wx-A"}).status_code == 401
    assert httpx.post(f"{base}/run", json={"account_id": "wx-A"}).status_code == 401
    assert httpx.get(f"{base}/status?account_id=wx-A").status_code == 401


def test_forged_account_id_without_token_rejected(mt_server):
    base, _, _ = mt_server
    # knows a real accountId, but has no bridge token → refused, no fallback user
    r = httpx.post(f"{base}/chat", json={"account_id": "wx-A", "text": "hi"})
    assert r.status_code == 401
    # valid token but unknown account → still refused (no default uid)
    r = httpx.post(f"{base}/chat", json={"account_id": "ghost", "text": "hi"},
                   headers=_auth())
    assert r.status_code == 401


def test_two_accounts_get_isolated_sessions(mt_server):
    base, llm, data_dir = mt_server
    # Alice speaks
    r = httpx.post(f"{base}/chat",
                   json={"account_id": "wx-A", "session": "peer1", "text": "alice-secret"},
                   headers=_auth())
    assert r.status_code == 200 and r.json()["reply"] == "ok"
    # Bob speaks on the same session name — must NOT see Alice's turn
    httpx.post(f"{base}/chat",
               json={"account_id": "wx-B", "session": "peer1", "text": "bob-msg"},
               headers=_auth())
    assert "alice-secret" not in llm.prompts[-1]

    # each user's history spilled under their OWN data dir, uid-prefixed key
    alice_store = SessionStore(data_dir / "users" / "alice1")
    bob_store = SessionStore(data_dir / "users" / "bob123")
    assert [t["owner"] for t in alice_store.history("alice1:peer1")] == ["alice-secret"]
    assert [t["owner"] for t in bob_store.history("bob123:peer1")] == ["bob-msg"]
    # cross-check: bob's dir has nothing of alice's
    assert alice_store.history("bob123:peer1") == []


def test_actions_scoped_per_user(mt_server):
    base, _, data_dir = mt_server
    httpx.post(f"{base}/actions/add_todo",
               json={"account_id": "wx-A", "title": "alice-todo"}, headers=_auth())
    httpx.post(f"{base}/actions/add_todo",
               json={"account_id": "wx-B", "title": "bob-todo"}, headers=_auth())
    a = httpx.post(f"{base}/actions/list_todos",
                   json={"account_id": "wx-A"}, headers=_auth()).json()["result"]
    b = httpx.post(f"{base}/actions/list_todos",
                   json={"account_id": "wx-B"}, headers=_auth()).json()["result"]
    assert "alice-todo" in a and "bob-todo" not in a
    assert "bob-todo" in b and "alice-todo" not in b
    # and each user's todos live under their own data dir
    assert (data_dir / "users" / "alice1").exists()
    assert (data_dir / "users" / "bob123").exists()


def test_status_resolves_per_user_via_query(mt_server):
    base, _, _ = mt_server
    r = httpx.get(f"{base}/status?account_id=wx-A", headers=_auth())
    assert r.status_code == 200 and r.json()["status"] == "no runs yet"


def test_run_endpoint_enqueues_for_resolved_uid(mt_server):
    base, _, data_dir = mt_server
    r = httpx.post(f"{base}/run", json={"account_id": "wx-A"}, headers=_auth())
    assert r.status_code == 200 and "queued" in r.json()["result"]
    # the durable job landed on the shared queue, owned by the resolved uid
    from assistant.jobs import JobQueue
    job = JobQueue(data_dir / "shared").claim()
    assert job["uid"] == "alice1" and job["kind"] == "run"


def test_filesystem_image_paths_refused_in_multi_tenant(mt_server, tmp_path, monkeypatch):
    base, llm, _ = mt_server
    # a network-supplied local path is a traversal/cross-user vector → ignored
    secret = tmp_path / "outside.png"
    secret.write_bytes(b"\x89PNG fake")
    monkeypatch.setattr("assistant.vision.describe_images", lambda s, p: ["LEAK"])
    r = httpx.post(f"{base}/chat",
                   json={"account_id": "wx-A", "text": "look", "image_paths": [str(secret)]},
                   headers=_auth(), timeout=10)
    assert r.status_code == 200
    # the caller-supplied path was dropped: no image reached the vision chain
    assert "LEAK" not in llm.prompts[-1]
    assert "Attached images" not in llm.prompts[-1]


def test_tick_tenants_per_user_and_daily_fanout(tmp_path, monkeypatch):
    """Reminders/routines tick with each ACTIVE user's own settings (root-scoped
    ticking never fired tenant reminders), and past `daily_run_hour` the daily
    run fans out once per user, idempotently (§12)."""
    from datetime import datetime

    from assistant.jobs import JobQueue
    from assistant.serve import _tick_tenants

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("DEPLOYMENT_MODE", "multi_tenant")
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    # never let the repo .env's real SMTP creds reach a test poller (env beats
    # dotenv layers, so this forces EmailChannel.enabled=False for every user)
    monkeypatch.setenv("SMTP_USER", "")
    monkeypatch.setenv("SMTP_PASSWORD", "")
    reg = UserRegistry(data_dir)
    reg.add_user("alice1")
    reg.add_user("bob123")
    reg.add_user("carol1")
    reg.set_status("carol1", "disabled")

    ticked, fired = [], []

    class FakeStore:
        def __init__(self, d):
            self.d = d

        def deliver_due(self, s):
            ticked.append((s.uid, s.data_dir))
            return []

    monkeypatch.setattr("assistant.notify.ReminderStore", FakeStore)
    monkeypatch.setattr("assistant.routines.fire_due", lambda s: fired.append(s.uid))

    root = Settings(_env_file=None)
    # before daily_run_hour (default 7): reminders/routines only, no fan-out
    _tick_tenants(root, now=datetime(2026, 7, 16, 5, 0))
    assert JobQueue(root.shared_dir).counts() == {}
    assert [u for u, _ in ticked] == ["alice1", "bob123"]     # disabled skipped
    assert all(d.name == u for u, d in ticked)                # per-user data dirs
    assert fired == ["alice1", "bob123"]
    # past the hour: one deduped daily run per active user, repeat ticks no-op
    _tick_tenants(root, now=datetime(2026, 7, 16, 8, 0))
    _tick_tenants(root, now=datetime(2026, 7, 16, 9, 0))
    assert JobQueue(root.shared_dir).counts() == {"queued": 2}


def test_chat_accepts_image_bytes_and_caps_size(mt_server, monkeypatch):
    """The multi_tenant image path: base64 bytes are accepted and staged under
    the RESOLVED user's media dir; an oversized image is dropped (§A.4)."""
    import base64 as b64

    base, llm, data_dir = mt_server
    monkeypatch.setattr("assistant.vision.describe_images", lambda s, p: ["a receipt"])
    monkeypatch.setattr("assistant.serve._MAX_IMAGE_BYTES", 64)   # keep the test tiny
    small = {"media_type": "image/png", "data": b64.b64encode(b"\x89PNG ok").decode()}
    big = {"media_type": "image/png",
           "data": b64.b64encode(b"\x89PNG" + b"x" * 200).decode()}
    r = httpx.post(f"{base}/chat",
                   json={"account_id": "wx-A", "text": "看看", "images": [small, big]},
                   headers=_auth(), timeout=10)
    assert r.status_code == 200
    # the surviving image reached the vision chain (described or attached natively)
    assert "Attached images" in llm.prompts[-1]
    staged = list((data_dir / "users" / "alice1" / "media").glob("chat-*.png"))
    assert len(staged) == 1                       # small staged, oversized dropped


def test_tick_tenants_polls_each_users_own_mailbox(tmp_path, monkeypatch):
    """Per-user email pollers (§11.7): each active user's mailbox is polled with
    THEIR settings (identity = their own creds), replies/history land in their
    own data dir, and a mailbox configured by two users is polled only once."""
    from datetime import datetime

    from assistant.serve import _tick_tenants

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("DEPLOYMENT_MODE", "multi_tenant")
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    reg = UserRegistry(data_dir)
    boxes = {"alice1": "alice@example.com", "bob123": "bob@example.com",
             "carol1": "alice@example.com"}          # carol shares alice's inbox
    for uid, box in boxes.items():
        reg.add_user(uid)
        cfg = data_dir / "users" / uid / "config.env"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(f"SMTP_USER={box}\nSMTP_PASSWORD=pw\n")

    polls, sent = [], []

    class FakeEmail:
        def __init__(self, settings, owners):
            self.settings = settings
            self.enabled = bool(settings.smtp_user)

        def poll(self):
            polls.append(self.settings.uid)
            if self.settings.uid == "alice1":
                return [{"sender": "alice@example.com", "text": "我的待办?"}]
            return []

        def send(self, reply, in_reply_to=None):
            sent.append((self.settings.uid, reply))

    monkeypatch.setattr("assistant.chat.email_channel.EmailChannel", FakeEmail)

    class NoStore:                                    # keep the tick email-only
        def __init__(self, d):
            pass

        def deliver_due(self, s):
            return []

    monkeypatch.setattr("assistant.notify.ReminderStore", NoStore)
    monkeypatch.setattr("assistant.routines.fire_due", lambda s: None)

    llm = FakeLLM()
    _tick_tenants(Settings(_env_file=None), now=datetime(2026, 7, 16, 5, 0),
                  llm_factory=lambda s: llm)

    assert polls == ["alice1", "bob123"]              # carol's duplicate skipped
    assert sent == [("alice1", "ok")]                 # alice answered via HER channel
    # history spilled under ALICE's own data dir with the uid-scoped key
    store = SessionStore(data_dir / "users" / "alice1")
    assert [t["owner"] for t in store.history("alice1:email:alice@example.com")] \
        == ["我的待办?"]
    # bob's dir has no email history
    assert SessionStore(data_dir / "users" / "bob123").history(
        "bob123:email:alice@example.com") == []


def test_tick_tenants_email_failure_isolated(tmp_path, monkeypatch):
    """One user's broken mailbox (poll raising) must not stop the next user's
    poller — failure isolation per user."""
    from datetime import datetime

    from assistant.serve import _tick_tenants

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("DEPLOYMENT_MODE", "multi_tenant")
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    reg = UserRegistry(data_dir)
    for uid, box in (("alice1", "a@x.com"), ("bob123", "b@x.com")):
        reg.add_user(uid)
        cfg = data_dir / "users" / uid / "config.env"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(f"SMTP_USER={box}\nSMTP_PASSWORD=pw\n")

    polls = []

    class FlakyEmail:
        def __init__(self, settings, owners):
            self.settings = settings
            self.enabled = True

        def poll(self):
            polls.append(self.settings.uid)
            if self.settings.uid == "alice1":
                raise OSError("imap down")
            return []

        def send(self, reply, in_reply_to=None):
            pass

    monkeypatch.setattr("assistant.chat.email_channel.EmailChannel", FlakyEmail)
    monkeypatch.setattr("assistant.routines.fire_due", lambda s: None)

    class NoStore:
        def __init__(self, d):
            pass

        def deliver_due(self, s):
            return []

    monkeypatch.setattr("assistant.notify.ReminderStore", NoStore)
    _tick_tenants(Settings(_env_file=None), now=datetime(2026, 7, 16, 5, 0),
                  llm_factory=lambda s: FakeLLM())
    assert polls == ["alice1", "bob123"]              # bob polled despite alice's error


def test_tick_tenants_weekly_gate(tmp_path, monkeypatch):
    """Sunday >= weekly_hour fires the weekly self-evolution set (idempotent);
    Saturday or too-early Sunday doesn't."""
    from datetime import datetime

    from assistant.jobs import GLOBAL_UID, JobQueue
    from assistant.serve import _tick_tenants

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("DEPLOYMENT_MODE", "multi_tenant")
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("SMTP_USER", "")
    monkeypatch.setenv("SMTP_PASSWORD", "")
    reg = UserRegistry(data_dir)
    reg.add_user("alice1")

    class NoStore:
        def __init__(self, d):
            pass

        def deliver_due(self, s):
            return []

    monkeypatch.setattr("assistant.notify.ReminderStore", NoStore)
    monkeypatch.setattr("assistant.routines.fire_due", lambda s: None)
    root = Settings(_env_file=None)
    q = JobQueue(root.shared_dir)

    # Saturday, and Sunday before weekly_hour: only daily runs fire (one per day)
    _tick_tenants(root, now=datetime(2026, 7, 18, 9, 0))    # Saturday
    _tick_tenants(root, now=datetime(2026, 7, 19, 7, 30))   # Sunday 07:30 < 8
    assert q.counts() == {"queued": 2}                      # Sat + Sun daily runs
    # Sunday 09:00: weekly set fires — consolidate+evolve for alice, 2 globals
    _tick_tenants(root, now=datetime(2026, 7, 19, 9, 0))
    _tick_tenants(root, now=datetime(2026, 7, 19, 10, 0))   # repeat tick dedupes
    counts = q.counts()
    assert counts == {"queued": 6}                          # 2 daily + 4 weekly
    kinds = []
    while (job := q.claim()) is not None:
        kinds.append((job["kind"], job["uid"]))
        q.mark(job["id"], "done")
    assert ("global_evolve", GLOBAL_UID) in kinds
    assert ("self_improve", GLOBAL_UID) in kinds
    assert ("evolve", "alice1") in kinds
    assert ("run_phase", "alice1") in kinds
