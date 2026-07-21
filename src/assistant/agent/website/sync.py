"""Publishing: render the site and push it to the website repo's default branch.

Owner's explicit choice (2026-07-02) is a direct push, not a PR; remote edits
(the owner editing online) are rebased in first and never force-pushed, so a
conflicting remote edit fails the run rather than clobbering it. Returns a
status dict — the caller (website phase) records it, never raises.
"""

import base64
import subprocess
from datetime import date
from pathlib import Path

import httpx

from assistant.platform.config import Settings
from assistant.agent.website.render import render_site

_API = "https://api.github.com"


def _auth_flag(settings: Settings) -> list[str]:
    """A `git -c http.<host>.extraheader=...` Basic-auth flag pair carrying the
    GitHub token, so clones/pushes authenticate without writing credentials to
    disk."""
    basic = base64.b64encode(f"{settings.github_user}:{settings.github_token}".encode()).decode()
    return ["-c", f"http.https://github.com/.extraheader=Authorization: Basic {basic}"]


def _git(workdir: Path, settings: Settings, *args: str,
         check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command in `workdir` with the auth flag injected; captures
    output. `check` raises on non-zero exit (pass check=False to inspect the
    result yourself)."""
    return subprocess.run(["git", *_auth_flag(settings), *args], cwd=workdir,
                          capture_output=True, text=True, check=check)


def sync_website(settings: Settings, profile: dict, todos: list[dict],
                 reading: list[dict] | None = None,
                 routines: list[dict] | None = None,
                 reminders: list[dict] | None = None) -> dict:
    """Render the site from `profile`/`todos` (+ reading/routines/reminders,
    defaulting to the live stores) and publish it to `settings.website_repo`'s
    default branch. Clones/updates a working checkout under the data dir, writes
    the rendered files, and commits+rebases+pushes only when something changed.

    Returns a status dict: `not_configured` (no repo/token), `no_change`,
    `failed` (remote had conflicting edits — rebase aborted), or `pushed` (with
    the commit sha and site url)."""
    if routines is None or reminders is None:  # default to the live stores
        from assistant.platform.notify import ReminderStore
        from assistant.agent.routines import RoutineStore

        routines = RoutineStore(settings.data_dir).active() if routines is None else routines
        reminders = (ReminderStore(settings.data_dir).pending()
                     if reminders is None else reminders)
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

    marks_cfg = ({"repo": settings.marks_repo, "token": settings.marks_push_token}
                 if settings.marks_repo and settings.marks_push_token else None)
    for filename, content in render_site(profile, todos, reading=reading,
                                         routines=routines, reminders=reminders,
                                         password=settings.website_password,
                                         marks_cfg=marks_cfg).items():
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
