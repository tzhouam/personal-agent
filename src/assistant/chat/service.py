"""Chat listener daemon: poll channels for owner messages, answer each one.

Run with `assistant chat-listen` (foreground; nohup it for background use).
A pid file prevents two listeners racing on the same inbox watermark.
"""

import logging
import os
import time

from ..config import Settings
from ..llm import LLM
from ..profile_store import ProfileStore
from .agent import handle_message
from .email_channel import EmailChannel
from .wecom import WeComChannel

log = logging.getLogger("assistant")


def _owner_addresses(settings: Settings) -> list[str]:
    """Every address that counts as the owner for sender authentication: the
    SMTP user and digest recipient, plus any emails recorded in the profile."""
    addresses = [settings.smtp_user, settings.digest_to]
    store = ProfileStore(settings.profile_dir)
    if store.exists():
        addresses += store.load().get("identity", {}).get("emails", [])
    return addresses


def build_channels(settings: Settings, log_wecom: bool = True) -> list:
    """All enabled inbound chat channels — shared by the standalone listener
    and the serve daemon's poll thread."""
    channels = []
    email = EmailChannel(settings, _owner_addresses(settings))
    if email.enabled:
        channels.append(email)
    wecom = WeComChannel(settings)
    if wecom.enabled:
        channels.append(wecom)
        if wecom.start_callback_server():
            if log_wecom:
                log.info("wecom: send + receive enabled")
        elif log_wecom:
            log.info("wecom: send-only (set WECOM_TOKEN/WECOM_AES_KEY + public "
                     "callback URL to receive)")
    return channels


def _acquire_pid_lock(settings: Settings) -> bool:
    """Claim the single-listener lock so two daemons don't double-process the
    same inbox. True on success; False if a live listener already holds the pid
    file — a stale pid (process gone) is overwritten and the lock granted."""
    pid_file = settings.data_dir / "chat_listener.pid"
    if pid_file.exists():
        try:
            other = int(pid_file.read_text().strip())
            os.kill(other, 0)
            log.error("chat listener already running (pid %d)", other)
            return False
        except (ValueError, ProcessLookupError, PermissionError):
            pass  # stale pid file
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))
    return True


def run_listener(settings: Settings, once: bool = False) -> int:
    """Poll every enabled channel forever, answering each owner message via the
    chat agent and replying on the same channel. ``once`` runs a single sweep
    (and skips the pid lock) for testing. Returns a nonzero exit code when it
    can't start — lock already held, or no channel configured. Per-channel and
    per-message errors are logged and swallowed so one failure never stops the
    loop."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not once and not _acquire_pid_lock(settings):
        return 1

    llm = LLM(settings)
    channels = build_channels(settings)
    if not channels:
        log.error("no chat channel configured (need SMTP creds or WeCom app)")
        return 1

    log.info("chat listener started — channels: %s, poll every %ds",
             ", ".join(c.name for c in channels), settings.chat_poll_seconds)
    while True:
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
                    reply = handle_message(message["text"], settings, llm)
                    channel.send(reply, in_reply_to=message)
                    log.info("replied via %s (%d chars)", channel.name, len(reply))
                except Exception:
                    log.exception("failed to answer %s message", channel.name)
        if once:
            return 0
        time.sleep(settings.chat_poll_seconds)
