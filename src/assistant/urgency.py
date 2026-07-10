"""Todo urgency score — one number driving calendar eligibility, list order,
and expiry (researched 2026-07-09; weights follow Taskwarrior's battle-tested
proportions: due 12 > blocking 8 > priority 6 > age 2 > source 1.5, with the
age horizon rescaled from Taskwarrior's 365 days to our 30-day lifecycle).

Key design point: committed items (due date, red priority, or someone waiting
on the owner) AGE UP and never silently expire; speculative undated items
DECAY OUT via the staleness factor (stale-bot convention: fade after 14 days
of no activity, gone at 30, warning surfaced from day 21).
"""

from datetime import date, datetime

_PRIORITY_W = {"red": 1.0, "yellow": 0.5}
# someone is waiting on the owner — the strongest non-deadline signal
_BLOCKING_W = {"review_requested": 1.0, "mention": 0.6, "assign": 0.6, "assigned": 0.6}
# external asks outrank self-generated notes
_SOURCE_W = {"github": 1.0, "chat": 0.6, "resume": 0.4, "manual": 0.3}

STALE_EXEMPT = "committed (due/red/blocking) — ages up instead of expiring"
FADE_START, FADE_END = 14, 30  # days without activity: fade begins / item dies
WARN_AT = 21                   # "going stale" from here on


def _day(value) -> date | None:
    """Parse the leading ``YYYY-MM-DD`` of ``value`` to a date, or None if
    empty/unparseable — todo dates are best-effort strings."""
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date() if value else None
    except ValueError:
        return None


def _due_ramp(due: date | None, today: date) -> float:
    """Taskwarrior's ramp: 0.2 floor for any dated item, linear to 1.0 across
    the 21-day window from due-14 to due+7, saturated past a week overdue."""
    if due is None:
        return 0.0
    days_overdue = (today - due).days
    if days_overdue >= 7:
        return 1.0
    if days_overdue >= -14:
        return 0.2 + 0.8 * (days_overdue + 14) / 21
    return 0.2


def _blocking(todo: dict) -> float:
    """Weight [0,1] for someone waiting on the owner. Resume approvals block by
    definition; otherwise the strongest ``_BLOCKING_W`` keyword found in the
    todo's ``action`` wins (review request > mention/assign), else 0.0."""
    if todo.get("source") == "resume":  # approval gate waits on the owner
        return 0.6
    action = str(todo.get("action") or "").lower()
    return next((w for key, w in _BLOCKING_W.items() if key in action), 0.0)


def _priority(todo: dict) -> float:
    """Priority weight: red 1.0, else yellow 0.5 (the default for an
    unset/unknown priority)."""
    return _PRIORITY_W.get(str(todo.get("priority", "")).lower(), _PRIORITY_W["yellow"])


def is_committed(todo: dict, today: date | None = None) -> bool:
    """Committed items are exempt from staleness decay — but a due date only
    protects until 30 days past due; after that even scheduled work is dead."""
    if _priority(todo) >= 1.0 or _blocking(todo) > 0:
        return True
    due = _day(todo.get("due"))
    return due is not None and ((today or date.today()) - due).days <= FADE_END


def staleness(todo: dict, today: date | None = None) -> float:
    """1.0 = fully alive; fades linearly to 0.0 between FADE_START and
    FADE_END days without activity; 0.0 = expired. Activity anchor is the
    creation date (todos are write-once today; a `touched` field would slot
    in here if editing is ever added)."""
    today = today or date.today()
    if _priority(todo) >= 1.0 or _blocking(todo) > 0:
        return 1.0
    due = _day(todo.get("due"))
    if due is not None:
        overdue = (today - due).days
        if overdue <= FADE_END:
            return 1.0
        return 0.0  # scheduled, but a month past due with no touch: dead
    created = _day(todo.get("created"))
    if created is None:
        return 1.0
    idle = (today - created).days
    if idle <= FADE_START:
        return 1.0
    return max(0.0, 1.0 - (idle - FADE_START) / (FADE_END - FADE_START))


def going_stale(todo: dict, today: date | None = None) -> bool:
    """True while an item is fading (warn from WARN_AT days) but not yet dead."""
    today = today or date.today()
    if is_committed(todo, today):
        return False
    created = _day(todo.get("created"))
    if created is None:
        return False
    return WARN_AT <= (today - created).days < FADE_END


def urgency(todo: dict, today: date | None = None) -> float:
    """The score: higher = more urgent. Reference points: red review-request
    due today ≈ 23; fresh yellow manual note ≈ 3.5; day-25 untouched manual
    note ≈ 1.2 and about to expire."""
    today = today or date.today()
    due = _day(todo.get("due"))
    created = _day(todo.get("created"))
    age_days = max(0, (today - created).days) if created else 0

    score = (
        6.0 * _priority(todo)
        + 12.0 * _due_ramp(due, today)
        + 8.0 * _blocking(todo)
        + 1.5 * _SOURCE_W.get(str(todo.get("source", "")).lower(), 0.3)
        + 2.0 * min(age_days / 30, 1.0)
    ) * staleness(todo, today)
    if due is not None and 0 < (today - due).days < 7:
        score += 2.0  # freshly overdue must outrank everything but red+blocking
    return round(score, 2)
