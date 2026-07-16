"""`assistant serve` — the one long-lived daemon behind the OpenClaw bridge.

Loopback-only HTTP (stdlib, zero new deps):

    POST /chat            {"session": "<channel:peer>", "text": "..."} → {"reply"}
    POST /actions/<name>  {<params>}                                   → {"result"}
    POST /run             {"resume": true?}                            → {"result"}
    GET  /status                                                       → {"status"}
    GET  /healthz                                                      → {"ok"}

Design invariants (doc/DESIGN_SERVICE_LAYER.md):
- ``Settings()``/``LLM()`` are rebuilt **per request**, so a `.env` edit (new
  API key, changed recipient) takes effect on the next message — the stale-
  credential failure class of the standalone listener is gone.
- /chat keeps a rolling per-session history (JSON spill under
  ``~/.personal-agent/sessions/``), which exec-per-message never had.
- The email chat poll runs as a background thread in here (OpenClaw has no
  IMAP channel), also with fresh Settings each cycle. The daemon holds the
  same ``chat_listener.pid`` lock as the standalone listener, so the two
  can never race one inbox watermark and the bridge supervisor's stale-pid
  takeover keeps working unchanged.
"""

import base64
import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .actions import run_action
from .chat.agent import handle_message
from .config import DEFAULT_UID, Settings
from .identity import Unauthorized, resolve_uid
from .llm import LLM
from .registry import UserRegistry

log = logging.getLogger("assistant")

_MAX_BODY = 12 * 1024 * 1024  # base64 image attachments ride in /chat bodies
_MAX_IMAGE_BYTES = 8 * 1024 * 1024  # per-image cap (multi_tenant, §A.4) —
                                    # matches the bridge's encodeBase64Capped


class SessionStore:
    """Rolling chat history per session id, spilled to disk so multi-turn
    context survives a daemon restart.

    Two decoupled horizons:

    - **context window** (``context_hours``, default 48 ≈ 2 days): only turns
      this recent enter a prompt (read-time filter — the token budget), capped
      at ``keep`` turns.
    - **retention** (``retention_days``, default 30 ≈ 1 month): how long turns
      stay on disk; the daily curate phase ``prune()``s past this.

    So the owner keeps a month of history while each reply only sees the last
    couple of days."""

    def __init__(self, data_dir: Path, keep: int = 10, context_hours: int = 48,
                 retention_days: int = 30, max_turns: int = 1500):
        """Store under `data_dir/sessions`. `keep`/`context_hours` bound what a
        prompt sees; `retention_days`/`max_turns` bound what stays on disk."""
        self.dir = data_dir / "sessions"
        self.keep = keep
        self.context_hours = context_hours
        self.retention_days = retention_days
        self.max_turns = max_turns

    def _path(self, session_id: str) -> Path:
        """Map a session id to its spill file via a short sha1 hash, so
        arbitrary channel:peer ids become safe fixed-length filenames."""
        return self.dir / (hashlib.sha1(session_id.encode()).hexdigest()[:16] + ".json")

    def _all(self, session_id: str) -> list[dict]:
        """Every stored turn for the session (unfiltered); `[]` when the file
        is absent or corrupt — a bad spill must never break a reply."""
        path = self._path(session_id)
        if not path.exists():
            return []
        try:
            return json.loads(path.read_text()).get("turns", [])
        except ValueError:
            return []

    def _within(self, turns: list[dict], hours: float) -> list[dict]:
        """Turns newer than `hours` ago; turns lacking a `ts` predate the
        timestamp feature and are treated as expired."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        return [t for t in turns if t.get("ts", "") >= cutoff]

    def history(self, session_id: str) -> list[dict]:
        """The turns a prompt should see: the last `keep` within the context
        window. Older turns stay on disk (retention) but never reach a prompt."""
        return self._within(self._all(session_id), self.context_hours)[-self.keep:]

    def append(self, session_id: str, owner: str, assistant: str) -> None:
        """Record one owner/assistant exchange (each side capped at 2000 chars).
        Keeps the whole retention window on disk — not just the prompt slice —
        bounded by `max_turns`, so a month of history survives while prompts
        stay short."""
        turns = self._within(self._all(session_id), self.retention_days * 24)
        turns.append({"ts": datetime.now(timezone.utc).isoformat(),
                      "owner": owner[:2000], "assistant": assistant[:2000]})
        self.dir.mkdir(parents=True, exist_ok=True)
        self._path(session_id).write_text(json.dumps(
            {"session": session_id, "turns": turns[-self.max_turns:]},
            ensure_ascii=False))

    def prune(self) -> dict:
        """Drop turns past the retention window from disk; delete session files
        left empty. Returns counts for the curate log/metrics."""
        removed_turns = removed_files = 0
        for path in self.dir.glob("*.json") if self.dir.exists() else []:
            try:
                data = json.loads(path.read_text())
            except ValueError:
                path.unlink(missing_ok=True)
                removed_files += 1
                continue
            turns = data.get("turns", [])
            fresh = self._within(turns, self.retention_days * 24)
            removed_turns += len(turns) - len(fresh)
            if not fresh:
                path.unlink(missing_ok=True)
                removed_files += 1
            elif len(fresh) != len(turns):
                path.write_text(json.dumps({**data, "turns": fresh}, ensure_ascii=False))
        return {"turns": removed_turns, "files": removed_files}


def make_server(settings_factory=Settings, llm_factory=None, port: int | None = None):
    """Build (but don't start) the HTTP server. Factories are per-request —
    that is the stale-credential fix — and injectable for tests."""
    boot = settings_factory()
    sessions = SessionStore(boot.data_dir, keep=boot.serve_session_turns,
                            context_hours=boot.chat_history_max_age_hours,
                            retention_days=boot.chat_history_retention_days)
    make_llm = llm_factory or (lambda s: LLM(s))

    class Handler(BaseHTTPRequestHandler):
        """Loopback JSON request handler closed over this server's per-request
        `settings_factory`, shared `sessions`, and `make_llm` — the closure is
        what gives every request fresh Settings/LLM (the stale-credential fix)."""

        server_version = "personal-agent"

        def log_message(self, fmt, *args):  # route to our logger, not stderr
            """Send stdlib access logs to our debug logger instead of stderr."""
            log.debug("serve: " + fmt, *args)

        def _send(self, code: int, payload: dict) -> None:
            """Write `payload` as a JSON response with `code` and the matching
            content-type/length headers."""
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self, settings: Settings) -> bool:
            """Gate a request on the bearer token: open when no `serve_token` is
            configured, else require an exact `Authorization: Bearer <token>`.

            This is the **single_user** gate only. It must never authorize a
            `multi_tenant` request — there the mandatory bridge-token check lives
            in `resolve_uid` (an unset token is never open access); see §A.2."""
            if not settings.serve_token:
                return True
            header = self.headers.get("Authorization", "")
            return header == f"Bearer {settings.serve_token}"

        def _bearer(self) -> str:
            """The presented `Authorization: Bearer <token>`, or `''`."""
            header = self.headers.get("Authorization", "")
            return header[7:] if header.startswith("Bearer ") else ""

        def _query(self) -> dict:
            """The request's query string as a flat `{key: first_value}` dict, so
            a GET (which has no body) can still carry `account_id`/`channel` for
            `resolve_uid` in `multi_tenant`."""
            return {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}

        def _resolve(self, body: dict):
            """Resolve this request to `(uid, per-user Settings, SessionStore,
            session-key prefix)`, or raise `Unauthorized`.

            `single_user` keeps the legacy path byte-for-byte: the `serve_token`
            bearer gate, the **boot-bound** `sessions` store (the `server.sessions`
            test seam), and **unprefixed** session keys — so history files written
            before multi-user still resolve. `multi_tenant` authenticates via the
            mandatory **bridge token** (`resolve_uid` — an unset/empty token is
            never open), builds a `SessionStore` rooted at the resolved user's own
            `data_dir` (path isolation), and **uid-prefixes** session keys as
            defense-in-depth. Neither `handle_message` nor the stores change — they
            already take `settings`. See §7, §A.2."""
            base = settings_factory()
            if base.deployment_mode != "multi_tenant":
                if not self._authorized(base):
                    raise Unauthorized("bad or missing bearer token")
                return DEFAULT_UID, base, sessions, ""
            uid = resolve_uid(self._bearer(), body, base, UserRegistry(base.data_dir))
            us = Settings.for_user(uid)
            store = SessionStore(us.data_dir, keep=us.serve_session_turns,
                                 context_hours=us.chat_history_max_age_hours,
                                 retention_days=us.chat_history_retention_days)
            return uid, us, store, f"{uid}:"

        def _body(self) -> dict:
            """Read and JSON-parse the request body as a dict, capped at
            `_MAX_BODY` to bound memory; `{}` for an empty body and a
            non-object payload. Malformed JSON raises `ValueError` for the
            caller to turn into a 400."""
            length = min(int(self.headers.get("Content-Length") or 0), _MAX_BODY)
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}

        def do_GET(self):
            """Route GETs: `/healthz` (the only unauthenticated route) and, behind
            per-user resolution, `/status` → that user's run-status; anything else
            is a 404. In `multi_tenant`, `/status` needs `account_id` in the query
            string (a GET has no body) or it's a 401 — no route bypasses
            `resolve_uid` except `/healthz` (§A.2)."""
            if urlsplit(self.path).path == "/healthz":
                return self._send(200, {"ok": True})
            try:
                _uid, settings, _store, _pfx = self._resolve(self._query())
            except Unauthorized as exc:
                return self._send(401, {"error": str(exc)})
            if urlsplit(self.path).path == "/status":
                return self._send(200, {"status": run_action("run_status", {}, settings)})
            return self._send(404, {"error": f"no route {self.path}"})

        def do_POST(self):
            """Route authenticated POSTs: `/chat` (run one turn with rolling
            history and record it), `/run` (trigger a pipeline run, optionally
            resuming), and `/actions/<name>` (dispatch a named action, mapping
            KeyError→404 and ValueError→400). Every route first resolves the
            request to one user (`_resolve` — mandatory bridge token in
            `multi_tenant`); the resolved per-user `settings`/`store` flow into the
            handler unchanged. Any unhandled exception becomes a JSON 500 so a
            handler bug never hangs the client."""
            try:
                body = self._body()
            except ValueError:
                return self._send(400, {"error": "body is not valid JSON"})
            try:
                _uid, settings, store, prefix = self._resolve(body)
            except Unauthorized as exc:
                return self._send(401, {"error": str(exc)})

            try:
                if self.path == "/chat":
                    text = str(body.get("text", "")).strip()
                    images = _staged_images(body, settings)
                    if not text and not images:
                        return self._send(400, {"error": "missing 'text'"})
                    skey = prefix + str(body.get("session", "") or "default")
                    reply = handle_message(text, settings, make_llm(settings),
                                           history=store.history(skey),
                                           image_paths=images or None)
                    noted = text + (f" [{len(images)} image(s) attached]" if images else "")
                    store.append(skey, noted.strip(), reply)
                    return self._send(200, {"reply": reply})

                if self.path == "/run":
                    params = {"resume": True} if body.get("resume") else {}
                    return self._send(200,
                                      {"result": run_action("trigger_run", params, settings)})

                if self.path.startswith("/actions/"):
                    name = self.path.removeprefix("/actions/")
                    try:
                        result = run_action(name, body, settings)
                    except KeyError:
                        return self._send(404, {"error": f"unknown action {name!r}"})
                    except ValueError as exc:
                        return self._send(400, {"error": str(exc)})
                    return self._send(200, {"result": result})

                return self._send(404, {"error": f"no route {self.path}"})
            except Exception as exc:  # any handler bug → JSON 500, not a hang
                log.exception("serve: %s failed", self.path)
                return self._send(500, {"error": str(exc)})

    server = ThreadingHTTPServer(("127.0.0.1", boot.serve_port if port is None else port),
                                 Handler)
    server.sessions = sessions  # test seam
    return server


def _staged_images(body: dict, settings: Settings) -> list[str]:
    """Image attachments from a /chat body, as verified local paths.

    Two forms, capped at `vision_max_images` total: `image_paths` (existing
    local files — the OpenClaw bridge passes the gateway's staged media
    straight through; trusted because the socket is loopback + bearer-token)
    and `images` (`[{media_type, data(b64)}, …]`), which are decoded into
    `DATA_DIR/media/` for the vision chain. Bad entries are skipped, never
    fatal.

    In `multi_tenant`, caller-supplied `image_paths` are **refused** — a
    filesystem path from the network is a traversal / cross-user-reference
    vector (§A.4); such deployments send image **bytes** only. `settings` is
    already the resolved user's, so decoded media lands under that user's
    `data_dir/media/`."""
    from .vision import media_type_for

    out: list[str] = []
    if settings.deployment_mode != "multi_tenant":
        for p in body.get("image_paths") or []:
            path = Path(str(p))
            if path.is_file() and media_type_for(path):
                out.append(str(path))
    suffix_of = {"image/png": ".png", "image/jpeg": ".jpg",
                 "image/gif": ".gif", "image/webp": ".webp"}
    for img in body.get("images") or []:
        if not isinstance(img, dict):
            continue
        suffix = suffix_of.get(str(img.get("media_type", "")))
        if not suffix:
            continue
        try:
            data = base64.b64decode(str(img.get("data", "")), validate=True)
        except Exception:
            continue
        # Defense in depth (§A.4): the bridge already caps per-image size, but in
        # multi_tenant the daemon must not rely on the caller — oversized blobs
        # are dropped here too. single_user keeps today's behavior (bounded by
        # _MAX_BODY anyway).
        if settings.deployment_mode == "multi_tenant" and len(data) > _MAX_IMAGE_BYTES:
            continue
        media_dir = settings.data_dir / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        path = media_dir / (
            f"chat-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-"
            f"{hashlib.sha1(data).hexdigest()[:8]}{suffix}")
        path.write_bytes(data)
        out.append(str(path))
    return out[:settings.vision_max_images]


def _tick_user_email(user: Settings, make_llm, polled: set) -> None:
    """One user's mailbox poll (§11.7): the user's **own IMAP creds are the
    identity** — the poller already runs as `Settings.for_user(uid)`, so no
    /chat auth is involved and the watermark (`chat_state.json`), media, and
    session history all live in that user's own data dir.

    `polled` dedupes mailboxes within a cycle: if two users configure the same
    inbox, only the first polls it — otherwise one inbound mail would be
    answered twice (the registry's unique email binding is the admin-time guard;
    this is the runtime one)."""
    from .chat.email_channel import EmailChannel
    from .chat.service import _owner_addresses

    email = EmailChannel(user, _owner_addresses(user))
    if not email.enabled:
        return
    box = str(user.smtp_user).strip().lower()
    if box in polled:
        log.warning("mailbox %s also configured by %s — skipping duplicate poller",
                    box, user.uid)
        return
    polled.add(box)
    store = SessionStore(user.data_dir, keep=user.serve_session_turns,
                         context_hours=user.chat_history_max_age_hours,
                         retention_days=user.chat_history_retention_days)
    try:
        messages = email.poll()
    except Exception as exc:
        log.warning("email poll failed for %s: %s", user.uid, exc)
        return
    for message in messages:
        log.info("email message for %s from %s: %.80s", user.uid,
                 message.get("sender", "?"), message["text"])
        try:
            skey = f"{user.uid}:email:{message.get('sender', '')}"
            reply = handle_message(message["text"], user, make_llm(user),
                                   history=store.history(skey),
                                   image_paths=message.get("images"))
            store.append(skey, message["text"], reply)
            email.send(reply, in_reply_to=message)
            log.info("replied via email for %s (%d chars)", user.uid, len(reply))
        except Exception:
            log.exception("failed to answer email for %s", user.uid)


def _tick_tenants(settings: Settings, now: "datetime | None" = None,
                  llm_factory=None) -> None:
    """One multi-tenant proactive cycle: per-user **email polling** (§11.7),
    reminders + routines, and the daily-run fan-out (§12).

    `settings` is the deployment-root Settings. For each **active** user this
    builds `Settings.for_user(uid)` so everything reads and sends from *that
    user's* data dir and credentials — the root data dir has none of it, which
    is exactly the bug this fixes (root-scoped ticking meant tenant reminders
    silently never fired). Email is per-user only: WeCom is out of scope in
    multi_tenant (§1), so this never builds the WeCom channel or its callback
    server. From `daily_run_hour` onward it also calls `enqueue_daily_runs` —
    idempotent per (uid, day) via the queue's dedupe key, so ticking every poll
    cycle can't double-run anyone. One user's failure never blocks another's."""
    from .registry import UserRegistry

    make_llm = llm_factory or (lambda s: LLM(s))
    now = now or datetime.now()
    if now.hour >= settings.daily_run_hour:
        try:
            from .scheduler import enqueue_daily_runs

            enqueue_daily_runs(settings, day=now.strftime("%Y-%m-%d"))
        except Exception:
            log.exception("daily fan-out failed")
    # weekly self-evolution set (§12b): per-user consolidate + evolve, plus the
    # global evolve + self-improve jobs — idempotent per ISO week via dedupe keys
    if now.weekday() == settings.weekly_day and now.hour >= settings.weekly_hour:
        try:
            from .scheduler import enqueue_weekly_jobs

            enqueue_weekly_jobs(settings, week=now.strftime("%G-W%V"))
        except Exception:
            log.exception("weekly fan-out failed")
    polled_mailboxes: set = set()
    for uid in UserRegistry(settings.data_dir).active():
        try:
            user = Settings.for_user(uid)
        except Exception:
            log.exception("tick: Settings.for_user(%s) failed", uid)
            continue
        try:  # per-user mailbox poller — the user's creds are the identity
            _tick_user_email(user, make_llm, polled_mailboxes)
        except Exception:
            log.exception("email tick failed for %s", uid)
        try:  # proactive path: due reminders go out with no inbound command
            from .notify import ReminderStore

            for r in ReminderStore(user.data_dir).deliver_due(user):
                log.info("reminder %s (%s) delivered: %.60s", r["id"], uid, r["message"])
        except Exception:
            log.exception("reminder delivery failed for %s", uid)
        try:  # conditional routines (workdays / weather gates / …)
            from .routines import fire_due

            fire_due(user)
        except Exception:
            log.exception("routine firing failed for %s", uid)


def _chat_poll_loop(settings_factory, sessions: SessionStore,
                    stop: threading.Event, llm_factory=None) -> None:
    """Email (+WeCom) chat polling, absorbed from the standalone listener.
    Everything is rebuilt each cycle so `.env` edits apply within one poll.

    In `multi_tenant` the shared-inbox poll is replaced by `_tick_tenants`:
    **per-user mailbox pollers** (§11.7 — each user's own IMAP creds are the
    identity, state/sessions in their own data dir), per-user reminders and
    routines, and the daily-run fan-out. The root `.env`'s inbox is nobody's
    tenant and is never polled in that mode; WeCom is out of scope there."""
    from .chat.service import build_channels

    make_llm = llm_factory or (lambda s: LLM(s))
    first = True
    while not stop.is_set():
        try:
            settings = settings_factory()
            if settings.deployment_mode == "multi_tenant":
                if first:
                    log.info("multi_tenant: ticking per-user email/reminders/"
                             "routines/daily-runs for active users")
                    first = False
                _tick_tenants(settings, llm_factory=make_llm)
                stop.wait(settings.chat_poll_seconds)
                continue
            channels = build_channels(settings, log_wecom=first)
            first = False
            for channel in channels:
                try:
                    messages = channel.poll()
                except Exception as exc:
                    log.warning("%s poll failed: %s", channel.name, exc)
                    continue
                for message in messages:
                    log.info("%s message from %s: %.80s", channel.name,
                             message.get("sender", "?"), message["text"])
                    try:
                        session = f"{channel.name}:{message.get('sender', '')}"
                        reply = handle_message(message["text"], settings,
                                               make_llm(settings),
                                               history=sessions.history(session),
                                               image_paths=message.get("images"))
                        sessions.append(session, message["text"], reply)
                        channel.send(reply, in_reply_to=message)
                        log.info("replied via %s (%d chars)", channel.name, len(reply))
                    except Exception:
                        log.exception("failed to answer %s message", channel.name)
            try:  # proactive path: due reminders go out with no inbound command
                from .notify import ReminderStore

                for r in ReminderStore(settings.data_dir).deliver_due(settings):
                    log.info("reminder %s delivered: %.60s", r["id"], r["message"])
            except Exception:
                log.exception("reminder delivery failed")
            try:  # conditional routines (workdays / weather gates / …)
                from .routines import fire_due

                fire_due(settings)
            except Exception:
                log.exception("routine firing failed")
            stop.wait(settings.chat_poll_seconds)
        except Exception:  # the poll thread must never die
            log.exception("chat poll cycle failed")
            stop.wait(60)


def run_serve(settings: Settings) -> int:
    """`assistant serve`: the long-lived daemon. Takes the shared inbox pid
    lock (so it can never race the standalone chat-listener on the watermark),
    starts the HTTP server plus the background chat-poll thread, wires
    SIGTERM/SIGINT to a clean shutdown, and blocks in `serve_forever`. Returns
    1 if the pid lock is already held, else 0 after shutdown."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from .chat.service import _acquire_pid_lock

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    if not _acquire_pid_lock(settings):  # same lock as chat-listen: one inbox reader
        return 1

    server = make_server()
    stop = threading.Event()
    poller = threading.Thread(
        target=_chat_poll_loop,
        args=(Settings, server.sessions, stop),
        name="chat-poll", daemon=True)
    poller.start()

    # multi_tenant background jobs run on the durable in-process queue instead of
    # detached CLIs (§6); the pool recovers orphaned jobs on start. single_user
    # keeps the legacy detached-Popen path, so no pool is needed there.
    pool = None
    if settings.deployment_mode == "multi_tenant":
        from .jobs import JobQueue
        from .worker import WorkerPool

        pool = WorkerPool(JobQueue(settings.shared_dir),
                          max_workers=settings.job_workers).start()
        log.info("serve: job worker pool started (%d workers)", settings.job_workers)

    def _shutdown(signum, frame):
        """Signal handler: log the signal and trigger a graceful shutdown."""
        log.info("serve: signal %d — shutting down", signum)
        stop.set()
        if pool is not None:
            pool.stop()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    log.info("serve: listening on 127.0.0.1:%d (chat poll every %ds)",
             server.server_address[1], settings.chat_poll_seconds)
    server.serve_forever()
    stop.set()
    if pool is not None:
        pool.stop()
    return 0


def _pid_alive(pid: int | None) -> bool:
    """Is `pid` a live process? (`os.kill(pid, 0)` — PermissionError means it
    exists but isn't ours, still alive.)"""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _healthz(settings: Settings) -> bool:
    """True if a serve daemon answers /healthz on the loopback port."""
    import httpx

    try:
        r = httpx.get(f"http://127.0.0.1:{settings.serve_port}/healthz", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def reboot(settings: Settings, delay: float = 0.0, timeout: float = 20.0,
           stop_wait: float = 8.0) -> dict:
    """One-command restart of the serve daemon so it reloads code.

    A running daemon holds its imported Python modules in memory — only a
    restart picks up a code change (the per-request Settings/LLM rebuild reloads
    `.env`, not code). This stops the current daemon (via the `chat_listener.pid`
    lock), then starts a fresh detached one and waits for `/healthz`. If some
    supervisor also respawns it, the shared pid lock lets only one survive (the
    loser exits), so a duplicate can't stick.

    `delay` (used by the chat `reboot` action) sleeps first so the reply flushes
    before the daemon goes down. Never signals its own pid, so it is safe to run
    detached from inside the daemon. Returns `{status, pid}`.

    multi_tenant (`assistant admin reboot`): reboot is **requeue**, not drain —
    SIGTERM makes `_shutdown` stop the worker pool (bounded join); any job still
    `running` when the process exits is requeued by `recover()` on the next
    start, and the per-job side-effect guards (delivery ledger) keep the replay
    from double-sending (§6)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if delay > 0:
        time.sleep(delay)
    pid_file = settings.data_dir / "chat_listener.pid"

    def _pid() -> int | None:
        try:
            return int(pid_file.read_text().strip())
        except (OSError, ValueError):
            return None

    old, me = _pid(), os.getpid()
    if old and old != me and _pid_alive(old):
        log.info("reboot: stopping daemon pid %d", old)
        try:
            os.kill(old, signal.SIGTERM)
            deadline = time.time() + stop_wait
            while _pid_alive(old) and time.time() < deadline:
                time.sleep(0.3)
            if _pid_alive(old):
                log.warning("reboot: pid %d didn't stop — SIGKILL", old)
                os.kill(old, signal.SIGKILL)
                time.sleep(1.0)
        except ProcessLookupError:
            pass

    # start a fresh detached daemon; it loads the current code. (The pid lock
    # makes this safe even if a supervisor also respawns one — only one keeps it.)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    log_file = (settings.data_dir / "serve.log").open("a")
    subprocess.Popen([sys.executable, "-m", "assistant.cli", "serve"],
                     stdout=log_file, stderr=subprocess.STDOUT, start_new_session=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _healthz(settings):
            log.info("reboot: daemon healthy (pid %s)", _pid())
            return {"status": "rebooted", "pid": _pid()}
        time.sleep(0.5)
    return {"status": "failed", "note": "daemon did not come back healthy in time"}
