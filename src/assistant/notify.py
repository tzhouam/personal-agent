"""Agent-initiated WeChat messages + scheduled reminders.

``send_wechat`` pushes a message to the owner through the OpenClaw gateway —
no inbound command required. The deliver-phase announce and the reminder
scheduler both ride on it. Requires the announce settings in .env
(WECHAT_ANNOUNCE account/target); returns a status string, never raises.

``ReminderStore`` holds one-shot reminders (``~/.personal-agent/
reminders.yaml``). The serve daemon's poll loop calls ``deliver_due`` every
cycle (~60s), so a reminder set from chat ("remind me in 2h to …") arrives
as a proactive WeChat message with no further owner action.
"""

import logging
import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from .config import Settings

log = logging.getLogger("assistant")


def send_wechat(settings: Settings, text: str) -> str:
    """Send ``text`` to the owner's WeChat. Returns "sent" / "disabled" /
    "failed: …" — never raises."""
    if not (settings.announce_account and settings.announce_to):
        return "disabled (set ANNOUNCE_ACCOUNT and ANNOUNCE_TO)"
    # the openclaw shim resolves `node` from PATH and needs Node >=22 — put
    # its own directory (e.g. /opt/node24/bin) first so any calling env works
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


# ── one-shot reminders ───────────────────────────────────────────────

_RELATIVE = re.compile(r"^\+?(\d+)\s*(m|min|minutes?|h|hours?|d|days?)$", re.IGNORECASE)


def parse_when(when: str, now: datetime | None = None) -> datetime | None:
    """'+30m' / '+2h' / '+1d', 'HH:MM' (today, or tomorrow if past),
    'YYYY-MM-DD HH:MM' — None if unparseable."""
    now = now or datetime.now()
    when = str(when).strip()
    match = _RELATIVE.match(when)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)[0].lower()
        return now + timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[unit]: amount})
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(when, fmt)
        except ValueError:
            pass
    try:
        at = datetime.strptime(when, "%H:%M").replace(
            year=now.year, month=now.month, day=now.day)
        return at if at > now else at + timedelta(days=1)
    except ValueError:
        return None


class ReminderStore:
    """One-shot reminders persisted to reminders.yaml. Each reminder carries a
    monotonic id, a due time, and a ``sent_at`` marker that flips once delivered
    (or "cancelled") so it fires at most once."""

    def __init__(self, data_dir: Path):
        """Bind the store to ``data_dir/reminders.yaml`` (created lazily)."""
        self.path = data_dir / "reminders.yaml"

    def _load(self) -> dict:
        """Read the reminders file, returning a fresh empty structure when it's
        missing or empty."""
        if not self.path.exists():
            return {"next_id": 1, "reminders": []}
        return yaml.safe_load(self.path.read_text()) or {"next_id": 1, "reminders": []}

    def _save(self, data: dict) -> None:
        """Write the reminders structure back, creating the data dir if needed."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))

    def add(self, message: str, due_at: datetime) -> dict:
        """Append a new unsent reminder due at ``due_at`` (message capped at 500
        chars), assign it the next id, persist, and return the stored record."""
        data = self._load()
        reminder = {"id": f"m{data['next_id']}", "message": message[:500],
                    "due_at": due_at.strftime("%Y-%m-%d %H:%M"), "sent_at": None}
        data["next_id"] += 1
        data["reminders"].append(reminder)
        self._save(data)
        return reminder

    def pending(self) -> list[dict]:
        """Reminders not yet sent or cancelled."""
        return [r for r in self._load()["reminders"] if not r.get("sent_at")]

    def cancel(self, reminder_id: str) -> bool:
        """Mark the pending reminder ``reminder_id`` as cancelled so it never
        fires. True if one was cancelled, False if unknown or already sent."""
        data = self._load()
        for r in data["reminders"]:
            if r["id"] == reminder_id and not r.get("sent_at"):
                r["sent_at"] = "cancelled"
                self._save(data)
                return True
        return False

    def deliver_due(self, settings: Settings, now: datetime | None = None,
                    send=send_wechat) -> list[dict]:
        """Send every due, unsent reminder; mark sent only on success so a
        gateway hiccup retries next cycle. Returns what was delivered."""
        now = now or datetime.now()
        data = self._load()
        delivered = []
        for r in data["reminders"]:
            if r.get("sent_at") or r["due_at"] > now.strftime("%Y-%m-%d %H:%M"):
                continue
            status = send(settings, f"⏰ Reminder: {r['message']}")
            if status == "sent":
                r["sent_at"] = now.strftime("%Y-%m-%d %H:%M")
                delivered.append(r)
            else:
                log.warning("reminder %s delivery failed: %s", r["id"], status)
        if delivered:
            self._save(data)
        return delivered
