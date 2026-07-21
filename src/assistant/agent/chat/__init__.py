"""Two-way chat: inbound channels (email, WeCom), the message-handling agent,
and the listener daemon that ties them together.

Submodules are imported directly (e.g. ``from .chat.service import
run_listener``); this package does not re-export a flat surface.
"""
