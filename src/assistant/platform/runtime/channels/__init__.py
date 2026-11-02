"""Outbound-connected chat adapters with explicit owner admission rules."""

from assistant.platform.runtime.channels.qq import QQChannel, QQConfig
from assistant.platform.runtime.channels.slack import SlackChannel, SlackConfig

__all__ = ["QQChannel", "QQConfig", "SlackChannel", "SlackConfig"]
