"""Bounded dispatch and lossless text splitting shared by chat transports."""

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import logging
from typing import Awaitable, Callable

from assistant.platform.runtime.contracts import ChannelEvent

log = logging.getLogger("assistant.channels")
Handler = Callable[[ChannelEvent], Awaitable[None]]


class ChannelError(RuntimeError):
    """A transport failed without exposing remote responses or credentials."""


def validate_identity(account_id: str, owner_id: str, allowed_users: tuple[str, ...],
                      queue_size: int) -> None:
    """Require an explicit owner admission list before opening a connection."""
    if not account_id.strip() or not owner_id.strip():
        raise ValueError("channel account_id and owner_id are required")
    if not allowed_users or any(not str(user).strip() or user == "*" for user in allowed_users):
        raise ValueError("channel allowed_users must name specific authorized users")
    if not 1 <= queue_size <= 1000:
        raise ValueError("channel queue_size must be between 1 and 1000")


def split_text(text: str, limit: int) -> list[str]:
    """Split at natural boundaries while preserving every input character."""
    if not text:
        return []
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + limit, len(text))
        if end < len(text):
            newline = text.rfind("\n", start, end)
            if newline >= start:
                end = newline + 1
        chunks.append(text[start:end])
        start = end
    return chunks


@dataclass
class _PendingEvent:
    """Keep a callback behind its provider acknowledgement when applicable."""

    event: ChannelEvent
    ready: asyncio.Event


class QueuedChannel:
    """Keep the socket reader independent from slow model and tool execution."""

    def __init__(self, queue_size: int):
        """Create bounded queues; start no network or worker in a constructor."""
        self._queue: asyncio.Queue[_PendingEvent] = asyncio.Queue(queue_size)
        self._stop = asyncio.Event()
        self._worker: asyncio.Task | None = None
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._closed = False
        self._acceptor: Handler | None = None
        self.connected = False

    def set_acceptor(self, acceptor: Handler | None) -> None:
        """Install a fast durable inbox sink; it must never run inference or tools.

        The transport awaits this sink before acknowledging provider input.
        Once persisted, a full in-memory execution queue does not lose the
        event: the host must drain its durable inbox independently.
        """
        self._acceptor = acceptor

    async def _accept(self, event: ChannelEvent) -> bool:
        """Return whether the configured host has durably accepted this event."""
        if self._acceptor is None:
            return False
        await self._acceptor(event)
        return True

    def _offer(self, event: ChannelEvent) -> asyncio.Event | None:
        """Reserve capacity without blocking the socket reader on execution."""
        if event.dedup_key in self._seen:
            return None
        ready = asyncio.Event()
        self._queue.put_nowait(_PendingEvent(event, ready))
        self._seen[event.dedup_key] = None
        if len(self._seen) > 4096:
            self._seen.popitem(last=False)
        return ready

    async def _dispatch(self, handler: Handler) -> None:
        """Run callbacks serially; host persistence provides restart deduplication."""
        while True:
            item = await self._queue.get()
            try:
                await item.ready.wait()
                await handler(item.event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Exception messages can carry SDK requests or authentication URLs.
                log.error("channel handler failed (%s)", type(exc).__name__)
                self._seen.pop(item.event.dedup_key, None)
            finally:
                self._queue.task_done()

    async def _backoff(self, delay: float) -> None:
        """Allow shutdown to interrupt reconnect delay immediately."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

    async def _close_worker(self) -> None:
        """Cancel a callback on explicit shutdown rather than leaving orphan work."""
        self._stop.set()
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None


async def connect_socket(url: str, headers: dict[str, str] | None = None):
    """Load the shared WebSocket dependency only when a channel is started."""
    from websockets.asyncio.client import connect

    return await connect(url, additional_headers=headers, max_size=1024 * 1024,
                         open_timeout=20, ping_interval=20, ping_timeout=20)
