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
        state keys. Atomic tmp+replace (Track D §2: a crash mid-write must
        not corrupt the rollback shadow old code resumes from)."""
        import os as _os

        state = self._load_state()
        state["email_last_uid"] = uid
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_name(self.state_file.name + ".tmp")
        tmp.write_text(json.dumps(state))
        _os.replace(tmp, self.state_file)

    # ── polling (Track D §2: the (UIDVALIDITY, UID) ledger) ──────────
    def poll(self) -> list[dict]:
        """Discover new mail into the outbox ledger and return this cycle's
        WORK, ascending by UID with head-of-line discipline (the caller stops
        at the first message that stays nonterminal):

        - `pending` rows come back as parsed message dicts carrying
          ``uidvalidity``/``uid`` — the caller drives begin_turn →
          finish_turn → send → ack via this channel's ledger methods.
        - `processed` rows (a turn already ran; its reply is the outbox)
          come back as ``{"kind": "email_outbox_retry", …}`` — the caller
          retries THE SEND ONLY. The old scalar watermark advanced before
          processing, so one failed turn silently dropped the owner's mail
          forever (audit F3); the watermark survives only as the rollback
          shadow, advanced to the settled frontier AFTER ledger commits.

        First ever run imports the legacy watermark (or seeds to the inbox
        tail) so history is never replayed; a UIDVALIDITY change re-baselines
        with a visible system note (old-epoch rows are retained)."""
        if not self.enabled:
            return []
        from assistant.platform.delivery import OutboxDB

        conn = imaplib.IMAP4_SSL(self.settings.imap_host, self.settings.imap_port)
        try:
            conn.login(self.settings.smtp_user, self.settings.smtp_password)
            conn.select("INBOX", readonly=True)
            try:
                uidvalidity = int((conn.response("UIDVALIDITY")[1] or [None])[0])
            except (TypeError, ValueError, IndexError):
                # a transient metadata failure must NEVER look like an epoch
                # change (that would trigger destructive re-baselining) —
                # skip this cycle entirely
                log.warning("email poll: UIDVALIDITY unavailable — skipping cycle")
                return []
            _, data = conn.uid("search", None, "ALL")
            uids = sorted(int(u) for u in data[0].split())
            outbox = OutboxDB(self.settings.data_dir)
            try:
                baseline = self._ensure_baseline(outbox, uidvalidity, uids)
                known = {u for (u,) in outbox.conn.execute(
                    "SELECT uid FROM email_ledger WHERE uidvalidity=?",
                    (uidvalidity,))}
                # Persist EVERY discovered UID before any fetch touches the
                # network (review round 2): intent lands first, so a fetch
                # failure or crash can never let the frontier advance past an
                # unrecorded mail. Content is fetched at PROCESSING time; the
                # ignored/pending split happens there.
                for uid in uids:
                    if uid > baseline and uid not in known:
                        outbox.email_discover(uidvalidity, uid)
                messages: list[dict] = []
                for item in outbox.email_due(uidvalidity):
                    if item["state"] == "processed":
                        messages.append({
                            "channel": self.name, "kind": "email_outbox_retry",
                            "text": "[outbox retry]", "sender": "",
                            "subject": "[assistant] chat",
                            "uidvalidity": item["uidvalidity"],
                            "uid": item["uid"],
                            "reply": item["reply"] or "",
                            "surfaced_ids": item["surfaced_ids"]})
                        continue
                    ok, msg = self._fetch_parse(conn, item["uid"])
                    if not ok:
                        break   # transient fetch failure: the row STAYS
                        #         pending; head-of-line stops here this cycle
                    if msg is None:   # deterministic non-message → settle
                        outbox.conn.execute(
                            "UPDATE email_ledger SET state='ignored', updated_at=?"
                            " WHERE uidvalidity=? AND uid=? AND state='pending'",
                            (datetime.now(timezone.utc).isoformat(),
                             uidvalidity, item["uid"]))
                        outbox.conn.commit()
                        continue
                    msg["uidvalidity"] = uidvalidity
                    msg["uid"] = item["uid"]
                    messages.append(msg)
                frontier = outbox.email_settled_frontier(uidvalidity, baseline)
                if frontier > int(self._load_state().get("email_last_uid") or 0):
                    self._save_uid(frontier)   # rollback shadow, ledger-first
                return messages
            finally:
                outbox.close()
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
        images, rejected = _image_attachments(msg, self.settings)
        if not text and not images and not rejected:
            return None
        return {"channel": self.name, "text": text[:4000], "subject": subject,
                "sender": sender, "images": images,
                "rejected_images": rejected}

    def _fetch_parse(self, conn, uid: int):
        """Fetch one UID and parse it → (ok, msg). ok=False means a TRANSIENT
        fetch failure (the row must stay pending — settling it as ignored
        would turn one IMAP timeout into a silently dropped mail, the exact
        F3 class this ledger exists to kill); ok=True with msg=None means the
        mail parsed to not-an-owner-chat-message (genuinely settleable)."""
        try:
            _, fetched = conn.uid("fetch", str(uid), "(RFC822)")
        except Exception:
            log.exception("email fetch failed for uid %s", uid)
            return False, None
        if not fetched or not isinstance(fetched[0], tuple):
            return False, None   # server said nothing usable: treat transient
        try:
            return True, self._parse(fetched[0][1])
        except OSError:
            # attachment staging does filesystem I/O — disk-full/permissions
            # are TRANSIENT environment failures, never grounds to settle
            log.exception("email parse I/O failed for uid %s", uid)
            return False, None
        except Exception:
            log.exception("email parse crashed for uid %s", uid)
            return True, None    # deterministic parse crash: settle as ignored

    def _ensure_baseline(self, outbox, uidvalidity: int, uids: list[int]) -> int:
        """The epoch baseline UID (mail at or below it is settled history).
        First use imports the legacy watermark paired with the CURRENT
        UIDVALIDITY; an unnoticed pre-import mailbox reset (highest UID below
        the watermark) re-baselines to the tail with a visible system note;
        a later UIDVALIDITY change re-baselines the new epoch (old-epoch
        rows retained) and says so."""
        stored_uv = outbox.get_meta("email_uidvalidity")
        tail = max(uids) if uids else 0
        if stored_uv is None:
            legacy = self._load_state().get("email_last_uid")
            if legacy is None:
                baseline = tail          # first start: never replay history
            elif tail < int(legacy):     # mailbox was reset before cutover
                baseline = tail
                outbox.add_system_note(
                    "邮箱状态在切换前被重置过 — 旧水位不可信，已按当前邮箱"
                    "末尾重新基线（期间的邮件可能被跳过）")
            else:
                baseline = int(legacy)
            outbox.set_meta_many({"email_uidvalidity": str(uidvalidity),
                                  "email_baseline_uid": str(baseline)})
            return baseline
        if int(stored_uv) != uidvalidity:
            settled = outbox.email_settle_old_epoch_pending(int(stored_uv))
            outbox.set_meta_many({"email_uidvalidity": str(uidvalidity),
                                  "email_baseline_uid": str(tail)})
            outbox.add_system_note(
                "邮箱 UIDVALIDITY 变化（邮箱被重建/迁移）— 已重新基线"
                + (f"，{settled} 封未处理邮件已按失败登记" if settled else "")
                + "；重置窗口内的邮件可能未被处理")
            return tail
        return int(outbox.get_meta("email_baseline_uid") or 0)

    # ── ledger transitions the poll-loop caller drives ───────────────
    def begin_turn(self, message: dict) -> str | None:
        from assistant.platform.delivery import OutboxDB

        db = OutboxDB(self.settings.data_dir)
        try:
            return db.email_begin_turn(message["uidvalidity"], message["uid"])
        finally:
            db.close()

    def finish_turn(self, message: dict, token: str, reply: str,
                    surfaced_ids: list[str]) -> bool:
        from assistant.platform.delivery import OutboxDB

        db = OutboxDB(self.settings.data_dir)
        try:
            return db.email_finish_turn(message["uidvalidity"], message["uid"],
                                        token, reply, surfaced_ids or [])
        finally:
            db.close()

    def turn_failed(self, message: dict, token: str, error: str) -> None:
        from assistant.platform.delivery import OutboxDB

        db = OutboxDB(self.settings.data_dir)
        try:
            db.email_turn_failed(message["uidvalidity"], message["uid"], token,
                                 error)
        finally:
            db.close()

    def ack(self, message: dict) -> None:
        from assistant.platform.delivery import OutboxDB

        db = OutboxDB(self.settings.data_dir)
        try:
            db.email_ack(message["uidvalidity"], message["uid"])
        finally:
            db.close()

    def send_failed(self, message: dict, error: str) -> None:
        from assistant.platform.delivery import OutboxDB

        db = OutboxDB(self.settings.data_dir)
        try:
            db.email_send_failed(message["uidvalidity"], message["uid"], error)
        finally:
            db.close()

    def send(self, text: str, in_reply_to: dict | None = None) -> None:
        """Email ``text`` back to the owner as HTML (each line a paragraph),
        threading it under the original subject when ``in_reply_to`` is given."""
        subject = f"Re: {in_reply_to['subject']}" if in_reply_to else "[assistant] chat"
        import html as _html
        body = "".join(f"<p>{_html.escape(line)}</p>" if line.strip() else "<br>"
                       for line in text.split("\n"))
        send_email(self.settings, subject, body)


def _image_attachments(msg: email.message.Message,
                       settings: Settings) -> tuple[list[str], list[str]]:
    """Save the mail's image attachments into `DATA_DIR/media/` and return
    `(paths, rejection_notes)` for the vision chain. Only runs for owner
    mail — `_parse` rejects other senders before we get here. Image-typed
    parts that CANNOT be staged (unsupported suffix, empty, oversized, past
    the cap) become bracketed notes instead of vanishing — a silently
    dropped attachment reads to the owner as "the agent ignored my photo"."""
    from assistant.platform.vision import media_type_for

    paths: list[str] = []
    rejected: list[str] = []

    def _clip(name: str) -> str:
        return name if len(name) <= 80 else name[:77] + "…"

    if not msg.is_multipart():
        return paths, rejected
    media_dir = settings.data_dir / "media"
    suffix_of = {"image/png": ".png", "image/jpeg": ".jpg",
                 "image/gif": ".gif", "image/webp": ".webp"}
    for part in msg.walk():
        ctype = part.get_content_type()
        if not ctype.startswith("image/"):
            continue
        name = part.get_filename() or "attachment"
        if len(paths) >= settings.vision_max_images:
            rejected.append(f"[image ignored (max {settings.vision_max_images} "
                            f"per message): {_clip(Path(name).name)}]")
            continue
        # Validate by the declared MIME type, not the filename: an image/tiff
        # named scan.png must not be staged as a PNG, and an extensionless
        # JPEG must not default to .png. The suffix is derived FROM the type.
        suffix = suffix_of.get(ctype)
        if suffix is None:
            rejected.append(f"[unsupported image type ({ctype[:40]}): "
                            f"{_clip(Path(name).name)}]")
            continue
        payload = part.get_payload(decode=True) or b""
        if not payload:
            rejected.append(f"[empty image attachment: {_clip(Path(name).name)}]")
            continue
        if len(payload) > 10 * 1024 * 1024:
            rejected.append(f"[image too large to process: {_clip(Path(name).name)}]")
            continue
        media_dir.mkdir(parents=True, exist_ok=True)
        path = media_dir / (
            f"mail-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-"
            f"{hashlib.sha1(payload).hexdigest()[:8]}{suffix}")
        path.write_bytes(payload)
        paths.append(str(path))
    return paths, rejected


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
