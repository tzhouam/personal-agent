"""Personal website generator + publisher.

The site is a DETERMINISTIC render of profile.yaml + todos — no LLM in the
loop, so nothing fabricated can reach a public page. Publishing pushes
directly to the repo's default branch (owner's explicit choice, 2026-07-02 —
was PR-gated before); remote edits are rebased in first, never force-pushed.
"""

import base64
import calendar as _calendar
import html
import subprocess
from datetime import date, datetime
from pathlib import Path

import httpx

from .config import Settings
from .utils import ref_label

_API = "https://api.github.com"


# ── rendering ────────────────────────────────────────────────────────
def render_site(profile: dict, todos: list[dict], today: date | None = None) -> dict[str, str]:
    """Returns {filename: content} for the generated site."""
    today = today or date.today()
    ident = profile.get("identity", {})
    e = html.escape

    def actives(section):
        return [x for x in profile.get(section, []) if x.get("status", "active") == "active"]

    parts = [
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        f"<title>{e(ident.get('name', ''))}</title>",
        "<link rel='stylesheet' href='agent-site.css'></head><body><main>",
        # ── header / personal info ──
        f"<header><h1>{e(ident.get('name', ''))}</h1>",
        f"<p class='sub'>{e(' · '.join(ident.get('affiliations', [])))}</p>",
        "<p class='links'>"
        + " · ".join(
            f"<a href='{e(link)}'>{e(link.split('//')[-1].rstrip('/'))}</a>"
            for link in ident.get("links", []) if link
        )
        + (f" · <a href='mailto:{e(ident['emails'][0])}'>email</a>" if ident.get("emails") else "")
        + "</p></header>",
    ]

    skills = actives("skills")
    if skills:
        parts.append("<section><h2>Skills</h2><p class='chips'>"
                     + "".join(f"<span class='chip'>{e(s['name'])}</span>" for s in skills)
                     + "</p></section>")

    experience = profile.get("experience", [])
    if experience:
        parts.append("<section><h2>Experience</h2><ul class='timeline'>")
        for job in experience:
            period = job.get("period", {})
            when = f"{period.get('start', '')} – {period.get('end') or 'present'}"
            parts.append(f"<li><b>{e(str(job.get('title', '')))}</b>, "
                         f"{e(str(job.get('org', '')))} <span class='when'>({e(when)})</span>")
            for h in job.get("highlights", []):
                parts.append(f"<br><span class='hl'>· {e(str(h))}</span>")
            parts.append("</li>")
        parts.append("</ul></section>")

    education = profile.get("education", [])
    if education:
        parts.append("<section><h2>Education</h2><ul>")
        for school in education:
            parts.append(f"<li><b>{e(str(school.get('school', '')))}</b> — "
                         f"{e(str(school.get('degree', '')))} {e(str(school.get('period', '')))}</li>")
        parts.append("</ul></section>")

    projects = actives("projects")
    if projects:
        parts.append("<section><h2>Projects</h2><ul class='projects'>")
        for p in projects:
            link = next(iter(p.get("evidence", [])), None)
            name = f"<a href='{e(link)}'>{e(p['name'])}</a>" if link and str(link).startswith("http") else e(p["name"])
            parts.append(f"<li><b>{name}</b> <span class='when'>({e(p.get('role', ''))})</span>")
            for h in p.get("highlights", []):
                parts.append(f"<br><span class='hl'>· {e(str(h))}</span>")
            parts.append("</li>")
        parts.append("</ul></section>")

    parts.append(_render_calendar(todos, today))
    parts.append(
        f"<footer>Maintained automatically by personal-agent · updated {today.isoformat()}</footer>"
        "</main></body></html>"
    )
    return {"index.html": "".join(parts), "agent-site.css": _CSS}


def _render_calendar(todos: list[dict], today: date) -> str:
    e = html.escape
    by_day: dict[int, list[dict]] = {}
    unscheduled = []
    for todo in todos:
        due = todo.get("due")
        try:
            due_date = datetime.strptime(str(due), "%Y-%m-%d").date() if due else None
        except ValueError:
            due_date = None
        if due_date and due_date.year == today.year and due_date.month == today.month:
            by_day.setdefault(due_date.day, []).append(todo)
        else:
            unscheduled.append(todo)

    parts = [f"<section><h2>Todo Calendar — {today.strftime('%B %Y')}</h2><table class='cal'>",
             "<tr>" + "".join(f"<th>{d}</th>" for d in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")) + "</tr>"]
    for week in _calendar.Calendar().monthdayscalendar(today.year, today.month):
        parts.append("<tr>")
        for day in week:
            if day == 0:
                parts.append("<td class='off'></td>")
                continue
            classes = "today" if day == today.day else ""
            cell = f"<b>{day}</b>"
            for todo in by_day.get(day, []):
                cell += f"<div class='todo'>{e(todo['title'][:40])}</div>"
            parts.append(f"<td class='{classes}'>{cell}</td>")
        parts.append("</tr>")
    parts.append("</table>")

    if unscheduled:
        parts.append("<h3>Open todos</h3><ul class='todos'>")
        for todo in unscheduled:
            title = e(todo["title"])
            label = ref_label(todo.get("url"), todo.get("detail", "") or todo.get("title", ""),
                              todo.get("type", ""))
            if label:  # short bracketed link; the summary stays plain text
                title = f"<a href='{e(todo['url'])}'>[{e(label)}]</a>: {title}"
            elif todo.get("url"):
                title = f"<a href='{e(todo['url'])}'>[link]</a>: {title}"
            parts.append(f"<li>{title} <span class='when'>({e(todo.get('source', ''))}, "
                         f"since {e(todo.get('created', ''))})</span></li>")
        parts.append("</ul>")
    parts.append("</section>")
    return "".join(parts)


_CSS = """body{font-family:-apple-system,'Segoe UI',sans-serif;color:#1f2937;margin:0;background:#fafafa}
main{max-width:780px;margin:0 auto;padding:32px 20px}
header h1{margin-bottom:0}.sub{color:#6b7280;margin-top:4px}
section{margin-top:28px}h2{border-bottom:2px solid #e5e7eb;padding-bottom:6px}
.chip{display:inline-block;background:#eef2ff;border-radius:12px;padding:2px 10px;margin:2px;font-size:14px}
.when{color:#9ca3af;font-size:13px}.hl{color:#4b5563;font-size:14px}
ul.projects li,ul.timeline li{margin-bottom:10px}
table.cal{border-collapse:collapse;width:100%}
.cal th,.cal td{border:1px solid #e5e7eb;vertical-align:top;padding:4px;height:52px;width:14%;font-size:13px}
.cal td.off{background:#f3f4f6}.cal td.today{background:#fef9c3}
.cal .todo{background:#fee2e2;border-radius:4px;padding:1px 4px;margin-top:2px;font-size:11px}
footer{margin-top:40px;color:#9ca3af;font-size:12px}
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
