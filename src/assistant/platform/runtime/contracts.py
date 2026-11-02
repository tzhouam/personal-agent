"""Small shared contracts; transport and provider implementations stay replaceable."""

from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Any, Protocol
import hashlib
import json


@dataclass(frozen=True)
class ToolCall:
    """A typed model request whose handler is resolved by the host registry."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class BackendRequest:
    """A Pi session or single model completion, scoped to an explicit workspace."""

    messages: list[dict]
    system: str = ""
    tools: list[dict] = field(default_factory=list)
    cwd: Path = field(default_factory=Path.cwd)
    session_id: str | None = None
    timeout: float = 120
    cancel: Event | None = None
    allow_write: bool = False
    max_tokens: int = 4096
    images: list[str | dict] = field(default_factory=list)
    thinking_level: str = ""
    controller_mode: bool = False


@dataclass
class Completion:
    """Normalized output; success requires an authoritative terminal result."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    session_id: str | None = None
    usage: dict = field(default_factory=dict)
    finish_reason: str = "stop"


@dataclass(frozen=True)
class BackendConfig:
    """A model route; credentials are never included in its representation."""

    kind: str = "codex"
    model: str = ""
    command: tuple[str, ...] = ()
    base_url: str = ""
    api_key: str = field(default="", repr=False)
    timeout: float = 120
    allow_unrestricted: bool = False
    api: str = ""
    provider: str = ""
    thinking_level: str = ""
    context_window: int = 128000


class Backend(Protocol):
    """Single-completion transport retained for adapters and injected test seams."""

    kind: str
    capabilities: frozenset[str]

    def complete(self, request: BackendRequest) -> Completion:
        """Produce one bounded result or raise a clear backend error."""
        ...


@dataclass(frozen=True)
class ChannelEvent:
    """Trusted adapter envelope; owner_id comes from configuration, not payload."""

    channel: str
    account_id: str
    event_id: str
    sender_id: str
    room_id: str
    text: str
    owner_id: str = "default"
    workspace_id: str = ""
    thread_id: str = ""
    message_id: str = ""
    is_direct: bool = False
    addressed: bool = False
    metadata: dict = field(default_factory=dict)

    @property
    def session_key(self) -> str:
        """Separate conversations across owners, transports, rooms and threads."""
        values = [self.owner_id, self.channel, self.account_id, self.workspace_id,
                  self.room_id, self.thread_id, self.sender_id]
        return hashlib.sha256(json.dumps(values).encode()).hexdigest()

    @property
    def dedup_key(self) -> str:
        """Retain account and workspace identity across reconnects and restarts."""
        values = [self.owner_id, self.channel, self.account_id, self.workspace_id,
                  self.event_id]
        return hashlib.sha256(json.dumps(values).encode()).hexdigest()


@dataclass(frozen=True)
class DeliveryReceipt:
    """Record uncertain sends explicitly; never replay an agent to resend text."""

    status: str
    message_ids: tuple[str, ...] = ()
    error: str = ""
