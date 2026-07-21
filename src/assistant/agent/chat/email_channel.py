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
import hashlib
import imaplib
import json
import logging
from datetime import datetime, timezone
from email.header import decode_header, make_header
from pathlib import Path

from assistant.platform.config import Settings
from assistant.agent.deliver.email import send_email

log = logging.getLogger("assistant")


class EmailChannel:
    """Inbound/outbound email channel over IMAP+SMTP. Enabled only when SMTP
    creds are present; ``owner_addresses`` is the allow-list of senders whose
    mail is answered, and a UID watermark (chat_state.json) makes each message
    fire at most once."""

    name = "email"

    def __init__(self, settings: Settings, owner_addresses: list[str]):
        """Normalize the owner allow-list to lowercased addresses and set the
        watermark file path; ``enabled`` reflects whether SMTP creds exist."""
        self.settings = settings
        self.owner = {a.strip().lower() for a in owner_addresses if a and "@" in a}
        self.state_file = settings.data_dir / "chat_state.json"
        self.enabled = bool(settings.smtp_user and settings.smtp_password)

    # ── UID watermark ────────────────────────────────────────────────
    def _load_state(self) -> dict:
        """Read chat_state.json, tolerating a missing or corrupt file (returns
        an empty dict) so a bad watermark never crashes the poll."""
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text())
            except ValueError:
                pass
        return {}

    def _save_uid(self, uid: int) -> None:
        """Advance the processed-mail watermark to ``uid``, preserving other
        state keys."""
        state = self._load_state()
        state["email_last_uid"] = uid
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(state))

    # ── polling ──────────────────────────────────────────────────────
    def poll(self) -> list[dict]:
        """Fetch new owner messages since the last watermark, newest UIDs only.
        On first ever run it seeds the watermark to the inbox tail and returns
        nothing so history is never replayed; otherwise it returns parsed owner
        messages and advances the watermark past everything seen. The IMAP
        connection is always logged out, even on error."""
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
        """Turn a raw RFC822 message into a channel message dict, or None to
        drop it. Rejects any sender not in the owner allow-list and any subject
        not starting with the chat prefix. The command text combines the
        after-prefix subject and the plain-text body, so a subject-only mail
        ("agent: trigger a run") still carries a command."""
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
        images = _image_attachments(msg, self.settings)
        if not text and not images:
            return None
        return {"channel": self.name, "text": text[:4000], "subject": subject,
                "sender": sender, "images": images}

    def send(self, text: str, in_reply_to: dict | None = None) -> None:
        """Email ``text`` back to the owner as HTML (each line a paragraph),
        threading it under the original subject when ``in_reply_to`` is given."""
        subject = f"Re: {in_reply_to['subject']}" if in_reply_to else "[assistant] chat"
        import html as _html
        body = "".join(f"<p>{_html.escape(line)}</p>" if line.strip() else "<br>"
                       for line in text.split("\n"))
        send_email(self.settings, subject, body)


def _image_attachments(msg: email.message.Message, settings: Settings) -> list[str]:
    """Save the mail's image attachments into `DATA_DIR/media/` and return
    their paths (capped at `vision_max_images`), for the vision chain. Only
    runs for owner mail — `_parse` rejects other senders before we get here."""
    from assistant.platform.vision import media_type_for

    paths: list[str] = []
    if not msg.is_multipart():
        return paths
    media_dir = settings.data_dir / "media"
    for part in msg.walk():
        if len(paths) >= settings.vision_max_images:
            break
        if not part.get_content_type().startswith("image/"):
            continue
        name = part.get_filename() or "attachment.png"
        suffix = Path(name).suffix.lower() or ".png"
        if media_type_for(f"x{suffix}") is None:
            continue
        payload = part.get_payload(decode=True) or b""
        if not payload or len(payload) > 10 * 1024 * 1024:
            continue
        media_dir.mkdir(parents=True, exist_ok=True)
        path = media_dir / (
            f"mail-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-"
            f"{hashlib.sha1(payload).hexdigest()[:8]}{suffix}")
        path.write_bytes(payload)
        paths.append(str(path))
    return paths


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
