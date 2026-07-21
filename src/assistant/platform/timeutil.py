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

import re
from datetime import date, datetime, timedelta


def _now() -> datetime:
    """The module's single clock read (aware, system-local) — a seam so tests
    monkeypatch this instead of comparing against the live clock."""
    return datetime.now().astimezone()


_WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def weekday_cn(d: date) -> str:
    """Chinese short weekday (周一…周日) for a date — shown next to record dates
    so the owner catches an off-by-one day at a glance."""
    return _WEEKDAY_CN[d.weekday()]


# fixed relative-day words → day offset from "today"
_REL_DAYS = {
    "今天": 0, "今日": 0, "today": 0,
    "昨天": -1, "昨日": -1, "yesterday": -1,
    "前天": -2, "前日": -2,
    "大前天": -3,
    "明天": 1, "明日": 1, "tomorrow": 1,
    "后天": 2, "大后天": 3,
}
_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def resolve_day(token: str, today: date | None = None) -> str | None:
    """Resolve a day expression to an absolute ``YYYY-MM-DD``, or ``None`` if it
    can't be parsed (the caller must then reject, never fall back to today).

    Handles: an already-absolute ``YYYY-MM-DD`` (validated, passed through);
    fixed relative words (今天/昨天/前天/大前天/明天/后天/today/yesterday/…); and
    counted offsets ``N天前``/``N天后``/``N days ago`` incl. Chinese numerals
    (三天前). Resolution is against system-local ``today`` (injectable for tests)."""
    today = today or _now().date()
    s = str(token or "").strip()
    if not s:
        return None
    try:  # already absolute
        return datetime.strptime(s, "%Y-%m-%d").date().isoformat()
    except ValueError:
        pass
    low = s.lower()
    if low in _REL_DAYS:
        return (today + timedelta(days=_REL_DAYS[low])).isoformat()
    m = re.fullmatch(r"(\d+)\s*天前", s)
    if m:
        return (today - timedelta(days=int(m.group(1)))).isoformat()
    m = re.fullmatch(r"(\d+)\s*天后", s)
    if m:
        return (today + timedelta(days=int(m.group(1)))).isoformat()
    m = re.fullmatch(r"(\d+)\s*days?\s*ago", low)
    if m:
        return (today - timedelta(days=int(m.group(1)))).isoformat()
    m = re.fullmatch(r"([一二两三四五六七八九十]+)\s*天前", s)
    if m and m.group(1) in _CN_NUM:
        return (today - timedelta(days=_CN_NUM[m.group(1)])).isoformat()
    return None


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
