"""Email channel: message the agent by mailing the digest mailbox with a
subject starting with the configured prefix (default "agent", e.g.
"agent: what's due this week?"). Replies come back by email.

Sender authentication: the From address must be one of the owner's own
addresses (profile emails + SMTP user + DIGEST_TO). Everything else in the
inbox is ignored. A UID watermark in chat_state.json guarantees each message
is processed at most once — on first start it is initialized to the current
inbox tail so history is never replayed.
"""

import email
import email.utils
import imaplib
import json
import logging
from email.header import decode_header, make_header

from ..config import Settings
from ..deliver.email import send_email

log = logging.getLogger("assistant")


class EmailChannel:
    name = "email"

    def __init__(self, settings: Settings, owner_addresses: list[str]):
        self.settings = settings
        self.owner = {a.strip().lower() for a in owner_addresses if a and "@" in a}
        self.state_file = settings.data_dir / "chat_state.json"
        self.enabled = bool(settings.smtp_user and settings.smtp_password)

    # ── UID watermark ────────────────────────────────────────────────
    def _load_state(self) -> dict:
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text())
            except ValueError:
                pass
        return {}

    def _save_uid(self, uid: int) -> None:
        state = self._load_state()
        state["email_last_uid"] = uid
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(state))

    # ── polling ──────────────────────────────────────────────────────
    def poll(self) -> list[dict]:
        if not self.enabled:
            return []
        conn = imaplib.IMAP4_SSL(self.settings.imap_host, self.settings.imap_port)
        try:
            conn.login(self.settings.smtp_user, self.settings.smtp_password)
            conn.select("INBOX", readonly=True)
            _, data = conn.uid("search", None, "ALL")
            uids = [int(u) for u in data[0].split()]
            if not uids:
                return []
            last = self._load_state().get("email_last_uid")
            if last is None:  # first start: don't replay inbox history
                self._save_uid(max(uids))
                return []
            fresh = [u for u in uids if u > last]
            if not fresh:
                return []
            self._save_uid(max(uids))

            messages = []
            for uid in fresh:
                _, fetched = conn.uid("fetch", str(uid), "(RFC822)")
                if not fetched or not isinstance(fetched[0], tuple):
                    continue
                msg = self._parse(fetched[0][1])
                if msg:
                    messages.append(msg)
            return messages
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    def _parse(self, raw: bytes) -> dict | None:
        msg = email.message_from_bytes(raw)
        sender = email.utils.parseaddr(str(msg.get("From", "")))[1].lower()
        if sender not in self.owner:
            return None  # not the owner — never processed, never answered
        subject = str(make_header(decode_header(msg.get("Subject", "")))).strip()
        bare = subject.lower().removeprefix("re:").strip()
        if not bare.startswith(self.settings.chat_subject_prefix.lower()):
            return None
        body = _text_body(msg)
        # subject text counts too, so "agent: trigger a run" with an empty body works
        text = bare[len(self.settings.chat_subject_prefix):].lstrip(":： ").strip()
        if body:
            text = f"{text}\n{body}".strip()
        if not text:
            return None
        return {"channel": self.name, "text": text[:4000], "subject": subject,
                "sender": sender}

    def send(self, text: str, in_reply_to: dict | None = None) -> None:
        subject = f"Re: {in_reply_to['subject']}" if in_reply_to else "[assistant] chat"
        import html as _html
        body = "".join(f"<p>{_html.escape(line)}</p>" if line.strip() else "<br>"
                       for line in text.split("\n"))
        send_email(self.settings, subject, body)


def _text_body(msg: email.message.Message) -> str:
    """First text/plain part, with quoted reply history stripped."""
    part = None
    if msg.is_multipart():
        for candidate in msg.walk():
            if candidate.get_content_type() == "text/plain":
                part = candidate
                break
    elif msg.get_content_type() == "text/plain":
        part = msg
    if part is None:
        return ""
    payload = part.get_payload(decode=True) or b""
    text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    lines = []
    for line in text.splitlines():
        if line.startswith(">") or line.strip().endswith("wrote:"):
            break  # start of quoted history
        lines.append(line)
    return "\n".join(lines).strip()[:4000]
