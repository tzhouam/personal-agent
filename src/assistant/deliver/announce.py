"""Best-effort WeChat announce, sent right after the digest email delivers.

Shells out to the OpenClaw gateway's CLI (`openclaw message send`) — the same
invocation the command-cron's --announce flag uses, so enabling this makes
manual and chat-triggered runs announce too. OFF by default: turn on
WECHAT_ANNOUNCE only after dropping --announce from the cron job, or the
07:00 run pings WeChat twice.

Never raises — a failed announce is a log line, not a pipeline error.
"""

import logging
import os
import subprocess
from pathlib import Path

from ..config import Settings

log = logging.getLogger("assistant")


def announce_digest(settings: Settings, text: str) -> str:
    """Send ``text`` to the owner's WeChat. Returns a one-word-ish status
    note ("sent" / "disabled" / "failed: …") for the run log."""
    if not settings.wechat_announce:
        return "disabled"
    if not (settings.announce_account and settings.announce_to):
        return "disabled (set ANNOUNCE_ACCOUNT and ANNOUNCE_TO)"
    # the openclaw shim resolves `node` from PATH and needs Node >=22 — put
    # its own directory (e.g. /opt/node24/bin) first so the pipeline env works
    env = {**os.environ,
           "PATH": f"{Path(settings.openclaw_bin).parent}:{os.environ.get('PATH', '')}"}
    try:
        proc = subprocess.run(
            [settings.openclaw_bin, "message", "send",
             "--channel", settings.announce_channel,
             "--account", settings.announce_account,
             "--target", settings.announce_to,
             "-m", text[:1000]],
            capture_output=True, text=True, timeout=90, env=env)
    except Exception as exc:
        return f"failed: {exc}"
    if proc.returncode == 0:
        return "sent"
    detail = (proc.stderr.strip() or proc.stdout.strip())[:200]
    return f"failed: rc={proc.returncode} {detail}"
