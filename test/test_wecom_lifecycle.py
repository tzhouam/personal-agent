"""F1 regression: the WeCom callback server must survive the serve poll loop's
per-cycle channel rebuild instead of raising EADDRINUSE into it — which
silently disabled email polling, reminders, and routines on every cycle after
the first while `/healthz` stayed green — and its inbox must outlive the
per-cycle `WeComChannel` instances so messages received between polls aren't
lost with a discarded queue."""

import base64
import os
import queue
import socket

import pytest

from assistant.agent.chat import wecom
from assistant.agent.chat.wecom import _CallbackHolder, _holder


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wecom_settings(settings, monkeypatch, port, owner="boss"):
    aes = base64.b64encode(os.urandom(32)).decode().rstrip("=")  # 43 chars
    for k, v in [("wecom_corp_id", "corp1"), ("wecom_secret", "sec"),
                 ("wecom_agent_id", "1"), ("wecom_token", "tok"),
                 ("wecom_aes_key", aes), ("wecom_owner_userid", owner),
                 ("wecom_callback_port", port)]:
        monkeypatch.setattr(settings, k, v)
    return settings


@pytest.fixture(autouse=True)
def _clean_holder():
    _holder.reset_for_tests()
    yield
    _holder.reset_for_tests()


def test_consecutive_cycles_share_one_server_and_inbox(settings, monkeypatch):
    """Cycle 2's build must reuse cycle 1's server — the EADDRINUSE bug —
    and a message queued between cycles must reach the next cycle's channel."""
    _wecom_settings(settings, monkeypatch, _free_port())
    ch1 = wecom.WeComChannel(settings)
    assert ch1.start_callback_server() is True
    ch1._inbox.put({"channel": "wecom", "text": "hi", "subject": "", "sender": "boss"})

    ch2 = wecom.WeComChannel(settings)  # the poll loop's next-cycle rebuild
    assert ch2.start_callback_server() is True
    assert ch2._inbox is ch1._inbox
    assert [m["text"] for m in ch2.poll()] == ["hi"]


def test_identity_change_rebinds_and_drops_old_queue(settings, monkeypatch):
    """A hot-reloaded credential tears down and rebinds; messages accepted
    under the old identity never cross into the new identity's queue."""
    port = _free_port()
    _wecom_settings(settings, monkeypatch, port, owner="old-owner")
    ch1 = wecom.WeComChannel(settings)
    assert ch1.start_callback_server() is True
    old_inbox = ch1._inbox
    old_inbox.put({"channel": "wecom", "text": "stale", "subject": "", "sender": "x"})

    monkeypatch.setattr(settings, "wecom_owner_userid", "new-owner")
    ch2 = wecom.WeComChannel(settings)
    assert ch2.start_callback_server() is True
    assert ch2._inbox is not old_inbox
    assert ch2.poll() == []


def test_bind_failure_degrades_and_recovers(settings, monkeypatch):
    """A port held by another process degrades to send-only (False) without
    raising — the poll cycle must survive — and recovers once the port frees."""
    blocker = socket.socket()
    blocker.bind(("0.0.0.0", 0))
    port = blocker.getsockname()[1]
    _wecom_settings(settings, monkeypatch, port)
    ch = wecom.WeComChannel(settings)
    assert ch.start_callback_server() is False
    blocker.close()
    assert ch.start_callback_server() is True


def test_dead_server_thread_is_rebound(settings, monkeypatch):
    """A crashed serve_forever thread must not be reused forever."""
    _wecom_settings(settings, monkeypatch, _free_port())
    ch = wecom.WeComChannel(settings)
    assert ch.start_callback_server() is True
    _holder._server.shutdown()          # simulate the server thread dying
    _holder._thread.join(timeout=5)
    assert _holder.status()["wecom_callback_ok"] is False
    assert _holder.status()["wecom_callback_state"] == "dead"
    assert ch.start_callback_server() is True
    assert _holder.status()["wecom_callback_ok"] is True


def test_stale_generation_enqueue_is_dropped(settings, monkeypatch):
    """A lingering request thread from a torn-down server can't deliver into
    the replacement identity's queue."""
    _wecom_settings(settings, monkeypatch, _free_port())
    ch = wecom.WeComChannel(settings)
    assert ch.start_callback_server() is True
    stale_gen = _holder._generation
    monkeypatch.setattr(settings, "wecom_owner_userid", "other")
    ch2 = wecom.WeComChannel(settings)
    assert ch2.start_callback_server() is True
    _holder.enqueue(stale_gen, ("corp1", "old-owner-principal"),
                    {"channel": "wecom", "text": "leak",
                     "subject": "", "sender": "x"})
    assert ch2.poll() == []


def test_inbox_overflow_drops_oldest():
    holder = _CallbackHolder()
    holder._inbox = queue.Queue(maxsize=2)
    holder._generation = 1
    holder.enqueue(1, None, {"text": "a"})
    holder.enqueue(1, None, {"text": "b"})
    holder.enqueue(1, None, {"text": "c"})   # full → oldest (a) dropped
    got = []
    while True:
        try:
            got.append(holder._inbox.get_nowait()["text"])
        except queue.Empty:
            break
    assert got == ["b", "c"]


def test_teardown_latches_against_rebind(settings, monkeypatch):
    """After daemon shutdown a late poll cycle must not rebind."""
    _wecom_settings(settings, monkeypatch, _free_port())
    ch = wecom.WeComChannel(settings)
    assert ch.start_callback_server() is True
    wecom.teardown_callback_server()
    assert wecom.callback_health() == {"wecom_callback_ok": None,
                                       "wecom_callback_state": "shutdown"}
    ch2 = wecom.WeComChannel(settings)
    assert ch2.start_callback_server() is False   # latched, no rebind


def test_send_only_without_token(settings, monkeypatch):
    """No Token/AESKey → send-only, holder untouched (existing behavior)."""
    monkeypatch.setattr(settings, "wecom_corp_id", "corp1")
    monkeypatch.setattr(settings, "wecom_secret", "sec")
    monkeypatch.setattr(settings, "wecom_agent_id", "1")
    ch = wecom.WeComChannel(settings)
    assert ch.enabled and ch.start_callback_server() is False
    assert wecom.callback_health() == {"wecom_callback_ok": None,
                                       "wecom_callback_state": "off"}


def test_same_principal_rotation_keeps_queue(settings, monkeypatch):
    """A token/AES/port rotation for the same corp+owner must not lose queued
    owner messages — only a principal change drops them."""
    _wecom_settings(settings, monkeypatch, _free_port())
    ch1 = wecom.WeComChannel(settings)
    assert ch1.start_callback_server() is True
    ch1._inbox.put({"channel": "wecom", "text": "keep me", "subject": "",
                    "sender": "boss"})
    monkeypatch.setattr(settings, "wecom_token", "rotated")   # same corp+owner
    monkeypatch.setattr(settings, "wecom_callback_port", _free_port())
    ch2 = wecom.WeComChannel(settings)
    assert ch2.start_callback_server() is True
    assert [m["text"] for m in ch2.poll()] == ["keep me"]


def test_disabled_config_releases_server(settings, monkeypatch):
    """Deleting WECOM_TOKEN from .env must close the old listener on the next
    cycle (desired-state sync), not leave it serving stale credentials —
    and re-adding the config must rebind."""
    from assistant.agent.chat.service import build_channels

    port = _free_port()
    _wecom_settings(settings, monkeypatch, port)
    monkeypatch.setattr(settings, "smtp_user", "")   # keep email channel off
    build_channels(settings, log_wecom=False)
    assert wecom.callback_health()["wecom_callback_state"] == "ok"

    token = settings.wecom_token
    monkeypatch.setattr(settings, "wecom_token", "")          # config removed
    build_channels(settings, log_wecom=False)
    assert wecom.callback_health() == {"wecom_callback_ok": None,
                                       "wecom_callback_state": "off"}
    with socket.socket() as probe:                            # port really freed
        probe.bind(("0.0.0.0", port))

    monkeypatch.setattr(settings, "wecom_token", token)       # config restored
    build_channels(settings, log_wecom=False)
    assert wecom.callback_health()["wecom_callback_state"] == "ok"


def test_wecom_disabled_entirely_releases_server(settings, monkeypatch):
    from assistant.agent.chat.service import build_channels

    _wecom_settings(settings, monkeypatch, _free_port())
    build_channels(settings, log_wecom=False)
    assert wecom.callback_health()["wecom_callback_state"] == "ok"
    monkeypatch.setattr(settings, "wecom_corp_id", "")        # channel disabled
    build_channels(settings, log_wecom=False)
    assert wecom.callback_health()["wecom_callback_state"] == "off"


def test_bind_failure_state_not_clobbered_by_sync(settings, monkeypatch):
    """A bind failure with receive still configured is a retry case, not a
    release — health must keep saying bind_failed."""
    from assistant.agent.chat.service import build_channels

    blocker = socket.socket()
    blocker.bind(("0.0.0.0", 0))
    _wecom_settings(settings, monkeypatch, blocker.getsockname()[1])
    build_channels(settings, log_wecom=False)
    assert wecom.callback_health()["wecom_callback_state"] == "bind_failed"
    blocker.close()


def test_thread_start_failure_degrades(settings, monkeypatch):
    """A failed thread spawn must release the socket and degrade to send-only,
    never propagate into the poll cycle."""
    port = _free_port()
    _wecom_settings(settings, monkeypatch, port)

    seen = {}

    class BadThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            # holder state must be PUBLISHED before the listener starts — a
            # callback in the start window must find generation/inbox set
            seen["published"] = (wecom._holder._inbox is not None
                                 and wecom._holder._identity is not None)
            raise RuntimeError("can't spawn")

    monkeypatch.setattr(wecom.threading, "Thread", BadThread)
    ch = wecom.WeComChannel(settings)
    assert ch.start_callback_server() is False
    assert seen["published"] is True
    assert wecom.callback_health()["wecom_callback_state"] == "bind_failed"
    assert wecom._holder._server is None      # rolled back, socket released
    monkeypatch.undo()
    with socket.socket() as probe:   # socket was released despite the failure
        probe.bind(("0.0.0.0", port))


def _encrypt_callback(token, aes_key_b43, corp_id, msg_xml, ts="1", nonce="n"):
    """Test-side WXBizMsgCrypt encryptor mirroring _MsgCrypto.decrypt."""
    import hashlib
    import struct as _struct

    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = base64.b64decode(aes_key_b43 + "=")
    plain = (os.urandom(16) + _struct.pack(">I", len(msg_xml.encode()))
             + msg_xml.encode() + corp_id.encode())
    pad = 32 - len(plain) % 32
    plain += bytes([pad]) * pad
    enc = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    blob = base64.b64encode(enc.update(plain) + enc.finalize()).decode()
    sig = hashlib.sha1("".join(sorted([token, ts, nonce, blob])).encode()).hexdigest()
    return blob, sig, ts, nonce


def test_real_callback_roundtrip_across_cycles(settings, monkeypatch):
    """Integration: two real poll cycles with an HTTP callback landing between
    them — the message must survive the cycle-2 channel rebuild."""
    import httpx

    port = _free_port()
    _wecom_settings(settings, monkeypatch, port, owner="boss")
    ch1 = wecom.WeComChannel(settings)
    assert ch1.start_callback_server() is True
    assert ch1.poll() == []                       # cycle 1: nothing yet

    msg = ("<xml><FromUserName>boss</FromUserName>"
           "<Content>回个话</Content></xml>")
    blob, sig, ts, nonce = _encrypt_callback(
        settings.wecom_token, settings.wecom_aes_key, settings.wecom_corp_id, msg)
    r = httpx.post(
        f"http://127.0.0.1:{port}/?msg_signature={sig}&timestamp={ts}&nonce={nonce}",
        content=f"<xml><Encrypt>{blob}</Encrypt></xml>", timeout=5)
    assert r.status_code == 200

    ch2 = wecom.WeComChannel(settings)            # cycle 2 rebuild
    assert ch2.start_callback_server() is True
    polled = ch2.poll()
    assert [m["text"] for m in polled] == ["回个话"]
    assert polled[0]["sender"] == "boss"


def test_serve_services_wire_teardown_and_health(settings, monkeypatch):
    """The composition root must wire the holder's teardown/health into the
    ServeServices contract the daemon shutdown path calls."""
    from assistant.agent.app import build_services

    _wecom_settings(settings, monkeypatch, _free_port())
    ch = wecom.WeComChannel(settings)
    assert ch.start_callback_server() is True
    services = build_services()
    assert services.channel_health()["wecom_callback_ok"] is True
    services.teardown_channels()
    assert services.channel_health() == {"wecom_callback_ok": None,
                                         "wecom_callback_state": "shutdown"}
    assert wecom.WeComChannel(settings).start_callback_server() is False  # latched


def test_failed_rotation_then_recovery_keeps_queue(settings, monkeypatch):
    """A same-principal rotation whose replacement bind FAILS must not lose
    the queued messages — the next successful bind adopts them."""
    _wecom_settings(settings, monkeypatch, _free_port())
    ch1 = wecom.WeComChannel(settings)
    assert ch1.start_callback_server() is True
    ch1._inbox.put({"channel": "wecom", "text": "survive", "subject": "",
                    "sender": "boss"})

    blocker = socket.socket()                       # replacement port is taken
    blocker.bind(("0.0.0.0", 0))
    monkeypatch.setattr(settings, "wecom_callback_port", blocker.getsockname()[1])
    monkeypatch.setattr(settings, "wecom_token", "rotated")
    ch2 = wecom.WeComChannel(settings)
    assert ch2.start_callback_server() is False     # rotation failed

    blocker.close()                                  # next cycle: port free
    ch3 = wecom.WeComChannel(settings)
    assert ch3.start_callback_server() is True
    assert [m["text"] for m in ch3.poll()] == ["survive"]


def test_inflight_same_principal_enqueue_survives_rotation(settings, monkeypatch):
    """An owner message decrypted by the OLD server's request thread mid-
    rotation is accepted (same principal) — only a principal change drops."""
    _wecom_settings(settings, monkeypatch, _free_port())
    ch1 = wecom.WeComChannel(settings)
    assert ch1.start_callback_server() is True
    old_gen = _holder._generation
    principal = ("corp1", "boss")
    monkeypatch.setattr(settings, "wecom_token", "rotated")   # same principal
    ch2 = wecom.WeComChannel(settings)
    assert ch2.start_callback_server() is True
    _holder.enqueue(old_gen, principal, {"channel": "wecom", "text": "inflight",
                                         "subject": "", "sender": "boss"})
    assert [m["text"] for m in ch2.poll()] == ["inflight"]


def test_shutdown_raise_still_releases_socket(settings, monkeypatch):
    """server_close runs in a finally — a raising shutdown() must not leak
    the listening socket."""
    port = _free_port()
    _wecom_settings(settings, monkeypatch, port)
    ch = wecom.WeComChannel(settings)
    assert ch.start_callback_server() is True
    monkeypatch.setattr(_holder._server, "request_shutdown",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    wecom.teardown_callback_server()
    with socket.socket() as probe:                  # socket freed regardless
        probe.bind(("0.0.0.0", port))


def test_bind_failed_reports_unhealthy_not_none(settings, monkeypatch):
    """bind_failed is should-be-running-but-isn't → explicitly False, never
    conflated with intentionally-unconfigured None."""
    blocker = socket.socket()
    blocker.bind(("0.0.0.0", 0))
    _wecom_settings(settings, monkeypatch, blocker.getsockname()[1])
    assert wecom.WeComChannel(settings).start_callback_server() is False
    assert wecom.callback_health() == {"wecom_callback_ok": False,
                                       "wecom_callback_state": "bind_failed"}
    blocker.close()




def _attach_live_poller(server):
    """Fake a functioning daemon on a bare make_server instance: /readyz fails
    closed without a live poll thread and a completed first cycle."""
    import threading
    import time as _t

    server.chat_poller = threading.current_thread()      # alive by definition
    server.poll_heartbeat = {"ts": _t.monotonic()}       # first cycle "done"

def test_readyz_reports_channel_states_and_fails_closed(settings, monkeypatch):
    """/readyz carries the channel detail; /healthz keeps its exact legacy
    contract; a RAISING health probe makes readiness fail closed (503)."""
    import json
    import threading

    import httpx

    from assistant.agent.app import build_services
    from assistant.platform import serve as serve_mod

    _wecom_settings(settings, monkeypatch, _free_port())
    assert wecom.WeComChannel(settings).start_callback_server() is True
    server = serve_mod.make_server(settings_factory=lambda: settings, port=0,
                                   services=build_services())
    _attach_live_poller(server)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        assert httpx.get(f"{base}/healthz", timeout=5).json() == {"ok": True}
        r = httpx.get(f"{base}/readyz", timeout=5)
        assert r.status_code == 200
        body = r.json()
        assert body["ready"] is True
        assert body["wecom_callback_ok"] is True
        assert body["wecom_callback_state"] == "ok"
        assert body["health_probe_ok"] is True

        services2 = build_services()
        services2.channel_health = lambda: (_ for _ in ()).throw(RuntimeError("probe"))
        server2 = serve_mod.make_server(settings_factory=lambda: settings,
                                        port=0, services=services2)
        _attach_live_poller(server2)
        t2 = threading.Thread(target=server2.serve_forever, daemon=True)
        t2.start()
        try:
            base2 = f"http://127.0.0.1:{server2.server_address[1]}"
            r = httpx.get(f"{base2}/readyz", timeout=5)
            assert r.status_code == 503                 # fail closed
            assert r.json()["health_probe_ok"] is False
            assert httpx.get(f"{base2}/healthz", timeout=5).json() == {"ok": True}
        finally:
            server2.shutdown()
            server2.server_close()
    finally:
        server.shutdown()
        server.server_close()


def test_run_serve_lifecycle_poller_and_teardown(settings, monkeypatch):
    """End-to-end daemon lifecycle: run_serve attaches a live poller to
    /healthz, and shutdown joins it then calls the teardown hook."""
    import threading

    import httpx

    from assistant.platform import serve as serve_mod
    from assistant.platform.serve import ServeServices

    events = []
    captured = {}
    real_make = serve_mod.make_server

    def capture(*a, **k):
        captured["server"] = real_make(settings_factory=lambda: settings,
                                       port=0, services=k.get("services"))
        return captured["server"]

    monkeypatch.setattr(serve_mod, "make_server", capture)
    monkeypatch.setattr(serve_mod.signal, "signal", lambda *a: None)
    monkeypatch.setattr(serve_mod, "Settings", lambda: settings)
    monkeypatch.setattr(settings, "login_qr_refresh", False)
    monkeypatch.setattr(settings, "chat_poll_seconds", 1)

    def record_teardown():
        poll_alive = any(th.name == "chat-poll" and th.is_alive()
                         for th in threading.enumerate())
        events.append(("teardown", poll_alive))

    services = ServeServices(
        run_action=lambda *a: "", handle_turn=lambda *a, **k: None,
        build_channels=lambda s, log_wecom=False: [],
        email_channel=lambda s: None, fire_due=lambda s: None,
        acquire_pid_lock=lambda s: True, worker_dispatch={},
        teardown_channels=record_teardown,
        channel_health=lambda: {})
    t = threading.Thread(target=serve_mod.run_serve, args=(settings, services))
    t.start()
    try:
        for _ in range(100):
            if "server" in captured and captured["server"].server_address[1]:
                break
            import time as _t

            _t.sleep(0.05)
        base = f"http://127.0.0.1:{captured['server'].server_address[1]}"
        assert httpx.get(f"{base}/healthz", timeout=5).json() == {"ok": True}
        for _ in range(100):                    # first cycle stamps heartbeat
            body = httpx.get(f"{base}/readyz", timeout=5).json()
            if body.get("poll_cycle_stale") is False:
                break
            import time as _t

            _t.sleep(0.05)
        assert body["ready"] is True and body["poller_alive"] is True
        assert body["poll_cycle_stale"] is False
    finally:
        captured["server"].shutdown()
        t.join(timeout=15)
    assert not t.is_alive()
    # teardown fired exactly once, AFTER the poller was joined (dead)
    assert events == [("teardown", False)]


def test_slow_partial_body_client_does_not_block_release(settings, monkeypatch):
    """Adversarial: a client that sends headers + a partial body and stalls
    must not hang release/rotation/shutdown — server_close must not join
    request threads (that join deadlocks with enqueue waiting on the holder
    lock), and the handler socket timeout bounds the thread's life."""
    import threading

    port = _free_port()
    _wecom_settings(settings, monkeypatch, port)
    assert wecom.WeComChannel(settings).start_callback_server() is True
    assert _holder._server.block_on_close is False   # the deadlock guard

    stall = socket.create_connection(("127.0.0.1", port), timeout=5)
    stall.sendall(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 500\r\n\r\nonly-a-bit")
    try:
        done = threading.Event()
        t = threading.Thread(target=lambda: (wecom.release_callback_server(),
                                             done.set()))
        t.start()
        assert done.wait(timeout=8), "release() hung behind a stalled client"
        assert wecom.callback_health()["wecom_callback_state"] == "off"
    finally:
        stall.close()


def test_enqueue_during_rotation_no_deadlock(settings, monkeypatch):
    """Adversarial: enqueue() contending for the holder lock while a rotation
    closes the server must complete (the message lands via generation or
    principal routing), never deadlock."""
    import threading

    _wecom_settings(settings, monkeypatch, _free_port())
    ch = wecom.WeComChannel(settings)
    assert ch.start_callback_server() is True
    gen, principal = _holder._generation, ("corp1", "boss")

    results = []

    def hammer():
        for i in range(200):
            _holder.enqueue(gen, principal, {"channel": "wecom",
                                             "text": f"m{i}", "subject": "",
                                             "sender": "boss"})
        results.append("done")

    t = threading.Thread(target=hammer)
    t.start()
    monkeypatch.setattr(settings, "wecom_token", "rotated")     # same principal
    monkeypatch.setattr(settings, "wecom_callback_port", _free_port())
    ch2 = wecom.WeComChannel(settings)
    assert ch2.start_callback_server() is True
    t.join(timeout=10)
    assert results == ["done"], "enqueue hammer deadlocked against rotation"
    # every message routed via generation-match (pre-rotation) or principal-
    # match (post-rotation) into the carried-over queue: none lost
    assert len(ch2.poll()) == 200


def test_startup_hook_unlatches_for_next_lifecycle(settings, monkeypatch):
    """Sequential run_serve lifecycles in one process: shutdown latches, the
    next startup's hook un-latches, and binding works again."""
    from assistant.agent.app import build_services

    _wecom_settings(settings, monkeypatch, _free_port())
    services = build_services()
    assert wecom.WeComChannel(settings).start_callback_server() is True
    services.teardown_channels()                       # lifecycle 1 ends
    assert wecom.WeComChannel(settings).start_callback_server() is False
    services.startup_channels()                        # lifecycle 2 begins
    monkeypatch.setattr(settings, "wecom_callback_port", _free_port())
    assert wecom.WeComChannel(settings).start_callback_server() is True


def test_first_cycle_never_completing_degrades_readiness(settings, monkeypatch):
    """Standard readiness semantics: NOT ready until the first poll cycle
    completes — a loop that errors every cycle is 503 from the FIRST probe
    (first_cycle_done false), not after some grace window."""
    import threading

    import httpx

    from assistant.platform import serve as serve_mod
    from assistant.platform.serve import ServeServices

    captured = {}
    real_make = serve_mod.make_server

    def capture(*a, **k):
        captured["server"] = real_make(settings_factory=lambda: settings,
                                       port=0, services=k.get("services"))
        return captured["server"]

    monkeypatch.setattr(serve_mod, "make_server", capture)
    monkeypatch.setattr(serve_mod.signal, "signal", lambda *a: None)
    monkeypatch.setattr(serve_mod, "Settings", lambda: settings)
    monkeypatch.setattr(serve_mod, "_STALE_FLOOR_SECONDS", 0.5)
    monkeypatch.setattr(settings, "login_qr_refresh", False)
    monkeypatch.setattr(settings, "chat_poll_seconds", 0)

    def broken_channels(s, log_wecom=False):
        raise RuntimeError("cycle always fails")

    services = ServeServices(
        run_action=lambda *a: "", handle_turn=lambda *a, **k: None,
        build_channels=broken_channels,
        email_channel=lambda s: None, fire_due=lambda s: None,
        acquire_pid_lock=lambda s: True, worker_dispatch={},
        channel_health=lambda: {})
    t = threading.Thread(target=serve_mod.run_serve, args=(settings, services))
    t.start()
    try:
        import time as _t

        for _ in range(100):
            if "server" in captured:
                break
            _t.sleep(0.05)
        base = f"http://127.0.0.1:{captured['server'].server_address[1]}"
        r = httpx.get(f"{base}/readyz", timeout=5)   # FIRST probe: already 503
        assert r.status_code == 503
        assert r.json()["first_cycle_done"] is False
    finally:
        captured["server"].shutdown()
        t.join(timeout=15)


def test_delayed_thread_start_then_immediate_release_no_hang(settings, monkeypatch):
    """Regression for the shutdown-before-serve_forever race: a bind whose
    server thread is slow to get scheduled must not hang an immediate
    release() — the started-handshake gates shutdown()."""
    import threading
    import time as _t

    real_thread = threading.Thread

    class SlowThread:
        def __init__(self, *a, target=None, **k):
            self._target = target
            self._k = k

        def start(self):
            def delayed():
                _t.sleep(0.5)          # thread exists but not yet scheduled
                self._target()

            self._t = real_thread(target=delayed, daemon=True)
            self._t.start()

        def is_alive(self):
            return self._t.is_alive()

        def join(self, timeout=None):
            self._t.join(timeout)

    _wecom_settings(settings, monkeypatch, _free_port())
    monkeypatch.setattr(wecom.threading, "Thread", SlowThread)
    ch = wecom.WeComChannel(settings)
    assert ch.start_callback_server() is True
    monkeypatch.setattr(wecom.threading, "Thread", real_thread)

    done = threading.Event()
    t = real_thread(target=lambda: (wecom.release_callback_server(), done.set()))
    t.start()
    assert done.wait(timeout=8), "release() hung on a not-yet-started server"
    assert wecom.callback_health()["wecom_callback_state"] == "off"



def _wait_until(cond, timeout=8.0, interval=0.05):
    """Deterministic condition wait (no fixed sleeps — meaningful under slow
    CI): polls `cond` until true or the deadline, returning the last value."""
    import time as _t

    deadline = _t.monotonic() + timeout
    while _t.monotonic() < deadline:
        if cond():
            return True
        _t.sleep(interval)
    return cond()


def test_concurrent_request_cap(settings, monkeypatch):
    """Aggregate request threads are bounded: past the cap, new connections
    are closed immediately instead of pinning another thread — and the cap
    frees when clients disconnect."""
    monkeypatch.setattr(wecom, "_MAX_CONCURRENT_CALLBACKS", 2)
    _holder.reset_for_tests()          # rebuild slots under the shrunk cap
    port = _free_port()
    _wecom_settings(settings, monkeypatch, port)
    assert wecom.WeComChannel(settings).start_callback_server() is True

    def stalled():
        c = socket.create_connection(("127.0.0.1", port), timeout=5)
        c.sendall(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 300\r\n\r\nabc")
        return c

    holders = [stalled(), stalled()]
    # both request threads hold slots once the cap refuses a new connection
    def over_cap_refused():
        try:
            extra = socket.create_connection(("127.0.0.1", port), timeout=5)
            extra.settimeout(2)
            extra.sendall(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n")
            refused = extra.recv(64) == b""
            extra.close()
            return refused
        except OSError:
            return False

    try:
        assert _wait_until(over_cap_refused), "cap never engaged"
    finally:
        for c in holders:
            c.close()
    # slots free once the disconnected threads exit — observable via service
    assert _wait_until(lambda: b"400" in _raw_status(port, ""), timeout=25), \
        "service did not resume after slots freed"


def _raw_status(port: int, headers: str) -> bytes:
    """Send a hand-built POST (bad Content-Length values no client library
    will emit) and return the status line."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(f"POST / HTTP/1.1\r\nHost: x\r\n{headers}\r\n".encode())
        return sock.recv(64).split(b"\r\n")[0]


def test_content_length_validation(settings, monkeypatch):
    """The public callback listener rejects malformed/oversized bodies before
    reading, and still serves normal requests after."""
    port = _free_port()
    _wecom_settings(settings, monkeypatch, port)
    assert wecom.WeComChannel(settings).start_callback_server() is True

    assert b"413" in _raw_status(port, "Content-Length: -5\r\n")        # negative
    assert b"413" in _raw_status(port, f"Content-Length: {2 << 20}\r\n")  # huge
    assert b"411" in _raw_status(port, "Content-Length: abc\r\n")       # malformed
    # absent Content-Length → treated as 0 → normal (failing decrypt) 400 path
    assert b"400" in _raw_status(port, "")


def test_readyz_fails_closed_without_poller(settings, monkeypatch):
    """A bare HTTP server with no poll thread attached is NOT a ready daemon."""
    import threading

    import httpx

    from assistant.agent.app import build_services
    from assistant.platform import serve as serve_mod

    server = serve_mod.make_server(settings_factory=lambda: settings, port=0,
                                   services=build_services())
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        r = httpx.get(f"{base}/readyz", timeout=5)
        assert r.status_code == 503 and r.json()["ready"] is False
        assert httpx.get(f"{base}/healthz", timeout=5).json() == {"ok": True}
    finally:
        server.shutdown()
        server.server_close()


def test_readyz_stale_and_dead_poller_after_success(settings, monkeypatch):
    """After a successful first cycle: a stale heartbeat degrades, and so does
    a dead poll thread."""
    import threading
    import time as _t

    import httpx

    from assistant.agent.app import build_services
    from assistant.platform import serve as serve_mod

    monkeypatch.setattr(serve_mod, "_STALE_FLOOR_SECONDS", 0.2)
    monkeypatch.setattr(settings, "chat_poll_seconds", 0)
    server = serve_mod.make_server(settings_factory=lambda: settings, port=0,
                                   services=build_services())
    _attach_live_poller(server)
    server.poll_heartbeat = {"ts": _t.monotonic() - 60}   # long-stale cycle
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        r = httpx.get(f"{base}/readyz", timeout=5)
        assert r.status_code == 503 and r.json()["poll_cycle_stale"] is True

        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        server.chat_poller = dead                          # dead poll thread
        server.poll_heartbeat = {"ts": _t.monotonic()}     # fresh heartbeat
        r = httpx.get(f"{base}/readyz", timeout=5)
        assert r.status_code == 503 and r.json()["poller_alive"] is False
    finally:
        server.shutdown()
        server.server_close()


def test_run_serve_poller_start_failure_still_cleans_up(settings, monkeypatch):
    """A poller that fails to start must not leak the listener socket or skip
    channel teardown — cleanup steps are independently guarded."""
    import socket as _socket
    import threading

    import pytest as _pytest

    from assistant.platform import serve as serve_mod
    from assistant.platform.serve import ServeServices

    captured = {}
    real_make = serve_mod.make_server

    def capture(*a, **k):
        captured["server"] = real_make(settings_factory=lambda: settings,
                                       port=0, services=k.get("services"))
        return captured["server"]

    real_thread = threading.Thread

    class BadPoller:
        def __init__(self, *a, **k):
            self.ident = None

        def start(self):
            raise RuntimeError("no threads left")

    monkeypatch.setattr(serve_mod, "make_server", capture)
    monkeypatch.setattr(serve_mod.signal, "signal", lambda *a: None)
    monkeypatch.setattr(serve_mod, "Settings", lambda: settings)
    monkeypatch.setattr(serve_mod.threading, "Thread", BadPoller)
    monkeypatch.setattr(settings, "login_qr_refresh", False)

    events = []
    services = ServeServices(
        run_action=lambda *a: "", handle_turn=lambda *a, **k: None,
        build_channels=lambda s, log_wecom=False: [],
        email_channel=lambda s: None, fire_due=lambda s: None,
        acquire_pid_lock=lambda s: True, worker_dispatch={},
        teardown_channels=lambda: events.append("teardown"),
        channel_health=lambda: {})
    with _pytest.raises(RuntimeError):
        serve_mod.run_serve(settings, services)
    monkeypatch.setattr(serve_mod.threading, "Thread", real_thread)
    assert events == ["teardown"]                 # teardown not skipped
    port = captured["server"].server_address[1]
    with _socket.socket() as probe:               # listener socket released
        probe.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", port))


def test_server_that_never_enters_serve_loop_release_returns(settings, monkeypatch):
    """The round-8 blocker window, deterministically: a bind whose thread NEVER
    runs serve_forever (entered_serve never set) — release() must skip
    shutdown(), return promptly, and leave the port rebindable."""
    import threading

    real_thread = threading.Thread

    class NeverRuns:
        def __init__(self, *a, **k):
            self.ident = 1

        def start(self):
            pass                      # thread "starts" but target never runs

        def is_alive(self):
            return True

        def join(self, timeout=None):
            pass

    port = _free_port()
    _wecom_settings(settings, monkeypatch, port)
    monkeypatch.setattr(wecom.threading, "Thread", NeverRuns)
    ch = wecom.WeComChannel(settings)
    assert ch.start_callback_server() is True
    monkeypatch.setattr(wecom.threading, "Thread", real_thread)
    bound_server = wecom._holder._server

    done = threading.Event()
    t = real_thread(target=lambda: (wecom.release_callback_server(), done.set()))
    t.start()
    assert done.wait(timeout=8), "release() hung on a never-entered serve loop"
    # the listening socket is truly closed (fd released) — asserted on the fd
    # itself: this box's other services can grab a just-freed port, so a
    # rebind probe would be flaky where the fd check is exact
    assert bound_server.socket.fileno() == -1
    # LATE ENTRY (the round-9 TOCTOU): serve_forever entering AFTER the close
    # decision must exit CLEANLY on the pre-set stop request — terminated by
    # return, not by an exception on the closed fd, and no zombie spin
    errors = []

    def run_late():
        try:
            bound_server.serve_forever()
        except BaseException as exc:   # noqa: BLE001 — recording, not hiding
            errors.append(exc)

    late = real_thread(target=run_late, daemon=True)
    late.start()
    late.join(timeout=5)
    assert not late.is_alive(), "late-entering serve loop did not terminate"
    assert errors == [], f"late entry exited by exception: {errors}"


def test_concurrent_cap_is_process_wide_across_rotation(settings, monkeypatch):
    """The request-thread bound survives server rotation: stalled requests
    from the OLD generation still count against the cap the NEW generation
    enforces — rapid rotations cannot multiply it."""
    monkeypatch.setattr(wecom, "_MAX_CONCURRENT_CALLBACKS", 2)
    port1 = _free_port()
    _wecom_settings(settings, monkeypatch, port1)
    _holder.reset_for_tests()          # rebuild slots under the shrunk cap
    assert wecom.WeComChannel(settings).start_callback_server() is True

    def stalled(port):
        c = socket.create_connection(("127.0.0.1", port), timeout=5)
        c.sendall(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 300\r\n\r\nab")
        return c

    def request_refused(port):
        """True only when the cap closes a complete request without a reply."""
        try:
            with socket.create_connection(
                    ("127.0.0.1", port), timeout=5) as extra:
                extra.settimeout(2)
                # A complete bad request returns HTTP 400 when accepted. Only
                # EOF proves the process-wide cap refused it before dispatch.
                extra.sendall(
                    b"POST / HTTP/1.1\r\nHost: x\r\n"
                    b"Content-Length: 0\r\n\r\n")
                return extra.recv(64) == b""
        except OSError:
            return False

    holders = []
    try:
        holders.append(stalled(port1))
        holders.append(stalled(port1))
        # connect/send only queues work; establish the happens-before edge by
        # proving both gen1 handlers consumed the two slots before rotation.
        assert _wait_until(lambda: request_refused(port1), timeout=8), \
            "old generation never filled cap"

        port2 = _free_port()
        monkeypatch.setattr(settings, "wecom_callback_port", port2)  # rotate
        assert wecom.WeComChannel(settings).start_callback_server() is True
        assert _wait_until(lambda: request_refused(port2), timeout=8), \
            "cap not shared across rotation"
    finally:
        for c in holders:
            c.close()
    assert _wait_until(lambda: b"400" in _raw_status(port2, ""), timeout=25), \
        "service did not resume on the new generation"


def test_image_callback_becomes_unsupported_media_event(settings, monkeypatch):
    """F8: an owner's WeCom image message (MsgType=image, no Content) used to
    fall through both branches — 200 OK, nothing else, the photo silently
    ignored. It now queues a structured event the poll loop answers with a
    fixed reply; non-owner images stay ignored."""
    import httpx

    port = _free_port()
    _wecom_settings(settings, monkeypatch, port, owner="boss")
    ch = wecom.WeComChannel(settings)
    assert ch.start_callback_server() is True

    def post_msg(inner_xml):
        blob, sig, ts, nonce = _encrypt_callback(
            settings.wecom_token, settings.wecom_aes_key,
            settings.wecom_corp_id, inner_xml)
        return httpx.post(
            f"http://127.0.0.1:{port}/?msg_signature={sig}&timestamp={ts}&nonce={nonce}",
            content=f"<xml><Encrypt>{blob}</Encrypt></xml>", timeout=5)

    r = post_msg("<xml><FromUserName>boss</FromUserName>"
                 "<MsgType>image</MsgType><PicUrl>http://x</PicUrl></xml>")
    assert r.status_code == 200
    events = ch.poll()
    assert len(events) == 1
    assert events[0]["kind"] == "unsupported_media"
    assert events[0]["sender"] == "boss" and events[0]["text"] == "[图片]"

    r = post_msg("<xml><FromUserName>stranger</FromUserName>"
                 "<MsgType>image</MsgType></xml>")
    assert r.status_code == 200
    assert ch.poll() == []                       # non-owner image ignored
