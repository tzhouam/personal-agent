"""Always-on WeChat login-QR refresher (daemon-owned).

`openclaw channels login` renders a QR, auto-refreshes it at most 3 times, then
gives up (`二维码多次失效，连接流程已停止`) — a total window of only ~3–4 minutes
per invocation, so an invitee who is slow to scan misses it. This background
thread (started by `run_serve` when `login_qr_refresh` is on) keeps one fresh:
it relaunches the login process whenever it exits, parses each new QR URL from
its output, renders a PNG in memory, and exposes the current one via
`current()` for the loopback `GET /qr` route.

The QR is a **join credential** — anyone who scans it joins the gateway — so it
is held in memory only and NEVER written under a repo (see `.gitignore`
backstop); the route that serves it is serve-token-gated. Being always-on it
also exercises the unverified A.8 multi-account path continuously; the operator
opts in via the config flag.
"""

import io
import logging
import re
import subprocess
import threading
from datetime import datetime, timezone

log = logging.getLogger("assistant")

# The login flow prints (and offers as a fallback link) a liteapp login URL that
# the QR encodes; we re-render our own PNG from it each time it changes.
_URL_RE = re.compile(r"https://liteapp\.weixin\.qq\.com/\S+")

# Give up on a single stalled login attempt so a wedged child never pins the
# thread forever — the loop just relaunches.
_ATTEMPT_TIMEOUT_S = 300


def render_qr_png(url: str) -> bytes:
    """Render `url` as PNG bytes (in memory — no file touches disk)."""
    import qrcode

    buf = io.BytesIO()
    qrcode.make(url).save(buf, format="PNG")
    return buf.getvalue()


class LoginQRRefresher:
    """A daemon thread that keeps one freshly-rendered login QR available.

    `current()` returns the latest `{png, url, ts}` (or None before the first QR
    is seen). `start()`/`stop()` manage the thread and its child process."""

    def __init__(self, settings, channel: str | None = None):
        self._bin = settings.openclaw_bin
        self._channel = channel or settings.announce_channel
        self._lock = threading.Lock()
        self._cur: dict | None = None
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None

    def current(self) -> dict | None:
        """A snapshot of the current QR (`{png, url, ts}`) or None if none yet."""
        with self._lock:
            return dict(self._cur) if self._cur else None

    def start(self) -> "LoginQRRefresher":
        self._thread = threading.Thread(target=self._loop, name="login-qr", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """Signal the loop to stop and terminate any running login child."""
        self._stop.set()
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()

    def _loop(self) -> None:
        """Relaunch the login flow forever (until stopped): each `openclaw
        channels login` invocation gives up after 3 refreshes, so we simply run
        another. Any failure backs off briefly rather than killing the thread."""
        while not self._stop.is_set():
            try:
                self._run_once()
            except FileNotFoundError:
                log.warning("login-qr: openclaw not found at %s — refresher idle", self._bin)
                self._stop.wait(60)   # misconfigured; don't hot-loop
                continue
            except Exception:
                log.exception("login-qr: login attempt failed")
            self._stop.wait(5)        # brief gap before relaunching a fresh attempt

    def _run_once(self) -> None:
        """Run one login attempt, updating the current QR on every URL it emits,
        until the child exits (or we're asked to stop)."""
        proc = subprocess.Popen(
            [self._bin, "channels", "login", "--channel", self._channel],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            bufsize=1, start_new_session=True)
        self._proc = proc
        try:
            for line in proc.stdout:                       # blocks per line
                if self._stop.is_set():
                    proc.terminate()
                    break
                match = _URL_RE.search(line)
                if match:
                    self._update(match.group(0))
            proc.wait(timeout=_ATTEMPT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.terminate()
        finally:
            if proc.poll() is None:
                proc.kill()
            self._proc = None

    def _update(self, url: str) -> None:
        try:
            png = render_qr_png(url)
        except Exception:
            log.exception("login-qr: failed to render QR")
            return
        with self._lock:
            self._cur = {"png": png, "url": url,
                         "ts": datetime.now(timezone.utc).isoformat()}
        log.info("login-qr: refreshed QR (…%s)", url[-14:])
