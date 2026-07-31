"""WeChat Work (企业微信) channel — reaches the owner inside regular WeChat.

Setup (one-time, owner):
1. Register a free WeCom org at work.weixin.qq.com (no business verification
   needed for personal use), create a self-built app (自建应用) → gives
   WECOM_CORP_ID / WECOM_SECRET / WECOM_AGENT_ID; WECOM_OWNER_USERID is your
   member id (usually your name pinyin, see 通讯录).
2. In WeChat: 我 → 设置 → 插件 → 企业微信 (WeChat plugin), scan the org QR —
   the app's messages then arrive inside WeChat and you can reply there.
3. Receiving replies requires the app's 接收消息 callback URL to reach this
   machine (tunnel/VPS → this port). Configure Token + EncodingAESKey from
   that page as WECOM_TOKEN / WECOM_AES_KEY.

Sending only needs outbound HTTPS, so push works even without the callback.
"""

import base64
import hashlib
import logging
import queue
import struct
import threading
import time
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import httpx

from assistant.platform.config import Settings

log = logging.getLogger("assistant")

_API = "https://qyapi.weixin.qq.com/cgi-bin"

_MAX_CONCURRENT_CALLBACKS = 32  # aggregate request-thread bound (public listener)


class _CappedThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a hard concurrent-request bound: the callback
    port is public (tunneled), and without a cap each stalled client pins a
    thread — the 20s socket timeout bounds duration, this bounds count.
    Overflow connections are closed immediately (WeCom retries)."""

    def __init__(self, *a, slots: threading.BoundedSemaphore | None = None,
                 **k):
        super().__init__(*a, **k)
        # The slot semaphore is INJECTED (holder-owned, process-wide): a
        # per-server semaphore would reset on every rotation while the old
        # generation's request threads live out their socket timeouts —
        # letting rapid rotations multiply the "aggregate" bound.
        self._slots = slots or threading.BoundedSemaphore(_MAX_CONCURRENT_CALLBACKS)
        self._stop = threading.Event()
        self._stopped = threading.Event()
        self._stopped.set()   # no loop running yet

    def request_shutdown(self):
        """Non-blocking, entry-order-independent shutdown request (public
        state only — no CPython privates). `BaseServer.shutdown()` blocks
        until the loop exits (deadlocks if it never entered) — unusable under
        a lock; and any timed proof-of-entry check races a loop that enters
        just after the check. Our own stop event + our own loop (below)
        close every ordering."""
        self._stop.set()

    def shutdown(self):
        """Drop-in for callers expecting `BaseServer.shutdown()`, with one
        deliberate difference: BOUNDED. It requests the stop and waits up to
        10s for the loop's own confirmation instead of potentially forever —
        a loop that never entered (or never will) cannot deadlock a caller.
        The postcondition is therefore "stop requested, loop exit confirmed
        or timed out", not the stdlib's unconditional exit-confirmed."""
        self._stop.set()
        self._stopped.wait(10)

    def serve_forever(self, poll_interval=0.5):
        """Own accept loop over public APIs, honoring `request_shutdown` in
        every ordering: request-before-entry returns immediately; request-
        during-loop exits within `poll_interval`; entry AFTER the socket was
        closed with a pending request exits cleanly instead of raising on
        the dead fd (the stdlib loop registers the socket before checking
        its flag, which is exactly the late-entry race)."""
        import selectors

        if self._stop.is_set():
            return
        self._stopped.clear()
        try:
            with selectors.DefaultSelector() as sel:
                sel.register(self, selectors.EVENT_READ)
                while not self._stop.is_set():
                    if sel.select(poll_interval):
                        if self._stop.is_set():
                            return
                        # documented BaseServer API only (get_request /
                        # verify_request / process_request / handle_error /
                        # shutdown_request) — no private stdlib internals
                        try:
                            request, client_address = self.get_request()
                        except OSError:
                            continue
                        if self.verify_request(request, client_address):
                            try:
                                self.process_request(request, client_address)
                            except Exception:
                                self.handle_error(request, client_address)
                                self.shutdown_request(request)
                        else:
                            self.shutdown_request(request)
                    self.service_actions()
        except (OSError, ValueError):
            if self._stop.is_set():
                return          # closed-under-us with a pending stop: clean
            raise
        finally:
            self._stopped.set()

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            log.warning("wecom callback: concurrent request cap hit — closing")
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class WeComChannel:
    """WeChat Work channel: pushes agent replies to the owner and, when a
    callback server is configured, receives their messages. Sending needs only
    outbound HTTPS (``enabled`` tracks that the corp/secret/agent creds exist);
    inbound arrives asynchronously via an internal inbox queue fed by the
    callback HTTP server."""

    name = "wecom"

    def __init__(self, settings: Settings):
        """Set ``enabled`` from the presence of corp/secret/agent creds and
        initialize the empty access-token cache. The inbox starts as a private
        placeholder queue; ``start_callback_server`` swaps in the process-wide
        holder's inbox so messages survive the per-cycle channel rebuilds."""
        self.settings = settings
        self.enabled = bool(settings.wecom_corp_id and settings.wecom_secret
                            and settings.wecom_agent_id)
        self._token: str = ""
        self._token_expiry: float = 0.0
        self._inbox: queue.Queue = queue.Queue()

    # ── sending (outbound HTTPS only) ────────────────────────────────
    def _access_token(self) -> str:
        """Return a valid WeCom API access token, fetching a fresh one only
        when the cached token is within 60s of expiry. Raises on an API error."""
        if time.time() < self._token_expiry - 60:
            return self._token
        resp = httpx.get(f"{_API}/gettoken", params={
            "corpid": self.settings.wecom_corp_id,
            "corpsecret": self.settings.wecom_secret}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode"):
            raise RuntimeError(f"wecom gettoken: {data}")
        self._token = data["access_token"]
        self._token_expiry = time.time() + data.get("expires_in", 7200)
        return self._token

    def send(self, text: str, in_reply_to: dict | None = None) -> None:
        """Push ``text`` (capped at 2000 chars) to the owner as a WeCom text
        message, or to everyone in the app when no owner userid is set.
        ``in_reply_to`` is unused — WeChat has no reply threading. Raises on an
        API error."""
        resp = httpx.post(f"{_API}/message/send",
                          params={"access_token": self._access_token()},
                          json={"touser": self.settings.wecom_owner_userid or "@all",
                                "msgtype": "text",
                                "agentid": self.settings.wecom_agent_id,
                                "text": {"content": text[:2000]}}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode"):
            raise RuntimeError(f"wecom send: {data}")

    # ── receiving (callback server; needs a public tunnel to this port) ──
    def poll(self) -> list[dict]:
        """Drain and return every message the callback server has queued since
        the last poll (empty list if none) — non-blocking."""
        messages = []
        while True:
            try:
                messages.append(self._inbox.get_nowait())
            except queue.Empty:
                return messages

    def start_callback_server(self) -> bool:
        """Adopt the process-wide callback server (binding it on first use), so
        inbound messages land in this channel's inbox queue. Returns False
        (send-only) when the Token/AESKey needed to decrypt callbacks aren't
        configured or the bind failed; True once the shared server is serving.

        Idempotent by design: the serve poll loop rebuilds channels every cycle
        for `.env` hot-reload, so this must NOT bind a fresh server per call —
        that raised EADDRINUSE into the cycle handler and silently disabled
        email polling, reminders, and routines on every cycle after the first,
        while `/healthz` stayed green. `_CallbackHolder` owns the lifecycle."""
        if not (self.settings.wecom_token and self.settings.wecom_aes_key):
            return False
        inbox = _holder.acquire(self.settings)
        if inbox is None:
            return False
        self._inbox = inbox
        return True


def _make_handler(holder: "_CallbackHolder", generation: int, crypto: "_MsgCrypto",
                  owner: str, principal: tuple):
    """The callback HTTP handler class for one bound server generation: GET
    serves the one-time URL-verification handshake, POST receives owner
    messages. Closes over the holder + its generation (so a request thread from
    a torn-down server can't leak messages across an identity change) and the
    identity's own ``crypto``/``owner`` — never live Settings, which may have
    been hot-reloaded to a different identity since the bind."""

    class Handler(BaseHTTPRequestHandler):
        timeout = 20   # socket timeout: a slow/partial-body client gets cut,
        #                never pins a request thread indefinitely

        def log_message(self, *args):
            """Silence the default stderr access log; route hits to our
            logger at debug level instead."""
            log.debug("wecom callback: %s", args)

        def _reply(self, code: int, body: str = "") -> None:
            """Write a bare HTTP response with ``code`` and optional body —
            the minimal reply WeCom expects (no headers beyond status)."""
            self.send_response(code)
            self.end_headers()
            self.wfile.write(body.encode())

        def do_GET(self):
            """WeCom URL-verification handshake: decrypt the ``echostr`` and
            echo it back (200) to prove ownership of Token/AESKey, or 400 if
            verification fails."""
            q = parse_qs(urlparse(self.path).query)
            try:
                echo = crypto.decrypt(
                    q["echostr"][0], q["msg_signature"][0],
                    q["timestamp"][0], q["nonce"][0])
                self._reply(200, echo)
            except Exception as exc:
                log.warning("wecom verification failed: %s", exc)
                self._reply(400)

        def do_POST(self):
            """Receive an incoming WeCom message: decrypt the body, verify
            the sender is the owner, and enqueue the text for ``poll`` to
            pick up. Always answers an empty 200 (no passive reply — the
            agent pushes its answer asynchronously via ``send``); a decrypt
            failure answers 400."""
            q = parse_qs(urlparse(self.path).query)
            # This listener is public (tunneled). Validate Content-Length and
            # REJECT bad bodies before reading — a WeCom text callback is a
            # few KB; truncating and parsing an attacker-sized body is not a
            # fallback, it's a bug.
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except (TypeError, ValueError):
                return self._reply(411)          # length required/unparseable
            if length < 0 or length > (1 << 20):
                return self._reply(413)          # payload too large
            raw = self.rfile.read(length)
            try:
                encrypted = ET.fromstring(raw).findtext("Encrypt", "")
                xml_text = crypto.decrypt(
                    encrypted, q["msg_signature"][0],
                    q["timestamp"][0], q["nonce"][0])
                root = ET.fromstring(xml_text)
                sender = root.findtext("FromUserName", "")
                text = (root.findtext("Content") or "").strip()
                if text and (not owner or sender == owner):
                    holder.enqueue(generation, principal, {
                        "channel": "wecom", "text": text[:4000],
                        "subject": "", "sender": sender})
                elif text:
                    log.warning("wecom message from non-owner %r ignored", sender)
                self._reply(200)  # empty 200 = no passive reply; we push async
            except Exception as exc:
                log.warning("wecom callback decrypt failed: %s", exc)
                self._reply(400)

    return Handler


class _CallbackHolder:
    """Process-wide owner of the WeCom callback HTTP server and its inbox.

    One holder outlives the per-cycle channel rebuilds: an ``acquire`` with the
    same identity reuses the running server AND its inbox (messages received
    between polls survive), a changed identity (credential/port hot-reload)
    tears the old server down and rebinds, and a dead server thread is
    detected and rebound rather than reused forever. The inbox is bounded so
    process-lifetime state can't grow without limit, enqueues are stamped with
    a bind generation so a lingering request thread from a torn-down server
    can't deliver into a different identity's queue, and ``teardown`` latches
    the holder closed so nothing rebinds during daemon shutdown.

    Process invariant: the holder is module-global, i.e. per-PROCESS. The
    serve pid lock is data-directory-scoped, so two daemons in two processes
    (even sharing a data dir config mistake) each have their own holder; two
    daemons inside ONE process would share it — an unsupported topology no
    CLI entry point creates."""

    _INBOX_MAX = 256

    def __init__(self):
        self._lock = threading.Lock()
        self._identity: tuple | None = None
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._inbox: queue.Queue | None = None
        self._generation = 0
        self._dropped = 0
        self._closed = False
        self._state = "off"   # off | ok | bind_failed | dead | shutdown
        # process-wide request-thread bound, shared across server generations
        self._slots = threading.BoundedSemaphore(_MAX_CONCURRENT_CALLBACKS)
        # A same-principal rotation whose REPLACEMENT bind fails must not lose
        # the owner's queued messages: they wait here and the next successful
        # bind for that principal adopts them.
        self._orphan: tuple[tuple, queue.Queue] | None = None

    @staticmethod
    def _identity_of(settings: Settings) -> tuple:
        """Everything a bound server bakes in at bind time — a change in any
        of these must tear down and rebind, not silently keep serving with
        stale credentials or delivering to a stale owner. The first element is
        the PRINCIPAL (whose messages these are: corp + owner); the rest is
        the binding. A rebind that keeps the principal keeps the queue — a
        token/AES/port rotation must not lose the owner's queued messages."""
        return ((settings.wecom_corp_id, settings.wecom_owner_userid),
                settings.wecom_callback_port, settings.wecom_token,
                settings.wecom_aes_key)

    def acquire(self, settings: Settings) -> queue.Queue | None:
        """Return the inbox for this identity, binding/rebinding as needed;
        None = degrade to send-only this cycle (bind failed or shut down)."""
        identity = self._identity_of(settings)
        with self._lock:
            if self._closed:
                return None
            if (self._identity == identity and self._thread is not None
                    and self._thread.is_alive()):
                return self._inbox
            keep_queue = None
            if self._server is not None:
                same_principal = (self._identity is not None
                                  and self._identity[0] == identity[0])
                if same_principal:
                    keep_queue = self._inbox   # rotation, not a new principal
                queued = self._inbox.qsize() if self._inbox is not None else 0
                self._close_locked()
                if queued and keep_queue is None:
                    log.warning("wecom callback: principal changed — dropped %d "
                                "queued message(s) from the previous identity",
                                queued)
            if keep_queue is None and self._orphan is not None:
                if self._orphan[0] == identity[0]:
                    keep_queue = self._orphan[1]   # adopt a failed rotation's queue
                else:
                    self._orphan = None            # different principal: discard
            return self._bind_locked(identity, settings, keep_queue)

    def enqueue(self, generation: int, principal: tuple, message: dict) -> None:
        """Bounded enqueue from a callback request thread. A stale generation
        is still accepted when its PRINCIPAL (corp+owner) matches the current
        bind or the orphan of a failed same-principal rotation — an in-flight
        owner message must survive a token/port rotation — but never across a
        principal change (that would leak one owner's message into another's
        queue) and never after shutdown or a deliberate release. Explicit
        tradeoff: a message authenticated under the PRE-rotation token that is
        still in flight when the token rotates IS delivered — rotation is not
        a revocation boundary for that one in-flight window (same corp+owner;
        the alternative silently drops an owner message). Overflow drops the
        OLDEST message (the newest is what the owner just sent)."""
        with self._lock:
            inbox = None
            if not self._closed:
                current = self._identity[0] if self._identity else None
                if self._inbox is not None and (
                        generation == self._generation or principal == current):
                    inbox = self._inbox
                elif self._orphan is not None and principal == self._orphan[0]:
                    inbox = self._orphan[1]   # rotation failed mid-request:
                    #                           the owner's message still lands
            if inbox is None:
                log.warning("wecom callback: enqueue from stale server "
                            "(generation %d) dropped", generation)
                return
        try:
            inbox.put_nowait(message)
        except queue.Full:
            try:
                inbox.get_nowait()
            except queue.Empty:
                pass
            self._dropped += 1
            log.warning("wecom inbox full — dropped oldest message "
                        "(%d dropped total)", self._dropped)
            try:
                inbox.put_nowait(message)
            except queue.Full:
                pass

    def status(self) -> dict:
        """`/healthz` fields. `wecom_callback_ok`: True = serving; False =
        should be serving but isn't (bind_failed / dead) — explicitly
        unhealthy, alert on it; None = intentionally not running (off /
        shutdown). `wecom_callback_state` names the exact state. Monitoring
        guidance: `ok` stays bare process liveness (the restart tooling asks
        "is the daemon up"); alert on `wecom_callback_ok is False` or
        `poller_alive is False` for degraded-but-alive states."""
        with self._lock:
            if self._server is None:
                ok = False if self._state == "bind_failed" else None
                return {"wecom_callback_ok": ok,
                        "wecom_callback_state": self._state}
            alive = bool(self._thread is not None and self._thread.is_alive())
            if not alive:
                self._state = "dead"
            return {"wecom_callback_ok": alive,
                    "wecom_callback_state": self._state if alive else "dead"}

    def release(self) -> None:
        """Desired-state sync for a cycle whose config no longer wants a
        callback server (credentials removed / channel disabled): close it —
        WITHOUT the shutdown latch, so re-adding the config next cycle
        rebinds. Without this, deleting WECOM_TOKEN from .env left the old
        listener serving with stale credentials forever."""
        with self._lock:
            if self._server is not None:
                log.info("wecom callback: config no longer enables receive — "
                         "server released")
            self._close_locked()
            self._orphan = None   # a deliberate disable drops queued messages;
            #                       re-enabling later must not replay them
            self._state = "off"

    def teardown(self) -> None:
        """Daemon shutdown: close the server and latch — any later `acquire`
        is a no-op returning None, so a poller outliving its bounded join
        cannot rebind after shutdown."""
        with self._lock:
            self._closed = True
            self._close_locked()
            self._orphan = None
            self._state = "shutdown"

    def reset_for_tests(self) -> None:
        """Test-fixture teardown: close and un-latch for the next test."""
        with self._lock:
            self._close_locked()
            self._orphan = None
            self._closed = False
            self._dropped = 0
            self._state = "off"
            # fresh cap so tests that shrink _MAX_CONCURRENT_CALLBACKS (and
            # any stalled threads from a previous test) start clean
            self._slots = threading.BoundedSemaphore(_MAX_CONCURRENT_CALLBACKS)

    def _close_locked(self) -> None:
        """Stop and fully release the current server (idempotent). After
        `server_close()` the listening socket is freed, so a rebind is safe
        even if a lingering request thread outlives the bounded join.
        Shutdown is a non-blocking flag raise (`request_shutdown`) rather
        than `BaseServer.shutdown()`: the blocking form deadlocks when the
        loop never entered and any timed entry-proof races a loop entering
        just after the check — the flag is atomic with respect to entry
        order, and this method holds the holder lock, so nothing unbounded
        may run under it."""
        server, thread = self._server, self._thread
        self._server = self._thread = self._inbox = None
        self._identity = None
        if server is None:
            return
        try:
            # Atomic w.r.t. loop entry, non-blocking, safe under the holder
            # lock: raise the serve_forever exit flag. A running loop exits
            # within its poll interval; one that enters after this line sees
            # the flag immediately — the late-entry TOCTOU (loop spinning
            # forever on a closed epoll fd) cannot occur.
            if hasattr(server, "request_shutdown"):
                server.request_shutdown()
        except Exception:
            log.exception("wecom callback shutdown request failed")
        finally:
            try:
                server.server_close()   # ALWAYS release the listening socket
            except Exception:
                log.exception("wecom callback socket close failed")
        if thread is not None:
            thread.join(timeout=5)
            if thread.is_alive():
                log.warning("wecom callback thread still alive after close "
                            "(socket already released — rebind is safe)")

    def _bind_locked(self, identity: tuple, settings: Settings,
                     keep_queue: queue.Queue | None = None) -> queue.Queue | None:
        """Bind a fresh server for `identity`. Any failure (port taken by
        another process, bad AES key, thread start) degrades to send-only —
        it must never propagate into the poll cycle, which is exactly the bug
        this holder exists to fix. `keep_queue` carries the previous inbox
        across a same-principal rotation so queued owner messages survive."""
        try:
            crypto = _MsgCrypto(settings.wecom_token, settings.wecom_aes_key,
                                settings.wecom_corp_id)
            handler = _make_handler(self, self._generation + 1, crypto,
                                    settings.wecom_owner_userid, identity[0])
            server = _CappedThreadingHTTPServer(
                ("0.0.0.0", settings.wecom_callback_port), handler,
                slots=self._slots)
        except Exception as exc:
            log.warning("wecom callback bind failed (%s) — send-only this "
                        "cycle, retrying next cycle", exc)
            self._state = "bind_failed"
            if keep_queue is not None:   # a failed rotation must not lose the
                self._orphan = (identity[0], keep_queue)   # owner's queue
            return None
        # Close-path notes, verified against CPython 3.11: ThreadingHTTPServer
        # request threads are daemonic (daemon_threads=True) and
        # socketserver._Threads.append skips daemon threads, so server_close()
        # never joins request threads here — block_on_close=False is
        # defense-in-depth should either default ever change (a join under
        # self._lock would deadlock with enqueue() waiting for the lock).
        server.block_on_close = False
        thread = threading.Thread(target=server.serve_forever,
                                  name="wecom-callback", daemon=True)
        # Publish holder state BEFORE the listener serves: a callback accepted
        # in the start window must find generation/inbox in place, not be
        # rejected as stale. (We hold the lock, so enqueue can't interleave —
        # publication order still matters for a start() that itself fails.)
        old = (self._generation, self._identity, self._server, self._inbox,
               self._orphan, self._thread, self._state)
        self._generation += 1
        self._identity = identity
        self._server = server
        self._inbox = keep_queue if keep_queue is not None \
            else queue.Queue(maxsize=self._INBOX_MAX)
        self._orphan = None
        self._thread = thread
        self._state = "ok"
        try:
            thread.start()
        except Exception:   # thread spawn failed: roll back, release, degrade
            log.exception("wecom callback thread start failed")
            (self._generation, self._identity, self._server, self._inbox,
             self._orphan, self._thread, self._state) = old
            server.server_close()
            self._state = "bind_failed"
            if keep_queue is not None:
                self._orphan = (identity[0], keep_queue)
            return None
        log.info("wecom callback server on :%d", settings.wecom_callback_port)
        return self._inbox


_holder = _CallbackHolder()


def teardown_callback_server() -> None:
    """`ServeServices.teardown_channels` hook: close the callback server and
    prevent any rebind (daemon shutdown)."""
    _holder.teardown()


def reopen_callback_server() -> None:
    """`ServeServices.startup_channels` hook: un-latch the holder at daemon
    start. Note the supported topology: sequential `run_serve` calls in ONE
    process are already blocked by the shared pid-file lock (the second
    acquire finds its own live pid in the file and refuses), so this hook
    exists for embedded/test harnesses that drive the lifecycle directly —
    a stale poller from a previous lifecycle is not a supported concurrent
    caller."""
    with _holder._lock:
        _holder._closed = False
        if _holder._state == "shutdown":
            _holder._state = "off"


def release_callback_server() -> None:
    """Desired-state sync: this cycle's config does not enable receiving —
    close any running callback server without latching (re-adding the config
    rebinds next cycle)."""
    _holder.release()


def callback_health() -> dict:
    """`ServeServices.channel_health` hook: `/healthz` fields for the callback
    server (the 2026-07 outage was invisible precisely because health stayed
    green while the poll cycle failed)."""
    return _holder.status()


class _MsgCrypto:
    """WeCom callback crypto (WXBizMsgCrypt): SHA1 signature + AES-256-CBC."""

    def __init__(self, token: str, encoding_aes_key: str, corp_id: str):
        """Cache the crypto primitives and decode the base64 EncodingAESKey to
        its 32-byte AES key (raising if it isn't exactly 32 bytes). ``token``
        signs callbacks and ``corp_id`` is checked against the decrypted
        payload's receiver."""
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        self._cipher_parts = (Cipher, algorithms, modes)
        self.token = token
        self.key = base64.b64decode(encoding_aes_key + "=")
        if len(self.key) != 32:
            raise ValueError("EncodingAESKey must decode to 32 bytes")
        self.corp_id = corp_id

    def decrypt(self, encrypted_b64: str, signature: str, timestamp: str, nonce: str) -> str:
        """Verify a callback's SHA1 signature, then AES-256-CBC decrypt the
        payload and return its inner message string. Raises ValueError on a bad
        signature or if the embedded corp id doesn't match — either means the
        request isn't a genuine WeCom callback. The wire format is a 16-byte
        random prefix, a 4-byte big-endian length, the message, then the corp
        id; PKCS#7 padding is stripped first."""
        expected = hashlib.sha1(
            "".join(sorted([self.token, timestamp, nonce, encrypted_b64])).encode()
        ).hexdigest()
        if expected != signature:
            raise ValueError("bad msg_signature")
        Cipher, algorithms, modes = self._cipher_parts
        decryptor = Cipher(algorithms.AES(self.key), modes.CBC(self.key[:16])).decryptor()
        plain = decryptor.update(base64.b64decode(encrypted_b64)) + decryptor.finalize()
        plain = plain[:-plain[-1]]  # strip PKCS#7 padding
        msg_len = struct.unpack(">I", plain[16:20])[0]
        msg = plain[20:20 + msg_len]
        receiver = plain[20 + msg_len:].decode()
        if receiver != self.corp_id:
            raise ValueError("corp id mismatch")
        return msg.decode()
