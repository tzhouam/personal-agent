"""QQ private/group chat through an authenticated OneBot v11 forward socket."""

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
import itertools
import json
import logging
import re
import time
from typing import Callable
from urllib.parse import urlparse

from assistant.platform.runtime.channels.base import (
    ChannelError, Handler, QueuedChannel, connect_socket, split_text, validate_identity,
)
from assistant.platform.runtime.contracts import ChannelEvent, DeliveryReceipt

log = logging.getLogger("assistant.channels.qq")
_CQ = re.compile(r"\[CQ:([\w-]+)((?:,[^\]]*)?)\]")


def _numeric_id(value, *, positive: bool = False) -> str:
    """Validate OneBot identifiers at ingress before they become reply targets."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return ""
    raw = str(value)
    if not re.fullmatch(r"-?\d+", raw):
        return ""
    number = int(raw)
    return str(number) if (number > 0 if positive else number != 0) else ""


def _cq_decode(value: str) -> str:
    """Decode entities only after segment boundaries and parameter commas."""
    return (value.replace("&#44;", ",").replace("&#91;", "[")
            .replace("&#93;", "]").replace("&amp;", "&"))


def parse_segments(message) -> list[dict]:
    """Accept OneBot array or CQ string format without promoting escaped CQ."""
    if isinstance(message, list):
        return [segment for segment in message
                if isinstance(segment, dict) and isinstance(segment.get("data"), dict)]
    if not isinstance(message, str):
        return []
    segments = []
    cursor = 0
    for match in _CQ.finditer(message):
        if match.start() > cursor:
            segments.append({"type": "text", "data": {"text": _cq_decode(message[cursor:match.start()])}})
        params = {}
        for parameter in match.group(2).lstrip(",").split(","):
            if "=" in parameter:
                key, value = parameter.split("=", 1)
                params[key] = _cq_decode(value)
        segments.append({"type": match.group(1), "data": params})
        cursor = match.end()
    if cursor < len(message):
        segments.append({"type": "text", "data": {"text": _cq_decode(message[cursor:])}})
    return segments


@dataclass(frozen=True)
class QQConfig:
    """Connect an existing OneBot implementation; this is not QQ Bot API v2."""

    account_id: str = "default"
    owner_id: str = "default"
    ws_url: str = "ws://127.0.0.1:3001"
    access_token: str = field(default="", repr=False)
    allowed_users: tuple[str, ...] = ()
    allowed_groups: tuple[str, ...] = ()
    self_id: str = ""
    queue_size: int = 32
    api_timeout: float = 15

    def __post_init__(self):
        """Restrict tokenless OneBot to local sockets and require owner admission."""
        validate_identity(self.account_id, self.owner_id, self.allowed_users, self.queue_size)
        parsed = urlparse(self.ws_url)
        if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
            raise ValueError("QQ ws_url must be a WebSocket URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("QQ credentials belong in access_token, not ws_url")
        if not self.access_token and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("A remote OneBot connection requires access_token")
        if self.api_timeout <= 0:
            raise ValueError("QQ api_timeout must be positive")
        if any(not _numeric_id(user, positive=True) for user in self.allowed_users):
            raise ValueError("QQ allowed_users must contain QQ numeric user ids")
        if any(not _numeric_id(group, positive=True) for group in self.allowed_groups):
            raise ValueError("QQ allowed_groups must contain QQ numeric group ids")
        if self.self_id and not _numeric_id(self.self_id, positive=True):
            raise ValueError("QQ self_id must be a QQ numeric user id")


class _OneBotRejected(ChannelError):
    """A provider explicitly refused an action, so no delivery is claimed."""


class QQChannel(QueuedChannel):
    """Keep API response correlation in its own reader during slow agent work."""

    def __init__(self, config: QQConfig, *, socket_factory: Callable = connect_socket):
        """Start no network in the constructor, allowing fixture-only decoding."""
        super().__init__(config.queue_size)
        self.config = config
        self._connect = socket_factory
        self._ws = None
        self._reader: asyncio.Task | None = None
        self._write_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future] = {}
        self._sequence = itertools.count(1)
        self._self_id = config.self_id
        self._quotes: OrderedDict[str, tuple[str, bool, float]] = OrderedDict()

    def _remember(self, message_id: str, text: str, own: bool) -> None:
        """Bound quote context by count, age and individual text size."""
        self._quotes[message_id] = (text[:800], own, time.monotonic())
        while self._quotes and (len(self._quotes) > 1000
                               or time.monotonic() - next(iter(self._quotes.values()))[2] > 7 * 86400):
            self._quotes.popitem(last=False)

    def decode_event(self, payload: dict) -> ChannelEvent | None:
        """Parse an authorized QQ message and preserve its concrete reply target."""
        if payload.get("post_type") != "message":
            return None
        kind = payload.get("message_type")
        if kind not in {"private", "group"}:
            return None
        sender = _numeric_id(payload.get("user_id"), positive=True)
        bot = _numeric_id(payload.get("self_id") or self._self_id, positive=True)
        message_id = _numeric_id(payload.get("message_id"))
        if not sender or not bot or not message_id or sender == bot:
            return None
        if self._self_id and bot != self._self_id:
            return None
        if sender not in self.config.allowed_users:
            return None
        direct = kind == "private"
        room = sender if direct else _numeric_id(payload.get("group_id"), positive=True)
        if not room or (not direct and self.config.allowed_groups and room not in self.config.allowed_groups):
            return None
        segments = parse_segments(payload.get("message", payload.get("raw_message", "")))
        texts, attachments = [], []
        mentioned, reply_to = False, ""
        for segment in segments:
            data = segment["data"]
            seg_type = segment.get("type")
            if seg_type == "text":
                texts.append(str(data.get("text") or ""))
            elif seg_type == "at":
                target = str(data.get("qq") or "")
                if target == bot:
                    mentioned = True
                else:
                    texts.append(f"@{target}")
            elif seg_type == "reply":
                reply_to = str(data.get("id") or "")
            elif seg_type in {"image", "file", "record"}:
                attachments.append({"type": seg_type,
                                    **{k: data[k] for k in ("file", "url", "name") if k in data}})
        text = "".join(texts).strip()
        quote = self._quotes.get(reply_to)
        if quote and time.monotonic() - quote[2] > 7 * 86400:
            quote = None
        addressed = mentioned or bool(quote and quote[1]) or text.startswith("/")
        if not direct and not addressed:
            return None
        if not text and not attachments and not addressed:
            return None
        metadata = {"reply_to_id": reply_to}
        if quote:
            metadata["quoted_text"] = quote[0]
        if attachments:
            metadata["attachments"] = attachments[:10]
        self._remember(message_id, text, False)
        return ChannelEvent(
            channel="qq_onebot", account_id=self.config.account_id,
            event_id=f"message:{bot}:{message_id}", sender_id=sender, room_id=room,
            text=text or ("[Attachment received]" if attachments else "[You mentioned the assistant]"),
            owner_id=self.config.owner_id, workspace_id=bot,
            thread_id="", message_id=message_id, is_direct=direct,
            addressed=addressed, metadata=metadata,
        )

    async def _write(self, payload: dict) -> None:
        """Serialize writes while leaving the reader free to deliver responses."""
        async with self._write_lock:
            if self._ws is None:
                raise ChannelError("QQ socket is not connected")
            await self._ws.send(json.dumps(payload, ensure_ascii=False))

    async def _call_api(self, action: str, params: dict) -> dict:
        """Correlate authoritative action responses without blocking ingress."""
        echo = str(next(self._sequence))
        future = asyncio.get_running_loop().create_future()
        self._pending[echo] = future
        try:
            await self._write({"action": action, "params": params, "echo": echo})
            result = await asyncio.wait_for(future, self.config.api_timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ChannelError(f"QQ action has no confirmed response ({type(exc).__name__})") from None
        finally:
            self._pending.pop(echo, None)
        if result.get("status") != "ok" or result.get("retcode") != 0:
            raise _OneBotRejected("OneBot rejected the action")
        data = result.get("data")
        if not isinstance(data, dict):
            raise ChannelError("OneBot returned an invalid action receipt")
        return data

    async def _busy(self, event: ChannelEvent) -> None:
        """Report rejected admission without waiting for an API response in reader."""
        action = "send_private_msg" if event.is_direct else "send_group_msg"
        target = "user_id" if event.is_direct else "group_id"
        await self._write({"action": action, "params": {
            target: int(event.room_id), "message": [{"type": "text", "data": {
                "text": "The assistant queue is full; please retry this request shortly."}}]},
            "echo": f"busy-{next(self._sequence)}"})

    async def _read(self, socket) -> None:
        """Route action echoes first; execution callbacks always use a separate queue."""
        try:
            async for raw in socket:
                try:
                    payload = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if not isinstance(payload, dict):
                    continue
                echo = payload.get("echo")
                if echo is not None:
                    future = self._pending.get(str(echo))
                    if future is not None and not future.done():
                        future.set_result(payload)
                    continue
                event = self.decode_event(payload)
                if event is None:
                    continue
                durable = await self._accept(event)
                try:
                    ready = self._offer(event)
                except asyncio.QueueFull:
                    if not durable:
                        await self._busy(event)
                    continue
                if ready is not None:
                    ready.set()
        finally:
            self._fail_pending()

    def _fail_pending(self) -> None:
        """An interrupted socket cannot supply receipts for its outstanding actions."""
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ChannelError("QQ connection interrupted before action receipt"))

    async def run(self, handler: Handler) -> None:
        """Reconnect the OneBot socket and refresh the authenticated bot identity."""
        if self._closed or self._worker is not None:
            raise ChannelError("QQ channel is already running or closed")
        self._worker = asyncio.create_task(self._dispatch(handler))
        delay = 1.0
        try:
            while not self._stop.is_set():
                try:
                    headers = ({"Authorization": f"Bearer {self.config.access_token}"}
                               if self.config.access_token else None)
                    self._ws = await self._connect(self.config.ws_url, headers)
                    self._reader = asyncio.create_task(self._read(self._ws))
                    login = await self._call_api("get_login_info", {})
                    bot = str(login.get("user_id") or "")
                    if not bot or (self.config.self_id and bot != self.config.self_id):
                        raise ChannelError("OneBot identity does not match configured self_id")
                    self._self_id = bot
                    self.connected = True
                    delay = 1.0
                    await self._reader
                except asyncio.CancelledError:
                    if self._stop.is_set():
                        break
                    raise
                except Exception as exc:
                    log.warning("QQ connection interrupted (%s)", type(exc).__name__)
                finally:
                    self.connected = False
                    if self._reader is not None:
                        self._reader.cancel()
                        try:
                            await self._reader
                        except asyncio.CancelledError:
                            pass
                        except Exception:
                            pass
                        self._reader = None
                    if self._ws is not None:
                        await self._ws.close()
                        self._ws = None
                    self._fail_pending()
                if not self._stop.is_set():
                    await self._backoff(delay)
                    delay = min(delay * 2, 60)
        finally:
            await self.close()

    async def send(self, event: ChannelEvent, text: str) -> DeliveryReceipt:
        """Use explicit OneBot message segments so model output cannot inject CQ."""
        if (event.channel != "qq_onebot" or event.account_id != self.config.account_id
                or event.owner_id != self.config.owner_id
                or event.sender_id not in self.config.allowed_users
                or (self._self_id and event.workspace_id != self._self_id)
                or (not event.is_direct and self.config.allowed_groups
                    and event.room_id not in self.config.allowed_groups)):
            return DeliveryReceipt("failed", error="QQ event does not belong to this channel")
        chunks = split_text(text, 1800)
        if not chunks:
            return DeliveryReceipt("failed", error="Cannot send an empty reply")
        action = "send_private_msg" if event.is_direct else "send_group_msg"
        target = "user_id" if event.is_direct else "group_id"
        ids = []
        for chunk in chunks:
            segments = [{"type": "text", "data": {"text": chunk}}]
            if event.message_id:
                segments.insert(0, {"type": "reply", "data": {"id": event.message_id}})
            try:
                data = await self._call_api(action, {target: int(event.room_id), "message": segments})
            except _OneBotRejected as exc:
                return DeliveryReceipt("failed", tuple(ids), str(exc))
            except ChannelError as exc:
                return DeliveryReceipt("unknown", tuple(ids), str(exc))
            message_id = str(data.get("message_id") or "")
            if not message_id:
                return DeliveryReceipt("unknown", tuple(ids), "OneBot omitted the sent message receipt")
            ids.append(message_id)
            self._remember(message_id, chunk, True)
        return DeliveryReceipt("sent", tuple(ids))

    async def close(self) -> None:
        """Stop the reader, pending calls and callback worker without long waits."""
        self.connected = False
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        if self._ws is not None:
            await self._ws.close()
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except asyncio.CancelledError:
                pass
            self._reader = None
        self._fail_pending()
        await self._close_worker()
