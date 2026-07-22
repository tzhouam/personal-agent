"""Self-serve "build my personal website from GitHub" — the on-demand ability.

Chained by the `build_website` job kind (`agent.dispatch`), run under the
tenant's own `Settings` by the worker pool. It sequences the subset of the daily
pipeline a fresh tenant needs, on request:

  provision `<login>.github.io` (create with auto_init if absent)
    → seed profile identity from GitHub
    → full history enrich (authored + reviewed + commits + repo context)
    → render + publish the site
    → WeChat the result.

Every step reuses the same code the owner's daily pipeline runs. `on_demand`
tenants are never swept into the scheduler (registry `schedule`), so this is the
only thing that refreshes their site — the completion message says so.
"""

import logging
from argparse import Namespace

import httpx

from assistant.platform.config import Settings
from assistant.platform.user_config import update_user_config

log = logging.getLogger("assistant")

_API = "https://api.github.com"
_ENRICH_SINCE = "2025-07"   # full-history default (matches `enrich-profile`)


def _api_client(token: str) -> httpx.Client:
    """An authenticated GitHub API client (context-managed by callers)."""
    return httpx.Client(
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"},
        timeout=30)


def site_repo_status(settings: Settings) -> dict:
    """Side-effect-free pre-flight for the `build_personal_website` action.

    Reports the `<login>.github.io` repo state so the synchronous action can
    decide whether to enqueue, ask the user to connect first, or ask for an
    overwrite confirmation. Returns `{state, login?, repo?, url?}` where state ∈
    `{missing_token, no_login, absent, empty, nonempty}`."""
    token, login = settings.github_token, settings.github_user
    if not token:
        return {"state": "missing_token"}
    if not login:
        return {"state": "no_login"}
    repo = f"{login}/{login}.github.io"
    with _api_client(token) as api:
        resp = api.get(f"{_API}/repos/{repo}")
    out = {"login": login, "repo": repo, "url": f"https://{login}.github.io"}
    if resp.status_code == 404:
        out["state"] = "absent"
    elif resp.status_code == 200:
        # `size` is approximate KB; 0 = no commits yet (safe to publish into).
        out["state"] = "empty" if int(resp.json().get("size") or 0) == 0 else "nonempty"
    else:
        resp.raise_for_status()
    return out


def _ensure_repo(token: str, login: str, repo: str) -> bool:
    """Ensure `<login>.github.io` exists; create it (auto_init, so it has a
    default branch for `sync_website` to check out) when absent. Returns True if
    it was freshly created."""
    with _api_client(token) as api:
        resp = api.get(f"{_API}/repos/{repo}")
        if resp.status_code == 200:
            return False
        if resp.status_code != 404:
            resp.raise_for_status()
        create = api.post(f"{_API}/user/repos", json={
            "name": f"{login}.github.io",
            "description": "Personal site — auto-generated from GitHub activity.",
            "homepage": f"https://{login}.github.io",
            "auto_init": True,
            "has_issues": False,
            "has_wiki": False,
        })
        create.raise_for_status()
    return True


def _seed_identity(settings: Settings) -> None:
    """Merge the GitHub `/user` identity into the (minimal, onboarding-seeded)
    profile — name/github/links/affiliations/emails — so the site's About/links
    are populated before enrich adds skills and projects. Idempotent: identity
    is a PROTECTED section, so the later consolidation pass never rewrites it."""
    from assistant.agent.collectors.github import GitHubCollector
    from assistant.agent.profile_store import ProfileStore

    gh = GitHubCollector(settings)
    user = gh.fetch_identity()
    store = ProfileStore(settings.profile_dir)
    profile = store.load() if store.exists() else {}
    ident = dict(profile.get("identity") or {})

    ident["github"] = settings.github_user
    if not str(ident.get("name") or "").strip() or ident.get("name") == settings.uid:
        ident["name"] = user.get("name") or settings.github_user
    links = list(ident.get("links") or [])
    for url in [user.get("html_url"), user.get("blog")]:
        if url and url not in links:
            links.append(url)
    if links:
        ident["links"] = links
    if user.get("company") and not ident.get("affiliations"):
        ident["affiliations"] = [user["company"]]
    emails = {e for e in (ident.get("emails") or []) if e}
    if user.get("email"):
        emails.add(user["email"])
    if emails:
        ident["emails"] = sorted(emails)

    profile["identity"] = ident
    store.save(profile, f"build_website: seed identity for {settings.github_user}")


def _run_enrich(settings: Settings) -> None:
    """Full history enrich (the `enrich-profile` command, invoked in-process):
    authored + reviewed PRs/issues, per-repo commit summaries, repo context,
    folded into the profile and consolidated."""
    from assistant.cli.commands import cmd_enrich_profile

    cmd_enrich_profile(settings, Namespace(
        since=_ENRICH_SINCE, include_comments=False, no_consolidate=False))


def _notify(settings: Settings, text: str) -> None:
    """Best-effort WeChat push of the result (the async completion ping). A fresh
    tenant may not have an announce target configured, in which case this is a
    no-op — the synchronous action reply already gave them the site URL."""
    try:
        from assistant.platform.notify import send_wechat
        log.info("build_website notify: %s", send_wechat(settings, text))
    except Exception:
        log.exception("build_website: completion notify failed")


def build_website(settings: Settings, *, overwrite: bool = False,
                  cancel_check=None) -> dict:
    """Provision → seed → enrich → publish for `settings`'s tenant. Returns a
    status dict and always notifies the user (best-effort). Never assumes the
    daily pipeline ran; seeds what a fresh tenant lacks."""
    from assistant.agent.profile_store import ProfileStore
    from assistant.agent.todo_store import ReadingList, TodoStore
    from assistant.agent.website import sync_website

    def _check():
        if cancel_check:
            cancel_check()

    _check()
    token, login = settings.github_token, settings.github_user
    if not token or not login:
        status = {"status": "failed", "note": "GitHub not connected"}
        _notify(settings, "⚠️ Couldn't build your site — connect GitHub first "
                          "(paste your token in chat).")
        return status

    repo = f"{login}/{login}.github.io"
    try:
        created = _ensure_repo(token, login, repo)
        _check()

        # point this tenant's config at the repo, then reload Settings so
        # website_repo is live for sync_website (per-user config.env re-read).
        update_user_config(settings.data_dir, {"WEBSITE_REPO": repo})
        settings = Settings.for_user(settings.uid)

        _seed_identity(settings)
        _check()
        _run_enrich(settings)
        _check()

        store = ProfileStore(settings.profile_dir)
        website = sync_website(
            settings, store.load(),
            TodoStore(settings.profile_dir).open_items(),
            reading=ReadingList(settings.profile_dir).open_items(),
            # clean slate on a freshly-created repo (drop auto-init README) or a
            # user-confirmed overwrite of an existing site.
            replace=created or overwrite)
    except Exception as exc:
        log.exception("build_website failed for %s", settings.uid)
        _notify(settings, f"⚠️ Website build failed: {exc}")
        return {"status": "failed", "note": str(exc)}

    url = f"https://{login}.github.io"
    if website.get("status") in ("pushed", "no_change"):
        _notify(settings, f"✅ Your site is live: {url}\n"
                          "Built from your GitHub activity. It won't refresh on "
                          "its own — say “build my site” anytime to update it.")
    else:
        _notify(settings, f"⚠️ Website publish didn't complete: "
                          f"{website.get('note', website.get('status'))}")
    website.setdefault("url", url)
    return website
