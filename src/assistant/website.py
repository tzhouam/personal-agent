"""Personal website generator + publisher.

The site is a DETERMINISTIC render of profile.yaml + todos — no LLM in the
loop, so nothing fabricated can reach a public page. Publishing pushes
directly to the repo's default branch (owner's explicit choice, 2026-07-02 —
was PR-gated before); remote edits are rebased in first, never force-pushed.

Todo pin/done buttons are client-side only (localStorage keyed by todo id):
the page has no backend, so "done" hides the item in that browser without
touching todos.yaml — the store is still closed by the agent's monitor pass
or `assistant todo done`.

The buttons are owner-only: hidden until owner mode is enabled by opening
todos.html#owner once in a browser (persisted in localStorage; #guest turns
it off). A static page can't truly authenticate, but a visitor who bypasses
this only ever reorders their own browser's view — guests always see the
canonical list because their stored marks are ignored outside owner mode.
"""

import base64
import calendar as _calendar
import html
import subprocess
from datetime import date, datetime

from .urgency import going_stale, urgency
from pathlib import Path

import httpx

from .config import Settings
from .utils import ref_label

_API = "https://api.github.com"


# ── rendering ────────────────────────────────────────────────────────
def render_site(profile: dict, todos: list[dict], today: date | None = None) -> dict[str, str]:
    """Returns {filename: content} for the generated site — one page per section.

    Every page is always rendered (an empty section shows a placeholder) so a
    previously published page never goes stale-but-orphaned in the repo."""
    today = today or date.today()
    ident = profile.get("identity", {})
    e = html.escape
    name = ident.get("name", "")
    photo = ident.get("photo") or (
        f"https://github.com/{ident['github']}.png" if ident.get("github") else ""
    )

    def actives(section):
        return [x for x in profile.get(section, []) if x.get("status", "active") == "active"]

    link_pills = [
        f"<a class='pill' href='{e(link)}'>{e(link.split('//')[-1].rstrip('/'))}</a>"
        for link in ident.get("links", []) if link
    ]
    if ident.get("emails"):
        link_pills.append(f"<a class='pill' href='mailto:{e(ident['emails'][0])}'>✉ email</a>")

    pages = [
        ("index.html", "Home", _about_html(profile) + _skills_html(actives("skills"))),
        ("experience.html", "Experience", _experience_html(profile.get("experience", []))),
        ("education.html", "Education", _education_html(profile.get("education", []))),
        ("projects.html", "Projects", _projects_html(actives("projects"))),
        ("todos.html", "Todos", _render_calendar(todos, today)),
    ]

    files = {"agent-site.css": _CSS, "agent-site.js": _JS}
    for filename, label, body in pages:
        nav = "<nav class='anchors'>" + "".join(
            f"<a href='{fn}'{' class=active' if fn == filename else ''}>{lbl}</a>"
            for fn, lbl, _ in pages
        ) + "</nav>"
        head = (
            "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<meta property='og:title' content='{e(name)}'>"
            f"<meta property='og:image' content='{e(photo)}'>"
            f"<title>{e(name) if filename == 'index.html' else f'{e(name)} — {label}'}</title>"
            "<link rel='icon' href='data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 "
            "viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>🚀</text></svg>'>"
            "<link rel='preconnect' href='https://fonts.googleapis.com'>"
            "<link href='https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&display=swap'"
            " rel='stylesheet'>"
            "<link rel='stylesheet' href='agent-site.css'>"
            "<script src='agent-site.js' defer></script></head><body>"
        )
        if filename == "index.html":  # full hero: photo + personal info
            hero = (
                "<header class='hero'><div class='hero-inner'>"
                + (f"<img class='avatar' src='{e(photo)}' alt='{e(name)}'>" if photo else "")
                + f"<h1>{e(name)}</h1>"
                + f"<p class='tagline'>{e(' · '.join(ident.get('affiliations', [])))}</p>"
                + f"<nav class='pills'>{''.join(link_pills)}</nav>"
                + nav + "</div></header>"
            )
        else:  # compact banner: name links home, same nav
            hero = (
                "<header class='hero compact'><div class='hero-inner'>"
                f"<h1><a href='index.html'>{e(name)}</a></h1>"
                + nav + "</div></header>"
            )
        files[filename] = (
            head + hero + "<main>"
            + (body or "<section class='card'><p class='empty'>Nothing here yet.</p></section>")
            + f"<footer>Maintained automatically by personal-agent · updated {today.isoformat()}"
              "</footer></main></body></html>"
        )
    return files


def _about_html(profile: dict) -> str:
    """Short self-introduction: identity.bio (owner-editable, never touched by the
    LLM — identity is a protected section) with a deterministic fallback composed
    from profile facts, so the section can't fabricate anything."""
    e = html.escape
    bio = str(profile.get("identity", {}).get("bio", "") or "").strip()
    if not bio:
        bio = _fallback_bio(profile)
    if not bio:
        return ""
    paragraphs = "".join(f"<p class='bio'>{e(p.strip())}</p>"
                         for p in bio.split("\n") if p.strip())
    return f"<section id='about' class='card'><h2>About</h2>{paragraphs}</section>"


def _fallback_bio(profile: dict) -> str:
    """One factual sentence straight from the profile when no bio is written."""
    name = profile.get("identity", {}).get("name", "")
    bits = []
    experience = profile.get("experience", [])
    if experience:
        job = experience[0]
        if job.get("title") and job.get("org"):
            bits.append(f"{job['title']} at {job['org']}")
    projects = [p["name"] for p in profile.get("projects", [])
                if p.get("status", "active") == "active" and p.get("name")][:3]
    if projects:
        bits.append("currently working on " + ", ".join(projects))
    if not (name and bits):
        return ""
    return f"{name} — {'; '.join(bits)}."


def _skills_html(skills: list[dict]) -> str:
    e = html.escape
    if not skills:
        return ""
    return ("<section id='skills' class='card'><h2>Skills</h2><p class='chips'>"
            + "".join(f"<span class='chip'>{e(s['name'])}</span>" for s in skills)
            + "</p></section>")


def _experience_html(experience: list[dict]) -> str:
    e = html.escape
    if not experience:
        return ""
    parts = ["<section id='experience' class='card'><h2>Experience</h2><ul class='timeline'>"]
    for job in experience:
        period = job.get("period", {})
        when = f"{period.get('start', '')} – {period.get('end') or 'present'}"
        parts.append(f"<li><div class='t-head'><b>{e(str(job.get('title', '')))}</b>"
                     f" · {e(str(job.get('org', '')))}"
                     f"<span class='when'>{e(when)}</span></div>")
        for h in job.get("highlights", []):
            parts.append(f"<div class='hl'>{e(str(h))}</div>")
        parts.append("</li>")
    parts.append("</ul></section>")
    return "".join(parts)


def _education_html(education: list[dict]) -> str:
    e = html.escape
    if not education:
        return ""
    parts = ["<section id='education' class='card'><h2>Education</h2><ul class='timeline'>"]
    for school in education:
        parts.append(f"<li><div class='t-head'><b>{e(str(school.get('school', '')))}</b>"
                     f" · {e(str(school.get('degree', '')))}"
                     f"<span class='when'>{e(str(school.get('period', '')))}</span></div></li>")
    parts.append("</ul></section>")
    return "".join(parts)


def _projects_html(projects: list[dict]) -> str:
    e = html.escape
    if not projects:
        return ""
    parts = ["<section id='projects' class='card'><h2>Projects</h2><div class='grid'>"]
    for p in projects:
        link = next((str(l) for l in p.get("evidence", []) if str(l).startswith("http")), None)
        title = f"<a href='{e(link)}'>{e(p['name'])}</a>" if link else e(p["name"])
        highlights = "".join(f"<div class='hl'>{e(str(h))}</div>" for h in p.get("highlights", []))
        parts.append(f"<div class='proj'><h3>{title}</h3>"
                     f"<span class='role'>{e(p.get('role', ''))}</span>{highlights}</div>")
    parts.append("</div></section>")
    return "".join(parts)


def _parse_day(value) -> date | None:
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
        created = _parse_day(todo.get("created"))
        return (-urgency(todo, today), created.toordinal() if created else 0)
    return key


def _render_calendar(todos: list[dict], today: date) -> str:
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
        # same-day todos embed into one collapsible group; group order follows
        # the urgency-ordered flat sort (most urgent day first)
        groups: dict[str, list[dict]] = {}
        for todo in todos:
            anchor = _parse_day(todo.get("due")) or _parse_day(todo.get("created"))
            groups.setdefault(anchor.isoformat() if anchor else "no date", []).append(todo)

        parts.append(f"<h3>Open todos ({len(todos)})</h3><div class='todo-scroll'>")
        idx = 0
        for group_i, (day, day_todos) in enumerate(groups.items()):
            anchor = _parse_day(day)
            label = f"{day} · {anchor.strftime('%a')}" if anchor else day
            if any(t.get("due") for t in day_todos):
                label += " · due"
            open_attr = " open" if group_i < 5 else ""  # older days start collapsed
            parts.append(f"<details class='t-day'{open_attr}><summary>{label} "
                         f"<span class='t-count'>({len(day_todos)})</span></summary>"
                         "<ul class='todos'>")
            parts.extend(_todo_li(todo, idx + j, today) for j, todo in enumerate(day_todos))
            idx += len(day_todos)
            parts.append("</ul></details>")
        parts.append("</div>")
        parts.append("<div id='todo-hidden-bar'><span></span> — "
                     "<button id='todo-show-hidden'>show</button></div>")
    parts.append("</section>")
    return "".join(parts)


def _todo_li(todo: dict, idx: int, today: date) -> str:
    e = html.escape
    title = f"<b>{e(todo['title'])}</b>"
    if going_stale(todo, today):  # fading toward the 30-day expiry
        title += " <span class='t-stale' title='untouched for 3+ weeks — expires at 30 days'>⏳ going stale</span>"
    label = ref_label(todo.get("url"), todo.get("detail", "") or todo.get("title", ""),
                      todo.get("type", ""))
    if label:  # short bracketed link; the summary stays plain text
        title = f"<a href='{e(todo['url'])}'>[{e(label)}]</a>: {title}"
    elif todo.get("url"):
        title = f"<a href='{e(todo['url'])}'>[link]</a>: {title}"
    due = f" · due {e(str(todo['due']))}" if todo.get("due") else ""
    detail = (f"<div class='t-detail'>{e(todo['detail'])}</div>"
              if todo.get("detail") else "")
    actions = ("<span class='t-actions'>"
               "<button class='b-pin' title='Pin to the top of the list'>📌 Pin</button>"
               "<button class='b-done' title='Mark done — hide on this page'>✓ Done</button>"
               "</span>")
    return (f"<li data-tid='{e(str(todo.get('id', '')))}' data-idx='{idx}'>"
            f"{actions}{title} <span class='when'>({e(todo.get('source', ''))}, "
            f"since {e(todo.get('created', ''))}{due})</span>{detail}</li>")


_CSS = """*{box-sizing:border-box}
body{font-family:'Inter',-apple-system,'Segoe UI',sans-serif;color:#1e293b;margin:0;
  background:#f1f5f9}
a{color:#4f46e5;text-decoration:none}a:hover{text-decoration:underline}

/* ── hero overlay ── */
.hero{background:linear-gradient(135deg,#0f172a 0%,#312e81 55%,#6d28d9 100%);
  color:#fff;padding:72px 20px 96px;text-align:center;position:relative;overflow:hidden}
.hero::after{content:'';position:absolute;inset:auto 0 -1px 0;height:70px;
  background:#f1f5f9;clip-path:ellipse(75% 100% at 50% 100%)}
.hero-inner{position:relative;z-index:1;max-width:820px;margin:0 auto}
.avatar{width:148px;height:148px;border-radius:50%;object-fit:cover;
  border:4px solid rgba(255,255,255,.85);box-shadow:0 0 0 8px rgba(255,255,255,.12),
  0 18px 40px rgba(0,0,0,.45)}
.hero h1{font-size:2.6rem;font-weight:800;margin:18px 0 4px;letter-spacing:-.02em}
.tagline{color:#c7d2fe;font-size:1.05rem;margin:0 0 18px}
.pills{display:flex;flex-wrap:wrap;gap:10px;justify-content:center;margin-bottom:22px}
.pill{background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.28);color:#fff;
  border-radius:999px;padding:6px 16px;font-size:.9rem;backdrop-filter:blur(4px);
  transition:background .2s}
.pill:hover{background:rgba(255,255,255,.25);text-decoration:none}
.anchors{display:flex;gap:22px;justify-content:center;flex-wrap:wrap}
.anchors a{color:#e0e7ff;font-weight:600;font-size:.92rem;text-transform:uppercase;
  letter-spacing:.08em;padding-bottom:3px;border-bottom:2px solid transparent}
.anchors a.active{color:#fff;border-bottom-color:#a5b4fc}
.anchors a:hover{text-decoration:none;color:#fff}

/* compact banner on section pages — its own shallower curve and card offset:
   the full hero's 70px curve + -48px main pull would swallow the title/nav */
.hero.compact{padding:26px 20px 84px}
.hero.compact::after{height:38px}
.hero.compact+main{margin-top:-26px}
.hero.compact h1{font-size:1.5rem;margin:0 0 14px}
.hero.compact h1 a{color:#fff}
.hero.compact h1 a:hover{text-decoration:none;color:#c7d2fe}
.empty{color:#94a3b8;margin:0}
.bio{color:#475569;line-height:1.65;margin:0 0 10px}
.bio:last-child{margin-bottom:0}

/* ── content cards ── */
main{max-width:860px;margin:-48px auto 0;padding:0 20px 40px;position:relative;z-index:2}
.card{background:#fff;border-radius:18px;box-shadow:0 8px 28px rgba(15,23,42,.08);
  padding:28px 30px;margin-bottom:26px}
h2{margin:0 0 16px;font-size:1.35rem;letter-spacing:-.01em}
h2::after{content:'';display:block;width:44px;height:4px;border-radius:2px;margin-top:8px;
  background:linear-gradient(90deg,#6366f1,#a855f7)}
.chips{margin:0}.chip{display:inline-block;background:linear-gradient(135deg,#eef2ff,#f5f3ff);
  border:1px solid #e0e7ff;color:#4338ca;border-radius:999px;padding:5px 14px;margin:3px;
  font-size:.9rem;font-weight:600}
.when{color:#94a3b8;font-size:.85rem;float:right}
.hl{color:#475569;font-size:.92rem;margin-top:4px;padding-left:14px;position:relative}
.hl::before{content:'›';position:absolute;left:0;color:#a855f7;font-weight:700}

/* timeline */
ul.timeline{list-style:none;margin:0;padding:0 0 0 22px;border-left:2px solid #e0e7ff}
ul.timeline li{margin-bottom:18px;position:relative;padding-left:16px}
ul.timeline li::before{content:'';position:absolute;left:-28px;top:6px;width:10px;height:10px;
  border-radius:50%;background:#6366f1;box-shadow:0 0 0 4px #eef2ff}
.t-head{font-size:1rem}

/* project grid */
.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:16px}
.proj{border:1px solid #e2e8f0;border-radius:14px;padding:16px 18px;
  transition:transform .18s,box-shadow .18s;background:linear-gradient(180deg,#fff,#fafaff)}
.proj:hover{transform:translateY(-4px);box-shadow:0 12px 26px rgba(79,70,229,.14)}
.proj h3{margin:0 0 2px;font-size:1.02rem}
.role{color:#94a3b8;font-size:.82rem;text-transform:uppercase;letter-spacing:.06em}

/* calendar + todos */
table.cal{border-collapse:collapse;width:100%;margin-top:6px}
.cal th{color:#64748b;font-size:.78rem;text-transform:uppercase;letter-spacing:.06em;
  padding-bottom:6px}
.cal td{border:1px solid #eef2f7;vertical-align:top;padding:5px;height:56px;width:14%;
  font-size:.82rem;border-radius:4px}
.cal td.off{background:#f8fafc}
.cal td.today{background:linear-gradient(135deg,#fef9c3,#fef3c7);outline:2px solid #f59e0b}
.cal .todo{background:linear-gradient(135deg,#e0e7ff,#ede9fe);border-left:3px solid #6366f1;
  border-radius:5px;padding:2px 5px;margin-top:3px;font-size:.72rem}
.cal .todo.due{background:linear-gradient(135deg,#fee2e2,#fce7f3);border-left-color:#ef4444}
.cal-note{color:#64748b;font-size:.85rem;margin:2px 0 0}
.cal .more{color:#6366f1;font-size:.72rem;font-weight:600;margin-top:2px}
.todo-scroll{max-height:480px;overflow-y:auto;padding-right:6px;margin-top:6px;
  scrollbar-width:thin;border-bottom:1px solid #e2e8f0}
details.t-day{margin:0 0 8px}
details.t-day summary{cursor:pointer;font-weight:600;color:#334155;font-size:.92rem;
  padding:6px 8px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;
  user-select:none}
details.t-day[open] summary{margin-bottom:6px}
details.t-day summary .t-count{color:#64748b;font-weight:400}
.t-stale{color:#b45309;background:#fef3c7;border-radius:6px;padding:1px 6px;
  font-size:.72rem;font-weight:600}
details.t-day.all-done{display:none}
body.show-hidden details.t-day.all-done{display:block}
ul.todos{list-style:none;padding:0;margin:0}
ul.todos li{border:1px solid #e2e8f0;border-left:4px solid #ef4444;border-radius:10px;
  padding:10px 14px;margin-bottom:8px;background:#fff}
ul.todos .when{float:none;display:inline}
.t-detail{color:#64748b;font-size:.86rem;margin-top:4px}

/* pin / done controls (state lives in this browser's localStorage);
   owner-only — hidden until body.owner is set via the #owner hash */
.t-actions{display:none}
body.owner .t-actions{float:right;display:flex;gap:6px;margin:-2px 0 4px 10px}
.t-actions button,#todo-show-hidden{border:1px solid #e2e8f0;background:#f8fafc;
  border-radius:8px;cursor:pointer;font:inherit;font-size:.78rem;padding:2px 10px;
  color:#64748b;white-space:nowrap;transition:background .15s,color .15s}
.t-actions button:hover,#todo-show-hidden:hover{background:#eef2ff;color:#4338ca}
ul.todos li.pinned{border-left-color:#f59e0b;
  background:linear-gradient(135deg,#fffbeb,#fff)}
ul.todos li.pinned .b-pin{background:#fef3c7;border-color:#fcd34d;color:#92400e}
ul.todos li.done-item{display:none}
body.show-hidden ul.todos li.done-item{display:block;opacity:.55}
.cal .todo.done-chip{display:none}
#todo-hidden-bar{display:none;color:#94a3b8;font-size:.85rem;margin-top:10px}
footer{text-align:center;color:#94a3b8;font-size:.8rem;margin-top:36px}

@media (max-width:600px){.hero h1{font-size:2rem}.when{float:none;display:block}
main{margin-top:-36px}.card{padding:20px 18px}.grid{grid-template-columns:1fr}}
"""


_JS = """(function () {
  var DONE = 'agent-todos-done', PIN = 'agent-todos-pinned', OWNER = 'agent-owner';
  function load(k) { try { return JSON.parse(localStorage.getItem(k) || '[]'); } catch (e) { return []; } }
  function save(k, v) { localStorage.setItem(k, JSON.stringify(v)); }
  function toggle(k, id) {
    var v = load(k), i = v.indexOf(id);
    if (i < 0) v.push(id); else v.splice(i, 1);
    save(k, v);
  }
  // owner mode: visit any page with #owner once to enable in this browser
  // (#guest disables). Guests never see the buttons and their stored marks
  // are ignored, so everyone else always sees the canonical list.
  function readHash() {
    if (location.hash === '#owner') localStorage.setItem(OWNER, '1');
    if (location.hash === '#guest') localStorage.removeItem(OWNER);
  }
  readHash();
  window.addEventListener('hashchange', function () { readHash(); apply(); });
  function isOwner() { return localStorage.getItem(OWNER) === '1'; }

  function apply() {
    var owner = isOwner();
    document.body.classList.toggle('owner', owner);
    var done = owner ? load(DONE) : [], pinned = owner ? load(PIN) : [];
    // calendar chips of done todos disappear too
    document.querySelectorAll('.cal [data-tid]').forEach(function (el) {
      el.classList.toggle('done-chip', done.indexOf(el.dataset.tid) >= 0);
    });
    var lists = Array.prototype.slice.call(document.querySelectorAll('ul.todos'));
    if (!lists.length) return;
    var hidden = 0;
    lists.forEach(function (list) {  // one ul per embedded day group
      var items = Array.prototype.slice.call(list.querySelectorAll('li[data-tid]'));
      // pinned first within their day (keeping relative order), rest as rendered
      items.sort(function (a, b) {
        var pa = pinned.indexOf(a.dataset.tid) >= 0 ? 0 : 1;
        var pb = pinned.indexOf(b.dataset.tid) >= 0 ? 0 : 1;
        return pa - pb || (+a.dataset.idx) - (+b.dataset.idx);
      }).forEach(function (li) { list.appendChild(li); });

      var allDone = items.length > 0;
      items.forEach(function (li) {
        var id = li.dataset.tid;
        var isDone = done.indexOf(id) >= 0, isPin = pinned.indexOf(id) >= 0;
        if (isDone) hidden++; else allDone = false;
        li.classList.toggle('done-item', isDone);
        li.classList.toggle('pinned', isPin);
        var pb = li.querySelector('.b-pin'), db = li.querySelector('.b-done');
        if (pb) pb.textContent = isPin ? '\\ud83d\\udccc Unpin' : '\\ud83d\\udccc Pin';
        if (db) db.textContent = isDone ? '\\u21a9 Restore' : '\\u2713 Done';
      });
      // a day whose todos are all done disappears with them
      var group = list.closest ? list.closest('details.t-day') : null;
      if (group) group.classList.toggle('all-done', allDone);
    });
    var bar = document.getElementById('todo-hidden-bar');
    if (bar) {
      bar.style.display = owner && hidden ? 'block' : 'none';
      bar.querySelector('span').textContent =
        hidden + ' done todo' + (hidden === 1 ? '' : 's') + ' hidden';
    }
  }

  document.addEventListener('click', function (ev) {
    if (!isOwner()) return;
    var btn = ev.target.closest ? ev.target.closest('button') : null;
    if (!btn) return;
    if (btn.id === 'todo-show-hidden') {
      var shown = document.body.classList.toggle('show-hidden');
      btn.textContent = shown ? 'hide' : 'show';
      return;
    }
    var li = btn.closest('li[data-tid]');
    if (!li) return;
    if (btn.classList.contains('b-pin')) toggle(PIN, li.dataset.tid);
    else if (btn.classList.contains('b-done')) toggle(DONE, li.dataset.tid);
    else return;
    apply();
  });
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', apply);
  } else {
    apply();
  }
})();
"""


# ── publishing (direct push to the default branch) ───────────────────
def _auth_flag(settings: Settings) -> list[str]:
    basic = base64.b64encode(f"{settings.github_user}:{settings.github_token}".encode()).decode()
    return ["-c", f"http.https://github.com/.extraheader=Authorization: Basic {basic}"]


def _git(workdir: Path, settings: Settings, *args: str,
         check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *_auth_flag(settings), *args], cwd=workdir,
                          capture_output=True, text=True, check=check)


def sync_website(settings: Settings, profile: dict, todos: list[dict]) -> dict:
    if not settings.website_repo or not settings.github_token:
        return {"status": "not_configured", "note": "set WEBSITE_REPO (owner/name) in .env"}

    api = httpx.Client(headers={"Authorization": f"Bearer {settings.github_token}",
                                "Accept": "application/vnd.github+json"}, timeout=30)
    repo_info = api.get(f"{_API}/repos/{settings.website_repo}")
    repo_info.raise_for_status()
    default_branch = repo_info.json()["default_branch"]

    workdir = settings.data_dir / "website"
    if not (workdir / ".git").exists():
        workdir.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", *_auth_flag(settings), "clone", "-q",
                        f"https://github.com/{settings.website_repo}.git", str(workdir)],
                       capture_output=True, text=True, check=True)
        _git(workdir, settings, "config", "user.name", settings.github_user)
        _git(workdir, settings, "config", "user.email", f"{settings.github_user}@users.noreply.github.com")

    _git(workdir, settings, "fetch", "-q", "origin")
    _git(workdir, settings, "checkout", "-q", "-B", default_branch,
         f"origin/{default_branch}")

    for filename, content in render_site(profile, todos).items():
        (workdir / filename).write_text(content)

    if not _git(workdir, settings, "status", "--porcelain").stdout.strip():
        return {"status": "no_change"}

    _git(workdir, settings, "add", "-A")
    _git(workdir, settings, "commit", "-q", "-m",
         f"agent: site update {date.today().isoformat()}")
    # remote may have moved (owner edits online) — rebase in, never force-push
    pull = _git(workdir, settings, "pull", "--rebase", "-q", "origin", default_branch,
                check=False)
    if pull.returncode != 0:
        _git(workdir, settings, "rebase", "--abort", check=False)
        return {"status": "failed",
                "note": f"remote {default_branch} has conflicting edits: {pull.stderr[-300:]}"}
    _git(workdir, settings, "push", "-q", "origin", default_branch)
    sha = _git(workdir, settings, "rev-parse", "--short", "HEAD").stdout.strip()
    return {"status": "pushed", "commit": sha,
            "url": f"https://{settings.website_repo.split('/', 1)[1]}"}
