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

from assistant.platform.config import Settings
from assistant.platform.locks import locked_transaction

log = logging.getLogger("assistant")


def split_message(text: str, max_bytes: int, hard_max_parts: int = 5) -> list[str]:
    """Split ``text`` into UTF-8-byte-bounded parts on paragraph/sentence
    boundaries (audit F10 — the old char-count caps silently truncated long
    replies, and WeCom's limit is BYTES, so a ~700-char Chinese reply
    exceeded 2048B, the API errored, and the whole reply was lost). Part
    markers' own bytes are reserved inside the budget; cuts never split a
    codepoint. Past ``hard_max_parts`` the final part ends with a visible
    truncation marker."""
    marker_reserve = len(f"({hard_max_parts}/{hard_max_parts}) ".encode())
    budget = max(max_bytes - marker_reserve, 64)
    parts: list[str] = []
    remaining = text
    while remaining:
        if len(remaining.encode()) <= budget:
            parts.append(remaining)
            break
        # widest prefix under budget, codepoint-safe
        cut = len(remaining)
        while len(remaining[:cut].encode()) > budget:
            cut -= max(1, (len(remaining[:cut].encode()) - budget) // 4)
        # prefer a paragraph, then sentence-ish, then any boundary
        window = remaining[:cut]
        for sep in ("\n\n", "\n", "。", "！", "？", ". ", "; ", " "):
            pos = window.rfind(sep)
            if pos > cut // 3:
                cut = pos + len(sep)
                break
        parts.append(remaining[:cut])
        remaining = remaining[cut:]
    if len(parts) > hard_max_parts:
        trunc = "…(回复过长已截断)"
        parts = parts[:hard_max_parts]
        last = parts[-1].rstrip()
        # the marker rides INSIDE the budget too — trim the payload to fit
        room = budget - len(trunc.encode())
        while last and len(last.encode()) > room:
            last = last[:-1]
        parts[-1] = last + trunc
    if len(parts) > 1:
        parts = [f"({i + 1}/{len(parts)}) {p}" for i, p in enumerate(parts)]
    return parts


def send_chunked(send_one, text: str, max_bytes: int,
                 hard_max_parts: int = 5) -> dict:
    """Sequentially send ``text`` in byte-bounded parts via ``send_one(part)``
    (which must RAISE on failure). Returns the SendReport contract:
    ``{"parts_sent": delivered_count, "parts_total": produced_count,
    "error": None | str}``. On the first failed part it stops — no
    out-of-order tail; retry ownership stays with the calling surface, and a
    retry duplicating already-sent parts is the accepted tradeoff (loss is
    not)."""
    parts = split_message(text, max_bytes, hard_max_parts)
    sent = 0
    for part in parts:
        try:
            send_one(part)
        except Exception as exc:
            return {"parts_sent": sent, "parts_total": len(parts),
                    "error": f"{exc} (sent {sent}/{len(parts)})"}
        sent += 1
    return {"parts_sent": sent, "parts_total": len(parts), "error": None}


def send_to_conversation(settings: Settings, account_id: str, target: str,
                         text: str) -> str:
    """Send ``text`` into one specific WeChat conversation on one gateway
    account (A.9 outbound routing — used for late replies that outlived the
    bridge wait, so the answer lands in the conversation it was asked in, for
    ANY tenant, no per-user announce config needed). Returns "sent" /
    "failed: …" — never raises."""
    if not (account_id and target):
        return "failed: missing account/target"
    # the openclaw shim resolves `node` from PATH and needs Node >=22 — put
    # its own directory (e.g. /opt/node24/bin) first so any calling env works
    env = {**os.environ,
           "PATH": f"{Path(settings.openclaw_bin).parent}:{os.environ.get('PATH', '')}"}

    def _one(part: str) -> None:
        proc = subprocess.run(
            [settings.openclaw_bin, "message", "send",
             "--channel", settings.announce_channel,
             "--account", str(account_id),
             "--target", str(target),
             "-m", part],
            capture_output=True, text=True, timeout=90, env=env)
        if proc.returncode != 0:
            detail = (proc.stderr.strip() or proc.stdout.strip())[:200]
            raise RuntimeError(f"rc={proc.returncode} {detail}")

    # byte-bounded chunking replaces the silent text[:1000] cap (audit F10);
    # callers compare the return with "sent" VERBATIM, so full success keeps
    # that exact string and part detail rides only inside failure text
    report = send_chunked(_one, text, settings.notify_max_bytes)
    if report["error"] is None:
        return "sent"
    return f"failed: {report['error']}"


def send_wechat(settings: Settings, text: str) -> str:
    """Send ``text`` to the owner's WeChat announce target. Returns "sent" /
    "disabled" / "failed: …" — never raises."""
    if not (settings.announce_account and settings.announce_to):
        return "disabled (set ANNOUNCE_ACCOUNT and ANNOUNCE_TO)"
    return send_to_conversation(settings, settings.announce_account,
                                settings.announce_to, text)


# ── one-shot reminders ───────────────────────────────────────────────

_RELATIVE = re.compile(r"^\+?(\d+)\s*(m|min|minutes?|h|hours?|d|days?)$", re.IGNORECASE)

# How many poll cycles a due reminder may fail to send before it is
# dead-lettered rather than retried forever (~60s apart).
_MAX_DELIVERY_ATTEMPTS = 3


def _record_failure_metric(settings: Settings) -> None:
    """Record one `reminder/failed` row through the registered metrics sink so a
    give-up reaches the digest health footer. Best-effort and agent-free: the
    sink is the agent-supplied implementation (`agent/observability.py`), absent
    in a bare platform process, in which case this is a no-op."""
    from assistant.platform.llm import get_default_metrics_sink

    sink = get_default_metrics_sink()
    if sink is None:
        return
    try:
        sink(settings, datetime.now().strftime("reminder-%Y%m%d"), "reminder",
             {"failed": 1})
    except Exception:  # metrics must never break delivery
        log.debug("reminder failure metric not recorded", exc_info=True)


def parse_when(when: str, now: datetime | None = None) -> datetime | None:
    """'+30m' / '+2h' / '+1d', 'HH:MM' (today, or tomorrow if past),
    'YYYY-MM-DD HH:MM', or full ISO-8601 — None if unparseable.

    ISO-8601 is accepted because that is what the chat model naturally emits for
    an absolute time ('2026-07-24T20:55:00+08:00'); rejecting it cost a repair
    round on every such reminder. An offset-aware value is converted to system
    local time and stored naive, since reminders fire against the system clock."""
    now = now or datetime.now()
    when = str(when).strip()
    match = _RELATIVE.match(when)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)[0].lower()
        return now + timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[unit]: amount})
    try:
        parsed = datetime.fromisoformat(when)
        return (parsed.astimezone().replace(tzinfo=None) if parsed.tzinfo else parsed)
    except ValueError:
        pass
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
        self._lock_file = data_dir / "write.lock"

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

    @locked_transaction
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

    @locked_transaction
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
        """Send every due, unsent reminder — claim-then-send (the deliver-phase
        ledger pattern): due reminders are marked sent under the write lock
        BEFORE sending, so a concurrent cancel or second poll cycle can never
        double-send; a failed send un-claims under the lock, so the next cycle
        retries. Returns what was actually delivered.

        Retries are bounded: each failure bumps `attempts` and records
        `last_error`, and at `_MAX_DELIVERY_ATTEMPTS` the reminder is
        dead-lettered (`sent_at = "failed"`) instead of un-claimed. Unbounded
        retry is how one broken send path produced 752 identical failures in a
        day (2026-07-24) while the owner saw nothing — so a give-up also logs at
        ERROR, records a `reminder/failed` metric, and leaves the record visible
        to `failed()`, which the chat context reads."""
        from assistant.platform.locks import _path_lock

        now = now or datetime.now()
        stamp = now.strftime("%Y-%m-%d %H:%M")
        with _path_lock(self._lock_file):     # claim: mark before sending
            data = self._load()
            claimed = [r for r in data["reminders"]
                       if not r.get("sent_at") and r["due_at"] <= stamp]
            for r in claimed:
                r["sent_at"] = stamp
            if claimed:
                self._save(data)
        delivered = []
        for r in claimed:                     # send outside the lock
            status = send(settings, f"⏰ Reminder: {r['message']}")
            if status == "sent":
                delivered.append(r)
                continue
            attempts, gave_up = 0, False
            with _path_lock(self._lock_file):
                data = self._load()
                for row in data["reminders"]:
                    if row["id"] != r["id"] or row.get("sent_at") != stamp:
                        continue
                    attempts = int(row.get("attempts") or 0) + 1
                    row["attempts"] = attempts
                    row["last_error"] = str(status)[:200]
                    # give up → dead-letter; otherwise un-claim so the next
                    # poll cycle retries
                    row["sent_at"] = ("failed" if attempts >= _MAX_DELIVERY_ATTEMPTS
                                      else None)
                    gave_up = row["sent_at"] == "failed"
                self._save(data)
            if gave_up:
                log.error("reminder %s gave up after %d attempts: %s",
                          r["id"], _MAX_DELIVERY_ATTEMPTS, status)
                _record_failure_metric(settings)
            else:
                log.warning("reminder %s delivery failed (attempt %d/%d): %s",
                            r["id"], attempts, _MAX_DELIVERY_ATTEMPTS, status)
        return delivered

    def failed(self) -> list[dict]:
        """Reminders that exhausted their delivery attempts and were never sent.

        The owner cannot be told over the channel that just failed, so this is
        the record the chat context reads to state the truth on the next turn
        instead of asserting a delivery that never happened."""
        return [r for r in self._load()["reminders"] if r.get("sent_at") == "failed"]
