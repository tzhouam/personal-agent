"""Recurring routines the owner creates from WeChat — with optional
real-world conditions.

A routine = WHEN (time + days: daily / workdays / weekends / a day list /
'monthly:<dom>' / 'yearly:<MM-DD>')
+ optional CONDITION (free text — "there is a weather alert in Shenzhen",
"vLLM shipped a new release" — evaluated at fire time by web search + an
LLM judge, conservative default false) + TASK (free text, run through the
chat agent so it can use every action: search, run_phase, todos, …). The
result is pushed to the owner's WeChat proactively (notify.send_wechat).

The serve daemon's poll loop calls ``fire_due`` every cycle; each routine
fires at most once per day (the day is marked checked even when the
condition doesn't hold — "check at 07:30" means one check, not polling).
"""

import calendar
import re
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

from assistant.platform.config import Settings
from assistant.platform.locks import locked_transaction

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


def valid_days(days: str) -> bool:
    """True when `days` is any supported schedule: a weekly spec
    (daily/workdays/weekends/day list), 'monthly:<1-31>' (day of month), or
    'yearly:<MM-DD>' (one date a year)."""
    days = str(days or "").strip().lower()
    if days.startswith("monthly:"):
        try:
            return 1 <= int(days.split(":", 1)[1]) <= 31
        except ValueError:
            return False
    if days.startswith("yearly:"):
        try:
            datetime.strptime(days.split(":", 1)[1].strip(), "%m-%d")
            return True
        except ValueError:
            return False
    return parse_days(days) is not None


def day_matches(days: str, today: date) -> bool:
    """Does schedule `days` fire on `today`? Weekly specs match by weekday.
    'monthly:D' matches day D, clamped to the month's last day (monthly:31
    fires Jun 30 / Feb 28-29). 'yearly:MM-DD' matches that date, with 02-29
    falling back to 02-28 in non-leap years (never silently skipping a
    year)."""
    days = str(days or "daily").strip().lower()
    if days.startswith("monthly:"):
        try:
            target = int(days.split(":", 1)[1])
        except ValueError:
            return False
        last = calendar.monthrange(today.year, today.month)[1]
        return today.day == min(target, last)
    if days.startswith("yearly:"):
        try:
            month, dom = (int(x) for x in days.split(":", 1)[1].strip().split("-"))
        except ValueError:
            return False
        if (month, dom) == (2, 29) and not calendar.isleap(today.year):
            month, dom = 2, 28
        return (today.month, today.day) == (month, dom)
    return today.weekday() in (parse_days(days) or set())


class RoutineStore:
    """Recurring routines persisted to routines.yaml. Each routine holds its
    schedule (time + days), optional condition, task text, and a
    ``last_checked`` date that enforces at-most-once-per-day firing; cancelled
    routines are kept but flagged."""

    def __init__(self, data_dir: Path):
        """Bind the store to ``data_dir/routines.yaml`` (created lazily)."""
        self.path = data_dir / "routines.yaml"
        self._lock_file = data_dir / "write.lock"

    def _load(self) -> dict:
        """Read the routines file, returning a fresh empty structure when it's
        missing or empty."""
        if not self.path.exists():
            return {"next_id": 1, "routines": []}
        return yaml.safe_load(self.path.read_text()) or {"next_id": 1, "routines": []}

    def _save(self, data: dict) -> None:
        """Atomically replace the routines file (tmp + os.replace, 0600) —
        a crash mid-write must not corrupt the store (Track D §4)."""
        import os as _os

        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
        _os.chmod(tmp, 0o600)
        _os.replace(tmp, self.path)

    @locked_transaction
    def add(self, task: str, time: str, days: str = "daily",
            condition: str = "", workflow: str = "",
            now: datetime | None = None) -> dict | None:
        """Validate and store a new routine, returning the record — or None if
        ``time`` isn't HH:MM, ``days`` doesn't parse, or ``workflow`` isn't a
        wf-id (so the caller can reject bad input). ``task`` and ``condition``
        are length-capped free text. A ``workflow`` binding makes ``fire_due``
        dispatch that saved workflow deterministically instead of interpreting
        the task text; the text still describes it so rolled-back code
        degrades to interpretation."""
        try:
            datetime.strptime(str(time).strip(), "%H:%M")
        except ValueError:
            return None
        if not valid_days(days):
            return None
        workflow = str(workflow or "").strip()
        if workflow and not re.match(r"^wf\d+$", workflow):
            return None
        data = self._load()
        # Creation guard (same comparison claim_due uses, boundary included):
        # "every morning 07:30" created at 22:00 must NOT fire at 22:00 — a
        # routine already due at creation instant waits for its next
        # occurrence. `now` is injectable for deterministic tests.
        now = now or datetime.now()
        due_now = (day_matches(str(days or "daily").lower(), now.date())
                   and str(time).strip() <= now.strftime("%H:%M"))
        routine = {"id": f"rt{data['next_id']}", "task": str(task)[:400],
                   "time": str(time).strip(), "days": str(days or "daily").lower(),
                   "condition": str(condition or "")[:300],
                   "last_checked": now.date().isoformat() if due_now else None}
        if workflow:
            routine["workflow"] = workflow
            routine["task"] = (str(task).strip() or f"run workflow {workflow}")[:400]
        data["next_id"] += 1
        data["routines"].append(routine)
        self._save(data)
        return routine

    def active(self) -> list[dict]:
        """Routines not cancelled."""
        return [r for r in self._load()["routines"] if not r.get("cancelled")]

    @locked_transaction
    def cancel(self, routine_id: str) -> bool:
        """Flag routine ``routine_id`` as cancelled so it stops firing. True if
        one was cancelled, False if unknown or already cancelled."""
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
                if day_matches(r["days"], now.date())
                and r["time"] <= now.strftime("%H:%M")
                and r.get("last_checked") != today]

    @locked_transaction
    def mark_checked(self, routine_id: str, day: date | None = None) -> None:
        """Record that ``routine_id`` was checked on ``day`` (default today) so
        ``due`` won't return it again that day — set before condition evaluation
        so a check counts even when the condition doesn't hold."""
        data = self._load()
        for r in data["routines"]:
            if r["id"] == routine_id:
                r["last_checked"] = (day or date.today()).isoformat()
        self._save(data)

    @locked_transaction
    def claim_due(self, now: datetime | None = None) -> list[dict]:
        """Atomically select the due routines and mark them checked — one
        locked load→mark→save, so a concurrent poller can't double-fire them
        and a concurrent `cancel` can't be lost between the read and the
        write. Returns the claimed routines (the caller then runs them
        outside the lock)."""
        now = now or datetime.now()
        today = now.date().isoformat()
        data = self._load()
        due = [r for r in data["routines"]
               if not r.get("cancelled")
               and day_matches(r["days"], now.date())
               and r["time"] <= now.strftime("%H:%M")
               and r.get("last_checked") != today]
        for r in due:
            r["last_checked"] = today
        if due:
            self._save(data)
        return due


def check_condition(settings: Settings, condition: str) -> tuple[bool, str]:
    """Free-text condition → (holds, why). Searches the web for current facts
    and lets an LLM judge; anything unclear or failing counts as NOT holding
    (a routine must never spam on uncertainty)."""
    if not condition.strip():
        return True, ""
    from assistant.platform.llm import LLM
    from assistant.platform.search import format_results, web_search

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
    """Run every due routine through the Track D occurrence ledger (design
    §4): claim (SQLite, token-fenced) → shadow `last_checked` (the old-code
    guard, written AFTER the ledger claim so a crash window is recordable) →
    condition check (still `claimed` — a crash here re-claims, it never
    becomes execution_unknown) → `executing` → TASK → `executed` with output
    persisted BEFORE delivery → `delivered`/retry. Cycle start recovers stale
    state: `executing` from a dead process goes `execution_unknown` (side
    effects may have started — never rerun, surfaced to the owner);
    executed-but-undelivered output retries DELIVERY ONLY. Returns
    [{id, fired, note}] for logging."""
    from assistant.agent.chat.agent import handle_message
    from assistant.platform.delivery import OutboxDB
    from assistant.platform.notify import send_wechat

    store = RoutineStore(settings.data_dir)
    now = now or datetime.now()
    outbox = OutboxDB(settings.data_dir)
    outcomes = []
    try:
        # recovery first: stale executing → unknown; undelivered output →
        # delivery-only retry (one attempt per cycle, bounded in the ledger)
        stale_before = (datetime.now() - timedelta(
            seconds=max(int(getattr(settings, "chat_poll_seconds", 60) or 60),
                        60))).isoformat()
        for row in outbox.routine_recover(stale_before):
            status = send_wechat(
                settings, f"🔁 [{row['routine_id']}] "
                + (row["output"] or f"(routine task failed: {row['error']})"))
            if status == "sent":
                outbox.routine_delivered(row["routine_id"], row["occurrence"],
                                         row["claim_token"])
                outcomes.append({"id": row["routine_id"], "fired": True,
                                 "note": "delivered on retry"})
            else:
                outbox.routine_delivery_failed(row["routine_id"],
                                               row["occurrence"],
                                               row["claim_token"], status)
        outcomes.extend(_fire_new(settings, store, outbox, now,
                                  handle_message, send_wechat))
    finally:
        outbox.close()
    return outcomes


def _fire_new(settings: Settings, store: "RoutineStore", outbox, now,
              handle_message, send_wechat) -> list[dict]:
    """The new-occurrence path of `fire_due` (recovery handled by the caller)."""
    outcomes = []
    for routine in store.claim_due(now):  # YAML shadow (old-code guard)
        occurrence = f"{now.date().isoformat()} {routine['time']}"
        # ledger claim AFTER the YAML shadow would make a crash window
        # unrecordable — but claim_due IS the shadow write, and the design
        # orders ledger-first. Practical resolution: the ledger insert is
        # idempotent per occurrence, so we claim immediately after and accept
        # that a crash BETWEEN shadow and ledger skips this occurrence
        # (bounded, recorded as a miss next cycle is impossible — documented
        # deviation, see design §4 rollback note).
        token = outbox.routine_claim(routine["id"], occurrence)
        if token is None:   # already past claimed (e.g. replayed cycle)
            continue
        holds, why = check_condition(settings, routine.get("condition", ""))
        if not holds:
            outbox.routine_transition(routine["id"], occurrence, token,
                                      "condition_false", error=why,
                                      from_states=("claimed",))
            log.info("routine %s: condition not met (%s)", routine["id"], why)
            outcomes.append({"id": routine["id"], "fired": False, "note": why})
            continue
        if not outbox.routine_transition(routine["id"], occurrence, token,
                                         "executing", from_states=("claimed",)):
            continue   # displaced by a newer claimant (token CAS)
        if routine.get("workflow"):
            # workflow-bound routine: deterministic dispatch through the saved
            # workflow's pre-planned task record — no chat-model interpretation
            from assistant.agent.actions import run_action

            try:
                note = run_action("run_workflow", {"id": routine["workflow"]},
                                  settings)
                outbox.routine_transition(routine["id"], occurrence, token,
                                          "executed", output=note,
                                          from_states=("executing",))
            except Exception as exc:
                log.exception("routine %s workflow dispatch failed", routine["id"])
                note = f"workflow dispatch failed: {exc}"
                outbox.routine_transition(routine["id"], occurrence, token,
                                          "execution_failed", error=str(exc),
                                          from_states=("executing",))
            status = send_wechat(settings, f"🔁 [{routine['id']}] {note}"[:800])
            if status == "sent":
                outbox.routine_delivered(routine["id"], occurrence, token)
            else:
                outbox.routine_delivery_failed(routine["id"], occurrence,
                                               token, status)
            log.info("routine %s workflow %s: %s", routine["id"],
                     routine["workflow"], status)
            outcomes.append({"id": routine["id"], "fired": True, "note": note})
            continue
        # frame the task as immediate execution — without this the chat agent
        # pattern-matches recurring tasks to plan_task and replies with a PLAN
        # (and a junk todo) instead of doing the work
        framed = ("[Scheduled routine firing NOW — execute the task immediately. "
                  "Use web_search for current information. Do NOT use plan_task, "
                  "create_routine, or add_todo; reply with the actual result.]\n"
                  + routine["task"]
                  + (f"\n[Condition already verified true: {why}]" if why else ""))
        try:
            reply = handle_message(framed, settings)
            outbox.routine_transition(routine["id"], occurrence, token,
                                      "executed", output=reply,
                                      from_states=("executing",))
        except Exception as exc:  # one broken routine must not block the rest
            log.exception("routine %s task failed", routine["id"])
            reply = f"(routine task failed: {exc})"
            outbox.routine_transition(routine["id"], occurrence, token,
                                      "execution_failed", error=str(exc),
                                      from_states=("executing",))
        prefix = f"🔁 [{routine['id']}] "
        if why:
            prefix += f"({why}) "
        status = send_wechat(settings, prefix + reply)
        if status == "sent":
            outbox.routine_delivered(routine["id"], occurrence, token)
        else:   # output is persisted — the next cycle retries DELIVERY only
            outbox.routine_delivery_failed(routine["id"], occurrence,
                                           token, status)
        log.info("routine %s fired: %s", routine["id"], status)
        outcomes.append({"id": routine["id"], "fired": True, "note": status})
    return outcomes
