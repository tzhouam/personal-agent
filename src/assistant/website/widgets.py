"""Interactive-list renderers: the todo calendar + scroll list, the reading
list, the routines/reminders view, and the shared `_todo_li` row. These carry
the owner-only pin/done/unrelated controls (data-tid drives the localStorage
marks in `templates._JS`). Urgency ordering and the calendar gate come from
`..urgency`.
"""

import calendar as _calendar
import html
from datetime import date, datetime

from ..todo_store import group_todos
from ..urgency import going_stale, urgency
from ..utils import ref_label


def _parse_day(value) -> date | None:
    """Parse a YYYY-MM-DD-ish value (only the first 10 chars) to a `date`, or
    None on empty/malformed input."""
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date() if value else None
    except ValueError:
        return None


_CAL_MAX_PER_DAY = 3
_CAL_URGENCY = 8.0  # Eisenhower-Q1 gate: priority alone doesn't clear it —
                    # it takes a deadline or someone waiting on the owner


def _cal_important(todos: list[dict], today: date) -> list[dict]:
    """Only the most important todos earn a calendar cell: anything dated (a
    calendar is for dates) or urgency above the gate. Everything else lives
    only in the scroll list below."""
    return [t for t in todos if t.get("due") or urgency(t, today) >= _CAL_URGENCY]


def _todo_sort_key(today: date):
    """Most urgent first; older-first tiebreak so equal-urgency items keep a
    stable, seniority-respecting order."""
    def key(todo: dict):
        """Sort key: descending urgency, then ascending creation day (undated
        items sort as oldest)."""
        created = _parse_day(todo.get("created"))
        return (-urgency(todo, today), created.toordinal() if created else 0)
    return key


def _render_calendar(todos: list[dict], today: date) -> str:
    """Render the todos page: a month grid showing only the important todos
    (`_cal_important`, ≤3 per day), then the full urgency-sorted open-todo list
    grouped into collapsible kind sections (PR reviews / issues / CI /
    personal). `today` anchors the month and the urgency scores."""
    e = html.escape
    # calendar shows only the important todos: due date if set, else created day
    by_day: dict[int, list[dict]] = {}
    for todo in _cal_important(todos, today):
        anchor = _parse_day(todo.get("due")) or _parse_day(todo.get("created"))
        if anchor and anchor.year == today.year and anchor.month == today.month:
            by_day.setdefault(anchor.day, []).append(todo)

    parts = [f"<section id='todos' class='card'><h2>Todo Calendar — {today.strftime('%B %Y')}</h2>"
             "<p class='cal-note'>Key items only — the full list is below.</p><table class='cal'>",
             "<tr>" + "".join(f"<th>{d}</th>" for d in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")) + "</tr>"]
    for week in _calendar.Calendar().monthdayscalendar(today.year, today.month):
        parts.append("<tr>")
        for day in week:
            if day == 0:
                parts.append("<td class='off'></td>")
                continue
            classes = "today" if day == today.day else ""
            cell = f"<b>{day}</b>"
            day_todos = by_day.get(day, [])
            for todo in day_todos[:_CAL_MAX_PER_DAY]:
                kind = "todo due" if todo.get("due") else "todo"
                cell += (f"<div class='{kind}' data-tid='{e(str(todo.get('id', '')))}'"
                         f" title='{e(todo.get('detail', ''))}'>{e(todo['title'][:40])}</div>")
            if len(day_todos) > _CAL_MAX_PER_DAY:
                cell += f"<div class='more'>+{len(day_todos) - _CAL_MAX_PER_DAY} more</div>"
            parts.append(f"<td class='{classes}'>{cell}</td>")
        parts.append("</tr>")
    parts.append("</table>")

    todos = sorted(todos, key=_todo_sort_key(today))
    if todos:
        # kind sections (PR reviews / issues / CI / personal — same grouping
        # as the digest email); most-urgent-first order within each section
        parts.append(f"<h3>Open todos ({len(todos)})</h3><div class='todo-scroll'>")
        idx = 0
        for label, group_items in group_todos(todos):
            parts.append(f"<details class='t-day' open><summary>{html.escape(label)} "
                         f"<span class='t-count'>({len(group_items)})</span></summary>"
                         "<ul class='todos'>")
            parts.extend(_todo_li(todo, idx + j, today) for j, todo in enumerate(group_items))
            idx += len(group_items)
            parts.append("</ul></details>")
        parts.append("</div>")
        parts.append("<div id='todo-hidden-bar'><span></span> — "
                     "<button id='todo-show-hidden'>show</button></div>")
    parts.append("</section>")
    return "".join(parts)


def _render_reading(reading: list[dict], today: date) -> str:
    """The reading list: a scrollable list of collapsible day groups (by
    surfaced date, newest first — reading is chronological, unlike the
    kind-grouped todos) with owner-only pin/done buttons — reading ids (r#) share the todos' localStorage marks.
    Done/Unrelated act locally and instantly; the marks also queue and push
    to the private marks repo in the background, where the agent collects
    them each run (owner decision 2026-07-10: no more mailto handoff)."""
    parts = ["<section id='reading' class='card'><h2>Reading list</h2>"]
    if not reading:
        parts.append("<p class='empty'>Nothing unread. 📚</p></section>")
        return "".join(parts)

    reading = sorted(reading, key=lambda r: str(r.get("created", "")), reverse=True)
    groups: dict[str, list[dict]] = {}
    for item in reading:
        groups.setdefault(str(item.get("created") or "no date"), []).append(item)

    parts.append(f"<p class='cal-note'>{len(reading)} unread — mark read with "
                 "<code>assistant reading done &lt;id&gt;</code> or the ✓ button.</p>"
                 "<div class='todo-scroll tall'>")
    idx = 0
    for group_i, (day, day_items) in enumerate(groups.items()):
        anchor = _parse_day(day)
        label = f"{day} · {anchor.strftime('%a')}" if anchor else day
        open_attr = " open" if group_i < 5 else ""
        parts.append(f"<details class='t-day'{open_attr}><summary>{label} "
                     f"<span class='t-count'>({len(day_items)})</span></summary>"
                     "<ul class='todos'>")
        for item in day_items:
            entry = {"id": item.get("id"), "title": item.get("title", ""),
                     "url": item.get("url"), "detail": item.get("why", ""),
                     "source": item.get("source", ""), "created": item.get("created", "")}
            parts.append(_todo_li(entry, idx, today, stale_badge=False,
                                  unrelated_btn=True))
            idx += 1
        parts.append("</ul></details>")
    parts.append("</div>")
    parts.append("<div id='todo-hidden-bar'><span></span> — "
                 "<button id='todo-show-hidden'>show</button></div>")
    parts.append("</section>")
    return "".join(parts)


def _render_routines(routines: list[dict], reminders: list[dict]) -> str:
    """Recurring routines + pending one-shot reminders — read-only view
    (create/cancel happens over WeChat: `/routine`, `/remind`)."""
    e = html.escape
    parts = ["<section id='routines' class='card'><h2>Routines</h2>"]
    if not routines and not reminders:
        parts.append("<p class='empty'>No routines yet — create one over WeChat: "
                     "“every workday at 8:30 …”</p></section>")
        return "".join(parts)

    if routines:
        parts.append("<p class='cal-note'>Recurring work the agent runs by itself — "
                     "manage via WeChat (<code>/routine</code>).</p><ul class='routines'>")
        for r in routines:
            gate = (f"<div class='t-detail'>if: {e(r['condition'])}</div>"
                    if r.get("condition") else "")
            checked = (f" · last checked {e(str(r['last_checked']))}"
                       if r.get("last_checked") else "")
            parts.append(
                f"<li><span class='r-when'>🔁 {e(r['days'])} {e(r['time'])}</span> "
                f"<b>{e(r['task'])}</b>"
                f"<span class='when'> [{e(str(r.get('id', '')))}]{checked}</span>{gate}</li>")
        parts.append("</ul>")
    if reminders:
        parts.append("<h3>Pending reminders</h3><ul class='routines'>")
        parts.extend(
            f"<li><span class='r-when'>⏰ {e(r['due_at'])}</span> {e(r['message'])}"
            f"<span class='when'> [{e(str(r.get('id', '')))}]</span></li>"
            for r in reminders)
        parts.append("</ul>")
    parts.append("</section>")
    return "".join(parts)


def _todo_li(todo: dict, idx: int, today: date, stale_badge: bool = True,
             unrelated_btn: bool = False) -> str:
    """Render one todo/reading `<li>` with its owner-only action buttons.
    `idx` seeds the JS stable-sort order; `stale_badge` adds the going-stale
    indicator (todos only); `unrelated_btn` adds the reading-list 🚫 button.
    Carries `data-tid` (the mark id) and a short bracketed source link."""
    e = html.escape
    title = f"<b>{e(todo['title'])}</b>"
    if stale_badge and going_stale(todo, today):  # fading toward auto-expiry
        title += " <span class='t-stale' title='untouched for weeks — will auto-expire soon'>⏳ going stale</span>"
    label = ref_label(todo.get("url"), todo.get("detail", "") or todo.get("title", ""),
                      todo.get("type", ""))
    if label:  # short bracketed link; the summary stays plain text
        title = f"<a href='{e(todo['url'])}'>[{e(label)}]</a>: {title}"
    elif todo.get("url"):
        title = f"<a href='{e(todo['url'])}'>[link]</a>: {title}"
    due = f" · due {e(str(todo['due']))}" if todo.get("due") else ""
    detail = (f"<div class='t-detail'>{e(todo['detail'])}</div>"
              if todo.get("detail") else "")
    unrel = ("<button class='b-unrel' title='Not relevant — hide now and teach "
             "the digest to avoid similar topics'>🚫 Unrelated</button>"
             if unrelated_btn else "")
    actions = ("<span class='t-actions'>"
               "<button class='b-pin' title='Pin to the top of the list'>📌 Pin</button>"
               "<button class='b-done' title='Mark done — hide on this page'>✓ Done</button>"
               f"{unrel}</span>")
    return (f"<li data-tid='{e(str(todo.get('id', '')))}' data-idx='{idx}'>"
            f"{actions}{title} <span class='when'>({e(todo.get('source', ''))}, "
            f"since {e(todo.get('created', ''))}{due})</span>{detail}</li>")
