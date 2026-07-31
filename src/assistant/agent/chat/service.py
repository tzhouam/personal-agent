"""Chat listener daemon: poll channels for owner messages, answer each one.

Run with `assistant chat-listen` (foreground; nohup it for background use).
A pid file prevents two listeners racing on the same inbox watermark.
"""

import contextvars
import logging
import os
import time

from assistant.platform.config import Settings
from assistant.platform.llm import LLM
from assistant.agent.profile_store import ProfileStore
from assistant.agent.chat.agent import handle_message
from assistant.agent.chat.email_channel import EmailChannel
from assistant.agent.chat.wecom import WeComChannel, release_callback_server

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
        else:
            # Desired-state sync: when THIS cycle's config no longer enables
            # receiving, any server bound by an earlier cycle must close —
            # otherwise deleting WECOM_TOKEN from .env leaves a listener
            # serving with stale credentials forever. A bind FAILURE with
            # receive still configured is not a release (retry next cycle).
            if not (settings.wecom_token and settings.wecom_aes_key):
                release_callback_server()
            if log_wecom:
                log.info("wecom: send-only (set WECOM_TOKEN/WECOM_AES_KEY + public "
                         "callback URL to receive)")
    else:
        release_callback_server()  # wecom disabled entirely this cycle
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


# True while the standalone listener is the host: scheduling actions
# (set_reminder/create_routine) refuse under it — the listener runs no
# delivery tick, so accepting them silently created reminders that would
# NEVER fire (audit F15). A contextvar, not global state: explicit,
# test-resettable, and scoped to the dispatching thread.
_listener_active: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "chat_listener_active", default=False)


def listener_active() -> bool:
    """Is the current execution hosted by `assistant chat-listen`?"""
    return _listener_active.get()


def run_listener(settings: Settings, once: bool = False) -> int:
    """DEPRECATED surface (prefer `assistant serve` — same channels plus
    reminders/routines/HTTP): poll every enabled channel, answering each
    owner message via the chat agent on the same channel. Scheduling actions
    refuse under this host (no delivery tick here — accepting them created
    reminders that never fired), and Settings/LLM/channels are rebuilt every
    sweep so a `.env` edit takes effect without a restart (the listener used
    to pin boot-time credentials forever). ``once`` runs a single sweep with
    the GIVEN settings (and skips the pid lock) for testing. Returns nonzero
    when it can't start."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log.warning("chat-listen is deprecated — `assistant serve` runs the same "
                "channels plus reminders, routines, and the HTTP API")
    if not once and not _acquire_pid_lock(settings):
        return 1
    if not build_channels(settings, log_wecom=True):
        log.error("no chat channel configured (need SMTP creds or WeCom app)")
        return 1

    log.info("chat listener started — poll every %ds", settings.chat_poll_seconds)
    _cap_token = _listener_active.set(True)
    try:
        while True:
            if not once:  # hot-reload: fresh credentials/config each sweep —
                # but process IDENTITY stays pinned: the pid lock and stores
                # live under the boot data_dir/uid, and a mid-flight .env
                # DATA_DIR edit must not silently split state
                fresh = Settings()
                fresh.data_dir = settings.data_dir
                fresh.uid = settings.uid
                settings = fresh
            llm = LLM(settings)
            for channel in build_channels(settings, log_wecom=False):
                try:
                    messages = channel.poll()
                except Exception as exc:
                    log.warning("%s poll failed: %s", channel.name, exc)
                    continue
                for message in messages:
                    if message.get("kind") == "unsupported_media":
                        try:  # deterministic fixed reply, no LLM (audit F8)
                            channel.send("这个渠道暂时收不到图片，请用微信或"
                                         "邮件发图片 🙏", in_reply_to=message)
                        except Exception:
                            log.exception("unsupported-media ack failed")
                        continue
                    if message.get("kind") == "email_outbox_retry":
                        try:   # Track D: retry the persisted reply only
                            channel.send(message["reply"], in_reply_to=message)
                            channel.ack(message)
                        except Exception as exc:
                            channel.send_failed(message, str(exc))
                            break
                        continue
                    log.info("%s message from %s: %.80s", channel.name,
                             message.get("sender", "?"), message["text"])
                    token = (channel.begin_turn(message)
                             if "uid" in message else None)
                    if "uid" in message and token is None:
                        continue
                    try:
                        reply = handle_message(
                            message["text"], settings, llm,
                            image_paths=message.get("images"),
                            rejected_images=message.get("rejected_images"))
                    except Exception as exc:
                        log.exception("failed to answer %s message", channel.name)
                        if token:
                            channel.turn_failed(message, token, str(exc))
                            break
                        continue
                    if token and not channel.finish_turn(message, token,
                                                         reply, []):
                        continue   # fenced out: the row settled elsewhere
                    try:
                        channel.send(reply, in_reply_to=message)
                        if token:
                            channel.ack(message)
                        log.info("replied via %s (%d chars)",
                                 channel.name, len(reply))
                    except Exception as exc:
                        log.exception("failed to answer %s message", channel.name)
                        if token:
                            channel.send_failed(message, str(exc))
                            break
            if once:
                return 0
            time.sleep(settings.chat_poll_seconds)
    finally:
        _listener_active.reset(_cap_token)
