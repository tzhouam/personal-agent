"""Best-effort WeChat announce, sent right after the digest email delivers.

Thin gate over notify.send_wechat (the general agent-initiated WeChat path):
this wrapper only adds the WECHAT_ANNOUNCE on/off switch so the daily
pipeline stays quiet unless the owner opted in. Never raises — a failed
announce is a log line, not a pipeline error.
"""

from ..config import Settings
from ..notify import send_wechat


def announce_digest(settings: Settings, text: str) -> str:
    """Send ``text`` to the owner's WeChat. Returns a one-word-ish status
    note ("sent" / "disabled" / "failed: …") for the run log."""
    if not settings.wechat_announce:
        return "disabled"
    return send_wechat(settings, text)
