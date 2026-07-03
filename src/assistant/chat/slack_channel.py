"""Slack channel: DM the bot, the agent replies in the DM.

Polling-only (conversations.history over outbound HTTPS), so it works behind
NAT with no public callback — the right fit for this container.

Setup (one-time, owner):
1. api.slack.com/apps → Create New App (from scratch) → OAuth & Permissions →
   Bot Token Scopes: ``im:history``, ``im:read``, ``chat:write`` and
   (only if SLACK_OWNER_ID is left empty) ``users:read`` + ``users:read.email``.
2. Install to workspace → copy the Bot User OAuth Token (xoxb-…) into
   SLACK_BOT_TOKEN.
3. Optionally set SLACK_OWNER_ID (profile → ⋮ → Copy member ID); otherwise the
   channel resolves it from the owner's email addresses at startup.
4. In Slack, open a DM with the bot's app name and just talk to it.

Sender authentication: only messages whose ``user`` is the resolved owner id
are processed — anyone else DMing the bot is ignored. A per-DM ``ts``
watermark in chat_state.json (initialized to the conversation tail on first
start) guarantees at-most-once processing and no history replay.
"""

import json
import logging

import httpx

from ..config import Settings

log = logging.getLogger("assistant")

_API = "https://slack.com/api"


class SlackChannel:
    name = "slack"

    def __init__(self, settings: Settings, owner_emails: list[str] | None = None):
        self.settings = settings
        self.enabled = bool(settings.slack_bot_token)
        self.owner_emails = [e for e in (owner_emails or []) if e and "@" in e]
        self.owner_id = settings.slack_owner_id
        self.state_file = settings.data_dir / "chat_state.json"

    # ── Slack Web API plumbing ───────────────────────────────────────
    def _api(self, method: str, **params) -> dict:
        resp = httpx.post(f"{_API}/{method}", data=params, timeout=15, headers={
            "Authorization": f"Bearer {self.settings.slack_bot_token}"})
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"slack {method}: {data.get('error')}")
        return data

    def _resolve_owner(self) -> str:
        if self.owner_id:
            return self.owner_id
        for address in self.owner_emails:
            try:
                self.owner_id = self._api("users.lookupByEmail", email=address)["user"]["id"]
                log.info("slack: owner resolved from %s → %s", address, self.owner_id)
                return self.owner_id
            except Exception:
                continue
        raise RuntimeError("slack: set SLACK_OWNER_ID (email lookup found no owner)")

    # ── ts watermark (per DM channel) ────────────────────────────────
    def _load_state(self) -> dict:
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text())
            except ValueError:
                pass
        return {}

    def _watermarks(self) -> dict:
        return self._load_state().get("slack_last_ts", {})

    def _save_watermark(self, channel_id: str, ts: str) -> None:
        state = self._load_state()
        state.setdefault("slack_last_ts", {})[channel_id] = ts
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(state))

    # ── polling ──────────────────────────────────────────────────────
    def poll(self) -> list[dict]:
        if not self.enabled:
            return []
        owner = self._resolve_owner()
        marks = self._watermarks()
        messages = []
        ims = self._api("conversations.list", types="im", limit=200).get("channels", [])
        for im in ims:
            if im.get("user") != owner:
                continue  # DMs from anyone but the owner are never read
            channel_id = im["id"]
            history = self._api("conversations.history", channel=channel_id,
                                limit=50, **({"oldest": marks[channel_id]}
                                             if channel_id in marks else {}))
            batch = history.get("messages", [])
            if channel_id not in marks:  # first start: don't replay history
                if batch:
                    self._save_watermark(channel_id, batch[0]["ts"])
                else:
                    self._save_watermark(channel_id, "0")
                continue
            fresh = [m for m in batch
                     if m.get("type") == "message" and not m.get("subtype")
                     and not m.get("bot_id") and m.get("user") == owner
                     and float(m["ts"]) > float(marks[channel_id])
                     and (m.get("text") or "").strip()]
            if batch:
                self._save_watermark(channel_id, batch[0]["ts"])  # newest first
            for m in sorted(fresh, key=lambda m: float(m["ts"])):
                messages.append({"channel": self.name, "text": m["text"][:4000],
                                 "subject": "", "sender": owner,
                                 "channel_id": channel_id})
        return messages

    def send(self, text: str, in_reply_to: dict | None = None) -> None:
        channel_id = (in_reply_to or {}).get("channel_id")
        if not channel_id:  # unsolicited push: open (or reuse) the owner DM
            channel_id = self._api("conversations.open",
                                   users=self._resolve_owner())["channel"]["id"]
        self._api("chat.postMessage", channel=channel_id, text=text[:4000])
