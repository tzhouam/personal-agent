"""The rewritten kernel reuses authenticated HTTP/email paths without API setup."""

from contextlib import contextmanager
from datetime import datetime
import threading
from types import SimpleNamespace

import httpx

from assistant.platform.config import Settings
from assistant.platform.jobs import GLOBAL_UID
from assistant.platform.registry import UserRegistry
from assistant.platform import serve
from assistant.platform.serve import ServeServices, SessionStore


def turn(reply="runtime reply"):
    """Return the persistence contract shared with the retained transport code."""
    return SimpleNamespace(reply=reply, outcome="neutral", repaired=False,
                           self_reported=False, prev_verdict=None,
                           surfaced_failure_ids=[])


def no_llm(_settings):
    """Fail if a native backend accidentally constructs an API-specific client."""
    raise AssertionError("a native runtime must not construct LLM")


def services(**updates):
    """Build inert transport services with optional runtime behavior."""
    fields = dict(run_action=lambda name, params, settings: name,
                  handle_turn=lambda *args, **kwargs: turn("legacy reply"),
                  build_channels=lambda *args, **kwargs: [],
                  email_channel=lambda settings: SimpleNamespace(enabled=False),
                  fire_due=lambda settings: None, acquire_pid_lock=lambda settings: True,
                  worker_dispatch={})
    fields.update(updates)
    return ServeServices(**fields)


@contextmanager
def serving(settings, active_services):
    """Serve only a loopback fixture and always close the test listener."""
    server = serve.make_server(settings_factory=lambda: settings, llm_factory=no_llm,
                               port=0, services=active_services)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_runtime_callback_receives_session_history_and_images_without_llm(settings):
    """The authenticated HTTP route invokes the new kernel and retains old history."""
    captured = []

    def handle(text, scoped, **kwargs):
        captured.append((text, scoped, kwargs))
        return turn()

    active = services(handle_runtime_turn=handle)
    with serving(settings, active) as (url, _server):
        response = httpx.post(url + "/chat", json={"session": "weixin:owner", "text": "first"})
        assert response.status_code == 200 and response.json()["reply"] == "runtime reply"
        response = httpx.post(url + "/chat", json={
            "session": "weixin:owner", "text": "image", "image_paths": ["/scratch/image.png"]})
        assert response.status_code == 200
    assert captured[0][2]["session"] == "weixin:owner"
    assert captured[1][2]["history"][0]["owner"] == "first"
    assert captured[1][2]["image_paths"] == ["/scratch/image.png"]
    assert SessionStore(settings.data_dir).history("weixin:owner")[-1]["owner"].startswith("image")


def test_runtime_resolver_runs_only_after_auth_and_preserves_configured_root(tmp_path, monkeypatch):
    """A scoped runtime can use a root differing from the process environment."""
    root = Settings(_env_file=None, data_dir=tmp_path / "configured", deployment_mode="multi_tenant")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "wrong-environment-root"))
    registry = UserRegistry(root.data_dir)
    registry.add_user("alice1")
    registry.bind_channel("alice1", "weixin", "account-A")
    registry.set_bridge_token("bridge-test-token")
    calls, turns = [], []
    scoped = root.model_copy(update={"uid": "alice1", "data_dir": root.data_dir / "users" / "alice1"})

    def resolve(uid):
        calls.append(uid)
        return scoped

    def handle(text, settings, **kwargs):
        turns.append((settings, kwargs))
        return turn()

    active = services(handle_runtime_turn=handle, runtime_settings_factory=lambda: root,
                      resolve_runtime_settings=resolve)
    with serving(root, active) as (url, _server):
        body = {"channel": "weixin", "account_id": "account-A", "uid": "forged", "text": "hello"}
        assert httpx.post(url + "/chat", json=body).status_code == 401
        assert not calls
        response = httpx.post(url + "/chat", json=body,
                              headers={"Authorization": "Bearer bridge-test-token"})
        assert response.status_code == 200
    assert calls == ["alice1"]
    assert turns[0][0].data_dir == scoped.data_dir
    assert turns[0][1]["session"] == "alice1:default"
    assert not (tmp_path / "wrong-environment-root").exists()


def test_without_runtime_callback_the_legacy_turn_contract_is_unchanged(settings):
    """Existing clients still receive one fresh LLM and the same keyword arguments."""
    llm, captured = object(), []

    def legacy(text, scoped, actual_llm, **kwargs):
        captured.append((text, scoped, actual_llm, kwargs))
        return turn("legacy")

    result = serve._invoke_turn(services(handle_turn=legacy), "hello", settings,
                                lambda scoped: llm, session="unused-by-legacy",
                                history=[{"owner": "earlier"}])
    assert result.reply == "legacy" and captured[0][2] is llm
    assert "session" not in captured[0][3]
    assert captured[0][3]["history"] == [{"owner": "earlier"}]


class EmailFixture:
    """A mailbox ledger that records execution/send/ack sequencing."""

    enabled = True
    name = "email"

    def __init__(self, messages):
        """Hold only scratch messages and receipts; no actual mail transport."""
        self.messages = messages
        self.events = []

    def poll(self):
        """Yield the fixture's current batch."""
        return self.messages

    def begin_turn(self, message):
        """Claim a new mail turn."""
        self.events.append("begin")
        return "claim"

    def finish_turn(self, message, token, reply, ids):
        """Persist the reply before the transport send."""
        self.events.append("finish")
        return True

    def send(self, reply, *, in_reply_to):
        """Record a successful fixture delivery."""
        self.events.append(("send", reply))

    def ack(self, message):
        """Settle only after the reply was accepted."""
        self.events.append("ack")

    def send_failed(self, message, error):
        """Retain a failed delivery for a subsequent poll."""
        self.events.append("send_failed")

    def turn_failed(self, message, token, error):
        """Record execution failures independently of transport failures."""
        self.events.append("turn_failed")


def test_tenant_email_callback_uses_scoped_session_without_constructing_llm(settings):
    """Plain and ledger email both enter the new runtime using the same owner key."""
    calls = []
    email = EmailFixture([{"sender": "owner@example.com", "text": "ordinary"},
                          {"sender": "owner@example.com", "text": "ledger", "uid": "mail-id"}])

    def runtime(text, scoped, **kwargs):
        calls.append((text, scoped.uid, kwargs["session"]))
        return turn(text)

    active = services(handle_runtime_turn=runtime, email_channel=lambda scoped: email)
    serve._tick_user_email(active, settings, no_llm, set())
    key = f"{settings.uid}:email:owner@example.com"
    assert calls == [("ordinary", settings.uid, key), ("ledger", settings.uid, key)]
    assert email.events == [("send", "ordinary"), "begin", "finish", ("send", "ledger"), "ack"]


def test_email_retry_only_sends_the_persisted_reply(settings):
    """Delivery retries never execute either runtime or legacy inference again."""
    email = EmailFixture([])
    active = services(handle_runtime_turn=lambda *args, **kwargs: no_llm(settings))
    message = {"kind": "email_outbox_retry", "reply": "already computed", "surfaced_ids": []}
    assert serve._process_email_ledger(email, message, settings, None, active,
                                       lambda *args: None, session="owner:email")
    assert email.events == [("send", "already computed"), "ack"]


def test_single_user_poller_uses_runtime_and_keeps_image_arguments(settings):
    """The shared email/WeCom polling path does not fall back to an API model."""
    stop, calls = threading.Event(), []
    email = EmailFixture([])

    def poll():
        stop.set()
        return [{"sender": "owner@example.com", "text": "plain", "images": ["photo.png"]}]

    email.poll = poll

    def handle(text, scoped, **kwargs):
        calls.append(kwargs)
        return turn()

    active = services(handle_runtime_turn=handle, build_channels=lambda *args, **kwargs: [email])
    serve._chat_poll_loop(lambda: settings, SessionStore(settings.data_dir), stop,
                         llm_factory=no_llm, services=active)
    assert calls[0]["session"] == f"{settings.uid}:email:owner@example.com"
    assert calls[0]["image_paths"] == ["photo.png"]
    assert email.events == [("send", "runtime reply")]


def test_tenant_ticks_use_runtime_resolver_at_the_configured_root(settings):
    """Scheduled mailbox ticks use the same tenant settings seam as HTTP."""
    settings.deployment_mode = "multi_tenant"
    registry = UserRegistry(settings.data_dir)
    registry.add_user("alice1")
    calls = []
    user = settings.model_copy(update={"uid": "alice1", "data_dir": settings.data_dir / "users" / "alice1"})

    def resolve(uid):
        calls.append(uid)
        return user

    active = services(resolve_runtime_settings=resolve)
    serve._tick_tenants(settings, now=datetime(2026, 7, 15, 1), services=active)
    assert calls == ["alice1"]


def test_daemon_uses_one_runtime_factory_for_server_poller_and_worker_settings(settings, monkeypatch):
    """Configured runtime roots reach every daemon path, including global jobs."""
    settings.deployment_mode = "multi_tenant"
    settings.login_qr_refresh = False
    calls = {}
    scoped = settings.model_copy(update={"uid": "alice1", "data_dir": settings.data_dir / "users" / "alice1"})
    factory = lambda: settings

    class Server:
        """An inert listener lets the real daemon lifecycle run without sockets."""

        sessions = SessionStore(settings.data_dir)
        server_address = ("127.0.0.1", 0)

        def serve_forever(self):
            """End immediately so cleanup can be asserted."""

        def server_close(self):
            """Record that the listener cleanup still runs."""
            calls["closed"] = True

    def make(**kwargs):
        calls["server_factory"] = kwargs["settings_factory"]
        return Server()

    def poll(actual_factory, sessions, stop, **kwargs):
        calls["poller_factory"] = actual_factory

    class Pool:
        """Capture the runtime's injected user/global settings resolver."""

        def __init__(self, queue, **kwargs):
            calls["worker_global"] = kwargs["settings_for"](GLOBAL_UID)
            calls["worker_user"] = kwargs["settings_for"]("alice1")

        def start(self):
            """Avoid creating background work in this lifecycle fixture."""

        def stop(self):
            """Record orderly worker cleanup."""
            calls["pool_stopped"] = True

    from assistant.platform import worker

    monkeypatch.setattr(serve, "make_server", make)
    monkeypatch.setattr(serve, "_chat_poll_loop", poll)
    monkeypatch.setattr(serve.signal, "signal", lambda *args: None)
    monkeypatch.setattr(worker, "WorkerPool", Pool)
    active = services(runtime_settings_factory=factory,
                      resolve_runtime_settings=lambda uid: scoped)
    assert serve.run_serve(settings, active) == 0
    assert calls["server_factory"] is calls["poller_factory"] is factory
    assert calls["worker_global"] is settings and calls["worker_user"] is scoped
    assert calls["closed"] and calls["pool_stopped"]
