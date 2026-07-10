"""Gmail collector — turns recent inbox headers into email/newsletter Observations.

Registers as `@register("gmail")`. Read-only IMAP, headers only: message bodies
never enter the pipeline, keeping the source privacy-preserving.
"""

import email
import email.utils
import imaplib
from datetime import datetime
from email.header import decode_header, make_header

from ..config import Settings
from . import register

_MAX_MESSAGES = 100
_HEADER_FIELDS = "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE LIST-UNSUBSCRIBE)])"


@register("gmail")
class GmailCollector:
    """IMAP, read-only, headers only — message bodies never enter the pipeline.

    Uses the same Gmail app password as SMTP delivery, so no OAuth dance.
    GitHub notification mail is skipped (the GitHub collector covers it natively).
    """

    name = "gmail"

    def __init__(self, settings: Settings):
        """Reuse the SMTP app password for IMAP; disabled unless both the flag
        and a password are set, so a missing credential makes the collector a
        no-op rather than an error."""
        self.enabled = settings.gmail_enabled and bool(settings.smtp_password)
        self.host = settings.imap_host
        self.port = settings.imap_port
        self.user = settings.smtp_user
        self.password = settings.smtp_password

    def collect(self, since: datetime) -> list[dict]:
        """Return one Observation per inbox message since `since` (headers only).

        Logs into INBOX read-only, fetches the last `_MAX_MESSAGES` matching the
        date-granular IMAP SINCE search, and maps each header block to an
        observation. Always logs out in a finally so a mid-fetch error never
        leaks the connection — the collector degrades, never crashes.
        """
        if not self.enabled:
            return []
        conn = imaplib.IMAP4_SSL(self.host, self.port)
        try:
            conn.login(self.user, self.password)
            conn.select("INBOX", readonly=True)
            _, data = conn.search(None, f'(SINCE "{since.strftime("%d-%b-%Y")}")')
            msg_ids = data[0].split()[-_MAX_MESSAGES:]

            observations = []
            for msg_id in msg_ids:
                _, fetched = conn.fetch(msg_id, _HEADER_FIELDS)
                if not fetched or not isinstance(fetched[0], tuple):
                    continue
                obs = self._headers_to_observation(fetched[0][1], since)
                if obs:
                    observations.append(obs)
            return observations
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    def _headers_to_observation(self, raw_headers: bytes, since: datetime) -> dict | None:
        """Map one raw header block to an Observation, or None to drop it.

        Decodes From/Subject, tags the item `newsletter` when a List-Unsubscribe
        header is present else `email`, and carries the sender's domain as the
        entity. Returns None for GitHub notification mail (the github collector
        owns those) and for messages older than `since` — the IMAP SINCE filter
        is date-granular, so the real timestamp cutoff is enforced here.
        """
        msg = email.message_from_bytes(raw_headers)
        sender = str(make_header(decode_header(msg.get("From", ""))))
        subject = str(make_header(decode_header(msg.get("Subject", ""))))

        if "notifications@github.com" in sender:
            return None  # GitHub collector already covers these
        parsed_date = email.utils.parsedate_to_datetime(msg.get("Date", "")) if msg.get("Date") else None
        if parsed_date and parsed_date < since:
            return None  # IMAP SINCE is date-granular; enforce the real cutoff

        sender_addr = email.utils.parseaddr(sender)[1]
        sender_domain = sender_addr.rsplit("@", 1)[-1] if "@" in sender_addr else ""
        kind = "newsletter" if msg.get("List-Unsubscribe") else "email"
        return {
            "source": "gmail",
            "ts": parsed_date.isoformat() if parsed_date else "",
            "kind": kind,
            "title": f"Email from {sender_addr or sender}: {subject}"[:300],
            "url": None,
            "entities": [sender_domain] if sender_domain else [],
            "raw": {},
        }
