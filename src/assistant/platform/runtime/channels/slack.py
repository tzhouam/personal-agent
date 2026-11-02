"""Slack Socket Mode ingress and threaded replies without a public HTTP server."""

import asyncio
from dataclasses import dataclass, field
import json
import logging
from typing import Callable
from urllib.parse import urlparse

import httpx

from assistant.platform.runtime.channels.base import (
    ChannelError, Handler, QueuedChannel, connect_socket, split_text, validate_identity,
)
from assistant.platform.runtime.contracts import ChannelEvent, DeliveryReceipt

log = logging.getLogger("assistant.channels.slack")


class _SlackRejected(ChannelError):
    """An explicit API rejection is distinguishable from an uncertain send."""


class _SlackRateLimited(_SlackRejected):
    """Retain provider backoff without retaining its response or credentials."""

    def __init__(self, retry_after: str):
        """Bound an externally supplied delay before a reconnect attempt."""
        super().__init__("Slack rate limit; retry after provider backoff")
        try:
            self.retry_after = min(60.0, max(1.0, float(retry_after)))
        except (ValueError, TypeError):
            self.retry_after = 30.0


@dataclass(frozen=True)
class SlackConfig:
    """Secrets stay out of repr; configured users are the only command senders."""

    account_id: str = "default"
    owner_id: str = "default"
    app_token: str = field(default="", repr=False)
    bot_token: str = field(default="", repr=False)
    allowed_users: tuple[str, ...] = ()
    allowed_channels: tuple[str, ...] = ()
    team_id: str = ""
    bot_user_id: str = ""
    queue_size: int = 32

    def __post_init__(self):
        """Reject open admission and missing credentials before a network call."""
        validate_identity(self.account_id, self.owner_id, self.allowed_users, self.queue_size)
        if not self.app_token or not self.bot_token:
            raise ValueError("Slack app_token and bot_token are required")


class SlackChannel(QueuedChannel):
    """Acknowledge accepted envelopes before dispatching model work."""

    def __init__(self, config: SlackConfig, *, http_client: httpx.AsyncClient | None = None,
                 socket_factory: Callable = connect_socket):
        """Accept injectable I/O for local fixture tests; connect only in run."""
        super().__init__(config.queue_size)
        self.config = config
        self._http = http_client or httpx.AsyncClient(timeout=30)
        self._own_http = http_client is None
        self._connect = socket_factory
        self._ws = None
        self._team_id = config.team_id
        self._bot_user_id = config.bot_user_id

    def decode_event(self, payload: dict) -> ChannelEvent | None:
        """Normalize admitted human events; never trust a payload owner identity."""
        body = payload.get("payload", payload)
        if not isinstance(body, dict):
            return None
        event = body.get("event", {})
        if not isinstance(event, dict) or event.get("type") not in {"message", "app_mention"}:
            return None
        if event.get("subtype") not in (None, "", "file_share") or event.get("bot_id"):
            return None
        user = str(event.get("user") or "")
        room = str(event.get("channel") or "")
        stamp = str(event.get("ts") or "")
        team = str(body.get("team_id") or event.get("team") or "")
        if not user or not room or not stamp or user == self._bot_user_id:
            return None
        if user not in self.config.allowed_users:
            return None
        if self._team_id and team != self._team_id:
            return None
        if self.config.allowed_channels and room not in self.config.allowed_channels:
            return None
        text = str(event.get("text") or "")
        direct = event.get("channel_type") == "im" or room.startswith("D")
        mention = f"<@{self._bot_user_id}>" if self._bot_user_id else ""
        addressed = event.get("type") == "app_mention" or bool(mention and mention in text)
        if not direct and not addressed:
            return None
        if mention:
            text = text.replace(mention, "").strip()
        files = event.get("files") or []
        attachments = [{k: f[k] for k in ("id", "name", "mimetype", "size") if k in f}
                       for f in files if isinstance(f, dict)][:10]
        if not text and not attachments:
            return None
        return ChannelEvent(
            channel="slack", account_id=self.config.account_id,
            event_id=f"message:{room}:{stamp}", sender_id=user, room_id=room,
            text=text or "[Attachment received]", owner_id=self.config.owner_id,
            workspace_id=team, thread_id=str(event.get("thread_ts") or ("" if direct else stamp)),
            message_id=stamp, is_direct=direct, addressed=addressed,
            metadata={"attachments": attachments} if attachments else {},
        )

    async def _api(self, method: str, payload: dict, *, app: bool = False) -> dict:
        """Keep Slack response bodies and bearer credentials out of exceptions."""
        token = self.config.app_token if app else self.config.bot_token
        try:
            response = await self._http.post(
                f"https://slack.com/api/{method}", json=payload,
                headers={"Authorization": f"Bearer {token}"})
            if response.status_code == 429:
                raise _SlackRateLimited(response.headers.get("Retry-After", "30"))
            if 400 <= response.status_code < 500:
                raise _SlackRejected("Slack rejected the API request")
            response.raise_for_status()
            result = response.json()
        except ChannelError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise ChannelError(f"Slack API unavailable ({type(exc).__name__})") from None
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise _SlackRejected("Slack rejected the API request")
        return result

    async def receive_envelope(self, envelope: dict, socket) -> bool:
        """Acknowledge only after durable admission, or available queue capacity."""
        envelope_id = envelope.get("envelope_id")
        if not envelope_id:
            return False
        event = self.decode_event(envelope)
        durable = False
        if event is not None:
            try:
                durable = await self._accept(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("Slack durable admission failed (%s); envelope left unacknowledged",
                          type(exc).__name__)
                return False
        try:
            ready = self._offer(event) if event else None
        except asyncio.QueueFull:
            if not durable:
                log.warning("Slack input queue is full; envelope left unacknowledged")
                return False
            # The durable host inbox, rather than this bounded callback queue,
            # owns the accepted event and will process it after a restart too.
            ready = None
        try:
            await socket.send(json.dumps({"envelope_id": envelope_id}))
        finally:
            if ready is not None:
                ready.set()
        return event is not None

    async def run(self, handler: Handler) -> None:
        """Reconnect outbound Socket Mode while keeping execution off its reader."""
        if self._closed or self._worker is not None:
            raise ChannelError("Slack channel is already running or closed")
        self._worker = asyncio.create_task(self._dispatch(handler))
        delay = 1.0
        try:
            while not self._stop.is_set():
                try:
                    identity = await self._api("auth.test", {})
                    team = str(identity.get("team_id") or "")
                    if self.config.team_id and team != self.config.team_id:
                        raise ChannelError("Slack token does not belong to configured workspace")
                    self._team_id = team or self.config.team_id
                    self._bot_user_id = str(identity.get("user_id") or self.config.bot_user_id)
                    opened = await self._api("apps.connections.open", {}, app=True)
                    url = opened.get("url", "")
                    parsed = urlparse(url)
                    if parsed.scheme != "wss" or not (parsed.hostname or "").endswith(".slack.com"):
                        raise ChannelError("Slack returned an invalid Socket Mode URL")
                    self._ws = await self._connect(url)
                    self.connected = True
                    delay = 1.0
                    async for raw in self._ws:
                        if self._stop.is_set():
                            break
                        try:
                            envelope = json.loads(raw)
                        except (ValueError, TypeError):
                            continue
                        if not isinstance(envelope, dict):
                            continue
                        if envelope.get("type") == "disconnect":
                            break
                        await self.receive_envelope(envelope, self._ws)
                except _SlackRateLimited as exc:
                    delay = exc.retry_after
                    log.warning("Slack rate limited; backing off")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("Slack connection interrupted (%s)", type(exc).__name__)
                finally:
                    self.connected = False
                    if self._ws is not None:
                        await self._ws.close()
                        self._ws = None
                if not self._stop.is_set():
                    await self._backoff(delay)
                    delay = min(delay * 2, 60)
        finally:
            await self.close()

    async def send(self, event: ChannelEvent, text: str) -> DeliveryReceipt:
        """Send every chunk to the original thread and retain confirmed receipts."""
        if (event.channel != "slack" or event.account_id != self.config.account_id
                or event.owner_id != self.config.owner_id
                or event.sender_id not in self.config.allowed_users
                or (self._team_id and event.workspace_id != self._team_id)
                or (self.config.allowed_channels and event.room_id not in self.config.allowed_channels)):
            return DeliveryReceipt("failed", error="Slack event does not belong to this channel")
        chunks = split_text(text, 3500)
        if not chunks:
            return DeliveryReceipt("failed", error="Cannot send an empty reply")
        ids = []
        for chunk in chunks:
            payload = {"channel": event.room_id, "text": chunk,
                       "unfurl_links": False, "unfurl_media": False}
            if event.thread_id:
                payload["thread_ts"] = event.thread_id
            try:
                result = await self._api("chat.postMessage", payload)
            except _SlackRejected as exc:
                return DeliveryReceipt("failed", tuple(ids), str(exc))
            except ChannelError as exc:
                # A failed network response may follow an accepted message. Never
                # retry a send without the host reconciling its delivery ledger.
                return DeliveryReceipt("unknown", tuple(ids), str(exc))
            stamp = str(result.get("ts") or "")
            if not stamp:
                return DeliveryReceipt("unknown", tuple(ids), "Slack omitted the sent message receipt")
            ids.append(stamp)
        return DeliveryReceipt("sent", tuple(ids))

    async def close(self) -> None:
        """Release sockets and callbacks; closing twice is harmless."""
        self.connected = False
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        if self._ws is not None:
            await self._ws.close()
        await self._close_worker()
        if self._own_http:
            await self._http.aclose()
