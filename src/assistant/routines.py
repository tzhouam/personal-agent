"""Recurring routines the owner creates from WeChat — with optional
real-world conditions.

A routine = WHEN (time + days: daily / workdays / weekends / a day list)
+ optional CONDITION (free text — "there is a weather alert in Shenzhen",
"vLLM shipped a new release" — evaluated at fire time by web search + an
LLM judge, conservative default false) + TASK (free text, run through the
chat agent so it can use every action: search, run_phase, todos, …). The
result is pushed to the owner's WeChat proactively (notify.send_wechat).

The serve daemon's poll loop calls ``fire_due`` every cycle; each routine
fires at most once per day (the day is marked checked even when the
condition doesn't hold — "check at 07:30" means one check, not polling).
"""

import logging
from datetime import date, datetime
from pathlib import Path

import yaml

from .config import Settings

log = logging.getLogger("assistant")

_DAY_GROUPS = {
    "daily": set(range(7)),
    "workdays": {0, 1, 2, 3, 4},
    "weekends": {5, 6},
}
_DAY_NAMES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

_CONDITION_SYSTEM = """You judge whether a condition CURRENTLY holds, using the web search results
below. Be conservative: if the results don't clearly establish the condition, it does not hold.
Respond with ONLY JSON: {"holds": true|false, "why": "<one short sentence>"}"""


def parse_days(days: str) -> set[int] | None:
    """'daily'/'workdays'/'weekends' or 'mon,wed,fri' → weekday set; None if invalid."""
    days = str(days or "daily").strip().lower()
    if days in _DAY_GROUPS:
        return _DAY_GROUPS[days]
    parsed = {_DAY_NAMES.get(d.strip()[:3]) for d in days.split(",")}
    return None if None in parsed or not parsed else parsed


class RoutineStore:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "routines.yaml"

    def _load(self) -> dict:
        if not self.path.exists():
            return {"next_id": 1, "routines": []}
        return yaml.safe_load(self.path.read_text()) or {"next_id": 1, "routines": []}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))

    def add(self, task: str, time: str, days: str = "daily",
            condition: str = "") -> dict | None:
        try:
            datetime.strptime(str(time).strip(), "%H:%M")
        except ValueError:
            return None
        if parse_days(days) is None:
            return None
        data = self._load()
        routine = {"id": f"rt{data['next_id']}", "task": str(task)[:400],
                   "time": str(time).strip(), "days": str(days or "daily").lower(),
                   "condition": str(condition or "")[:300], "last_checked": None}
        data["next_id"] += 1
        data["routines"].append(routine)
        self._save(data)
        return routine

    def active(self) -> list[dict]:
        return [r for r in self._load()["routines"] if not r.get("cancelled")]

    def cancel(self, routine_id: str) -> bool:
        data = self._load()
        for r in data["routines"]:
            if r["id"] == routine_id and not r.get("cancelled"):
                r["cancelled"] = True
                self._save(data)
                return True
        return False

    def due(self, now: datetime | None = None) -> list[dict]:
        """Routines whose day matches, whose time has passed, and which
        haven't been checked today."""
        now = now or datetime.now()
        today = now.date().isoformat()
        return [r for r in self.active()
                if now.weekday() in (parse_days(r["days"]) or set())
                and r["time"] <= now.strftime("%H:%M")
                and r.get("last_checked") != today]

    def mark_checked(self, routine_id: str, day: date | None = None) -> None:
        data = self._load()
        for r in data["routines"]:
            if r["id"] == routine_id:
                r["last_checked"] = (day or date.today()).isoformat()
        self._save(data)


def check_condition(settings: Settings, condition: str) -> tuple[bool, str]:
    """Free-text condition → (holds, why). Searches the web for current facts
    and lets an LLM judge; anything unclear or failing counts as NOT holding
    (a routine must never spam on uncertainty)."""
    if not condition.strip():
        return True, ""
    from .llm import LLM
    from .search import format_results, web_search

    results = web_search(condition, max_results=6, settings=settings)
    try:
        verdict = LLM(settings).complete_json(
            f"Today is {datetime.now():%Y-%m-%d %H:%M}.\n\n"
            f"## Condition\n{condition}\n\n"
            f"## Web search results\n{format_results(results)}",
            system=_CONDITION_SYSTEM, max_tokens=4000)
        return bool(verdict.get("holds")), str(verdict.get("why", ""))[:200]
    except Exception as exc:
        log.warning("condition check failed for %r: %s", condition, exc)
        return False, f"check failed: {exc}"


def fire_due(settings: Settings, now: datetime | None = None) -> list[dict]:
    """Run every due routine: gate on its condition, execute the task through
    the chat agent (full action set), push the result to WeChat. Returns
    [{id, fired, note}] for logging."""
    from .chat.agent import handle_message
    from .notify import send_wechat

    store = RoutineStore(settings.data_dir)
    outcomes = []
    for routine in store.due(now):
        store.mark_checked(routine["id"], (now or datetime.now()).date())
        holds, why = check_condition(settings, routine.get("condition", ""))
        if not holds:
            log.info("routine %s: condition not met (%s)", routine["id"], why)
            outcomes.append({"id": routine["id"], "fired": False, "note": why})
            continue
        try:
            reply = handle_message(routine["task"], settings)
        except Exception as exc:  # one broken routine must not block the rest
            log.exception("routine %s task failed", routine["id"])
            reply = f"(routine task failed: {exc})"
        prefix = f"🔁 [{routine['id']}] "
        if why:
            prefix += f"({why}) "
        status = send_wechat(settings, prefix + reply)
        log.info("routine %s fired: %s", routine["id"], status)
        outcomes.append({"id": routine["id"], "fired": True, "note": status})
    return outcomes
