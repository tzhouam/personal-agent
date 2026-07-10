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

import hashlib
import json
import logging
import signal
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .actions import run_action
from .chat.agent import handle_message
from .config import Settings
from .llm import LLM

log = logging.getLogger("assistant")

_MAX_BODY = 64 * 1024


class SessionStore:
    """Rolling chat history per session id, spilled to disk so multi-turn
    context survives a daemon restart.

    Turns expire after ``max_age_hours`` (default 48): expired turns never
    enter a prompt (read-time filter — the context-window budget), and the
    daily curate phase calls ``prune()`` to delete them from disk."""

    def __init__(self, data_dir: Path, keep: int = 10, max_age_hours: int = 48):
        """Store under `data_dir/sessions`, retaining at most `keep` turns per
        session on disk and treating turns older than `max_age_hours` as
        expired."""
        self.dir = data_dir / "sessions"
        self.keep = keep
        self.max_age_hours = max_age_hours

    def _path(self, session_id: str) -> Path:
        """Map a session id to its spill file via a short sha1 hash, so
        arbitrary channel:peer ids become safe fixed-length filenames."""
        return self.dir / (hashlib.sha1(session_id.encode()).hexdigest()[:16] + ".json")

    def _fresh(self, turns: list[dict]) -> list[dict]:
        """Keep only turns newer than the `max_age_hours` cutoff; turns lacking
        a `ts` predate the expiry feature and are dropped as expired."""
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(hours=self.max_age_hours)).isoformat()
        # turns without a ts predate the expiry feature — treat as expired
        return [t for t in turns if t.get("ts", "") >= cutoff]

    def history(self, session_id: str) -> list[dict]:
        """Load the session's non-expired turns for prompt context, returning
        `[]` when the file is absent or corrupt — history is best-effort, a bad
        spill must never break a reply (degrade, never crash)."""
        path = self._path(session_id)
        if not path.exists():
            return []
        try:
            return self._fresh(json.loads(path.read_text()).get("turns", []))
        except ValueError:
            return []

    def append(self, session_id: str, owner: str, assistant: str) -> None:
        """Record one owner/assistant exchange, capping each side at 2000 chars
        and writing back only the newest `keep` turns so the file stays
        bounded."""
        turns = self.history(session_id)
        turns.append({"ts": datetime.now(timezone.utc).isoformat(),
                      "owner": owner[:2000], "assistant": assistant[:2000]})
        self.dir.mkdir(parents=True, exist_ok=True)
        self._path(session_id).write_text(json.dumps(
            {"session": session_id, "turns": turns[-self.keep:]},
            ensure_ascii=False))

    def prune(self) -> dict:
        """Drop expired turns from disk; delete session files left empty.
        Returns counts for the curate log/metrics."""
        removed_turns = removed_files = 0
        for path in self.dir.glob("*.json") if self.dir.exists() else []:
            try:
                data = json.loads(path.read_text())
            except ValueError:
                path.unlink(missing_ok=True)
                removed_files += 1
                continue
            turns = data.get("turns", [])
            fresh = self._fresh(turns)
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
                            max_age_hours=boot.chat_history_max_age_hours)
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
            configured, else require an exact `Authorization: Bearer <token>`."""
            if not settings.serve_token:
                return True
            header = self.headers.get("Authorization", "")
            return header == f"Bearer {settings.serve_token}"

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
            """Route GETs: `/healthz` (always open) and, behind auth,
            `/status` → the run-status action; anything else is a 404."""
            settings = settings_factory()
            if self.path == "/healthz":
                return self._send(200, {"ok": True})
            if not self._authorized(settings):
                return self._send(401, {"error": "bad or missing bearer token"})
            if self.path == "/status":
                return self._send(200, {"status": run_action("run_status", {}, settings)})
            return self._send(404, {"error": f"no route {self.path}"})

        def do_POST(self):
            """Route authenticated POSTs: `/chat` (run one turn with rolling
            history and record it), `/run` (trigger a pipeline run, optionally
            resuming), and `/actions/<name>` (dispatch a named action, mapping
            KeyError→404 and ValueError→400). Any unhandled exception becomes a
            JSON 500 so a handler bug never hangs the client."""
            settings = settings_factory()
            if not self._authorized(settings):
                return self._send(401, {"error": "bad or missing bearer token"})
            try:
                body = self._body()
            except ValueError:
                return self._send(400, {"error": "body is not valid JSON"})

            try:
                if self.path == "/chat":
                    text = str(body.get("text", "")).strip()
                    if not text:
                        return self._send(400, {"error": "missing 'text'"})
                    session = str(body.get("session", "") or "default")
                    reply = handle_message(text, settings, make_llm(settings),
                                           history=sessions.history(session))
                    sessions.append(session, text, reply)
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


def _chat_poll_loop(settings_factory, sessions: SessionStore,
                    stop: threading.Event, llm_factory=None) -> None:
    """Email (+WeCom) chat polling, absorbed from the standalone listener.
    Everything is rebuilt each cycle so `.env` edits apply within one poll."""
    from .chat.service import build_channels

    make_llm = llm_factory or (lambda s: LLM(s))
    first = True
    while not stop.is_set():
        try:
            settings = settings_factory()
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
                                               history=sessions.history(session))
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

    def _shutdown(signum, frame):
        """Signal handler: log the signal and trigger a graceful shutdown."""
        log.info("serve: signal %d — shutting down", signum)
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    log.info("serve: listening on 127.0.0.1:%d (chat poll every %ds)",
             server.server_address[1], settings.chat_poll_seconds)
    server.serve_forever()
    stop.set()
    return 0
