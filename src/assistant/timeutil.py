"""Temporal anchor — the runtime's "what time is it" for LLM prompts.

An LLM has no clock: without an injected anchor it guesses the date from
training data or stale prompt text. `temporal_anchor()` renders one line of
current **system-local** time (aware, with UTC offset and weekday) that
`llm.LLM._call` appends to the tail of every user prompt — tail so the static
prompt prefix (and its provider-side KV cache) is untouched.

System-local only, deliberately: reminders, routines, schedule gates, and
persisted records all run on naive system-local time, so anchoring prompts to
any *other* timezone would split what the model reads from what the runtime
executes (a "14:00" reminder written from a shifted anchor fires hours off).
On a host pinned to UTC, set the process `TZ` env var — that moves the anchor
and the schedulers together.
"""

from datetime import datetime


def _now() -> datetime:
    """The module's single clock read (aware, system-local) — a seam so tests
    monkeypatch this instead of comparing against the live clock."""
    return datetime.now().astimezone()


def temporal_anchor(now: datetime | None = None) -> str:
    """One line of current time, minute granularity:
    ``[temporal anchor] Now: 2026-07-17 09:32 +0800 (Friday, CST)``.

    Weekday so "tomorrow"/"next Monday" resolve; offset + tz name from the
    aware datetime (the name may be an abbreviation or an offset repeat
    depending on platform — informational only, never parsed)."""
    dt = now or _now()
    name = dt.tzname() or ""
    tail = f" ({dt:%A}, {name})" if name else f" ({dt:%A})"
    return f"[temporal anchor] Now: {dt:%Y-%m-%d %H:%M} {dt:%z}{tail}"
