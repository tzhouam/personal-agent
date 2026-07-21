"""GitHub collector — turns the owner's GitHub activity into Observations.

Registers as `@register("github")`. Draws on the events API, the search API
(authored/reviewed/commented items and RFCs), repo/commit backfill, per-item
context, and notifications; also exposes `summarize_commits` to fold raw commits
into per-repo-month observations for direct-push work that leaves no PR trail.
"""

import base64
import re
import time
from collections import defaultdict
from datetime import datetime, timezone

import httpx

from assistant.platform.config import Settings
from assistant.agent.collectors import register

API = "https://api.github.com"
_SEARCH_PAGE_DELAY = 2.1  # search API = 30 req/min authenticated; 0 in tests
_SEARCH_MAX_PAGES = 10    # search API hard-caps at 1000 results

_EVENT_KINDS = {
    "PushEvent": "commit",
    "PullRequestEvent": "pr",
    "PullRequestReviewEvent": "review",
    "PullRequestReviewCommentEvent": "review_comment",
    "IssuesEvent": "issue",
    "IssueCommentEvent": "comment",
    "WatchEvent": "star",
    "ForkEvent": "fork",
    "CreateEvent": "create",
    "ReleaseEvent": "release",
}


@register("github")
class GitHubCollector:
    """Collects the owner's GitHub activity as Observations via a token-authed client."""

    name = "github"

    def __init__(self, settings: Settings):
        """Store the target username and build a reusable authenticated httpx
        client (Bearer token + API version header) for every request."""
        self.user = settings.github_user
        self.client = httpx.Client(
            headers={
                "Authorization": f"Bearer {settings.github_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30,
        )

    # ── activity events → observations ──────────────────────────────
    def collect(self, since: datetime) -> list[dict]:
        """Return Observations for the owner's activity since `since`.

        Leads with authored PRs/issues/RFCs (their bodies carry the richest
        profile signal), then walks up to three pages of the events feed —
        stopping at the first event older than `since` — mapping each recognized
        event type to an observation.
        """
        # authored PRs/issues/RFCs first — their bodies carry the profile signal
        observations = self.fetch_authored_items(since=since)
        for page in range(1, 4):  # events API caps at ~300 recent events anyway
            resp = self.client.get(
                f"{API}/users/{self.user}/events", params={"per_page": 100, "page": page}
            )
            resp.raise_for_status()
            events = resp.json()
            if not events:
                break
            for event in events:
                ts = datetime.fromisoformat(event["created_at"].replace("Z", "+00:00"))
                if ts < since:
                    return observations
                obs = self._event_to_observation(event, ts)
                if obs:
                    observations.append(obs)
        return observations

    def _event_to_observation(self, event: dict, ts: datetime) -> dict | None:
        """Map one events-API event at time `ts` to an Observation, or None.

        Returns None for event types outside `_EVENT_KINDS`. Otherwise builds a
        human-readable title and best-effort URL per kind (push/PR/review/issue/
        comment/star/release/fork/create), tagging the repo as the entity. PR
        `closed`+merged is reported as "merged".
        """
        kind = _EVENT_KINDS.get(event.get("type", ""))
        if kind is None:
            return None
        repo = event.get("repo", {}).get("name", "?")
        payload = event.get("payload", {})
        url = None

        if kind == "commit":
            commits = payload.get("commits", [])
            count = payload.get("size") or len(commits)
            first_msg = commits[0]["message"].splitlines()[0] if commits else ""
            title = f"Pushed {count} commit(s) to {repo}" + (f": {first_msg}" if first_msg else "")
            url = f"https://github.com/{repo}"
        elif kind == "pr":
            pr = payload.get("pull_request", {})
            action = "merged" if payload.get("action") == "closed" and pr.get("merged") else payload.get("action", "")
            title = f"PR {action} in {repo}: {pr.get('title', '')}"
            url = pr.get("html_url")
        elif kind in ("review", "review_comment"):
            pr = payload.get("pull_request", {})
            title = f"Reviewed PR in {repo}: {pr.get('title', '')}"
            url = pr.get("html_url")
        elif kind == "issue":
            issue = payload.get("issue", {})
            title = f"Issue {payload.get('action', '')} in {repo}: {issue.get('title', '')}"
            url = issue.get("html_url")
        elif kind == "comment":
            issue = payload.get("issue", {})
            title = f"Commented in {repo}: {issue.get('title', '')}"
            url = payload.get("comment", {}).get("html_url")
        elif kind == "star":
            title = f"Starred {repo}"
            url = f"https://github.com/{repo}"
        elif kind == "release":
            release = payload.get("release", {})
            title = f"Release {release.get('tag_name', '')} in {repo}"
            url = release.get("html_url")
        else:  # fork / create
            title = f"{kind} in {repo} ({payload.get('ref_type', '')} {payload.get('ref', '') or ''})".strip()
            url = f"https://github.com/{repo}"

        return {
            "source": "github",
            "ts": ts.isoformat(),
            "kind": kind,
            "title": title[:300],
            "url": url,
            "entities": [repo],
            "raw": {"type": event.get("type"), "id": event.get("id")},
        }

    # ── authored PRs / issues / RFCs (rich profile signal) ──────────
    def _search_issues(self, query: str, max_items: int | None = None) -> list[dict]:
        """Paginated /search/issues sweep → raw items. max_items=None = full
        sweep (bounded by the API's own 1000-result cap)."""
        items: list[dict] = []
        for page in range(1, _SEARCH_MAX_PAGES + 1):
            if max_items is not None and len(items) >= max_items:
                break
            if page > 1 and _SEARCH_PAGE_DELAY:
                time.sleep(_SEARCH_PAGE_DELAY)
            resp = self.client.get(
                f"{API}/search/issues",
                params={"q": query, "sort": "updated", "order": "desc",
                        "per_page": 100, "page": page},
            )
            resp.raise_for_status()
            batch = resp.json().get("items", [])
            if not batch:
                break
            items.extend(batch)
            if page == _SEARCH_MAX_PAGES and len(batch) == 100:
                print(f"warning: search hit the 1000-result cap for {query!r} — "
                      "narrow the window to sweep the rest")
        return items if max_items is None else items[:max_items]

    def fetch_authored_items(self, since: datetime | None = None,
                             max_items: int | None = 100) -> list[dict]:
        """Search-API sweep of everything the owner authored — PR and RFC bodies
        carry far more profile signal than bare push events. since=None means
        full history, max_items=None means no cap (both used by
        `assistant enrich-profile`)."""
        query = f"author:{self.user}"
        if since is not None:
            query += f" updated:>={since.date().isoformat()}"
        return [self._issue_to_observation(i)
                for i in self._search_issues(query, max_items)]

    def fetch_reviewed_items(self, since: datetime | None = None,
                             max_items: int | None = None) -> list[dict]:
        """PRs the owner reviewed but did not author — the 'core reviewer'
        signal the events API loses after ~90 days."""
        query = f"is:pr reviewed-by:{self.user} -author:{self.user}"
        if since is not None:
            query += f" updated:>={since.date().isoformat()}"
        return [self._reviewed_to_observation(i, kind="pr_reviewed", verb="Reviewed")
                for i in self._search_issues(query, max_items)]

    def fetch_commented_items(self, since: datetime | None = None,
                              max_items: int | None = None) -> list[dict]:
        """PRs/issues the owner commented on without authoring or reviewing —
        noisier than reviews, so callers gate it behind a flag."""
        query = f"commenter:{self.user} -author:{self.user} -reviewed-by:{self.user}"
        if since is not None:
            query += f" updated:>={since.date().isoformat()}"
        observations = []
        for item in self._search_issues(query, max_items):
            is_pr = "pull_request" in item
            observations.append(self._reviewed_to_observation(
                item,
                kind="pr_commented" if is_pr else "issue_commented",
                verb="Commented on"))
        return observations

    def _reviewed_to_observation(self, item: dict, kind: str, verb: str) -> dict:
        """Map a search-API PR/issue `item` to an Observation for reviewed/commented
        activity. `kind` sets the observation kind and `verb` opens the title
        (e.g. "Reviewed"/"Commented on"); noun (PR vs issue) is derived from the
        item, and a trimmed body snippet is appended for signal."""
        repo = item.get("repository_url", "").replace("https://api.github.com/repos/", "")
        noun = "PR" if "pull_request" in item else "issue"
        snippet = " ".join((item.get("body") or "").split())[:400]
        return {
            "source": "github",
            "ts": item.get("updated_at", ""),
            "kind": kind,
            "title": (f"{verb} {noun} in {repo}: {item.get('title', '')}"
                      + (f" — {snippet}" if snippet else ""))[:600],
            "url": item.get("html_url"),
            "entities": [repo],
            "raw": {"number": item.get("number")},
        }

    # ── repo understanding + commit history (enrich backfill) ───────
    def fetch_repo_context(self, full_name: str) -> dict | None:
        """Repo description/topics + README head, or None when the repo is
        unreachable with this token (private → 404/403)."""
        resp = self.client.get(f"{API}/repos/{full_name}")
        if resp.status_code in (403, 404):
            return None
        resp.raise_for_status()
        repo = resp.json()
        readme = ""
        readme_resp = self.client.get(f"{API}/repos/{full_name}/readme")
        if readme_resp.status_code == 200:
            content = base64.b64decode(readme_resp.json().get("content", "") or "")
            readme = " ".join(content.decode("utf-8", errors="replace").split())[:400]
        return {
            "repo": full_name,
            "description": repo.get("description") or "",
            "topics": repo.get("topics", []) or [],
            "language": repo.get("language") or "",
            "readme": readme,
        }

    def fetch_repo_commits(self, full_name: str, since: datetime) -> list[dict] | None:
        """Author-filtered commits since `since`. None = repo unreachable with
        this token (404/403); [] = reachable but empty (409 = empty repo)."""
        commits: list[dict] = []
        for page in range(1, 6):
            resp = self.client.get(
                f"{API}/repos/{full_name}/commits",
                params={"author": self.user, "since": since.isoformat(),
                        "per_page": 100, "page": page},
            )
            if resp.status_code in (403, 404):
                return None
            if resp.status_code == 409:  # empty repository
                return []
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            commits.extend(batch)
        return commits

    def _issue_to_observation(self, item: dict) -> dict:
        """Map an authored search-API `item` to an Observation, classifying it as
        rfc / pr_authored / issue_authored. RFCs are detected from an "rfc" token
        in the title or a label; the title carries state, repo, and a body
        snippet."""
        is_pr = "pull_request" in item
        title = item.get("title", "")
        is_rfc = "rfc" in title.lower() or any(
            "rfc" in (label.get("name", "").lower()) for label in item.get("labels", [])
        )
        kind = "rfc" if is_rfc else ("pr_authored" if is_pr else "issue_authored")
        label = "RFC" if is_rfc else ("PR" if is_pr else "Issue")
        repo = item.get("repository_url", "").replace("https://api.github.com/repos/", "")
        snippet = " ".join((item.get("body") or "").split())[:400]
        return {
            "source": "github",
            "ts": item.get("updated_at", ""),
            "kind": kind,
            "title": (f"{label} [{item.get('state', '?')}] in {repo}: {title}"
                      + (f" — {snippet}" if snippet else ""))[:600],
            "url": item.get("html_url"),
            "entities": [repo],
            "raw": {"number": item.get("number")},
        }

    # ── per-item context & completion state (feeds the todo store) ──
    def fetch_item_context(self, html_url: str | None) -> dict:
        """Structured context for a PR/issue: ``meta`` (author/size/age line,
        shown verbatim) and ``body`` (raw text — LLM-summarized, never shown raw)."""
        parts = _split_item_url(html_url)
        if not parts:
            return {}
        owner, repo, kind, number = parts
        if kind == "pull":
            resp = self.client.get(f"{API}/repos/{owner}/{repo}/pulls/{number}")
            resp.raise_for_status()
            d = resp.json()
            meta = (f"PR by {d.get('user', {}).get('login', '?')} · "
                    f"{d.get('changed_files', '?')} files "
                    f"(+{d.get('additions', '?')}/−{d.get('deletions', '?')}) · "
                    f"opened {str(d.get('created_at', ''))[:10]}")
        else:
            resp = self.client.get(f"{API}/repos/{owner}/{repo}/issues/{number}")
            resp.raise_for_status()
            d = resp.json()
            meta = (f"Issue by {d.get('user', {}).get('login', '?')} · "
                    f"{d.get('comments', 0)} comments · "
                    f"opened {str(d.get('created_at', ''))[:10]}")
        return {"meta": meta, "body": " ".join((d.get("body") or "").split())[:1500]}

    def check_finished(self, html_url: str | None) -> tuple[bool, str]:
        """Is the task behind this URL done? Merged/closed items are done; a
        review-request is also done once the owner has submitted a review."""
        parts = _split_item_url(html_url)
        if not parts:
            return False, ""
        owner, repo, kind, number = parts
        if kind == "pull":
            resp = self.client.get(f"{API}/repos/{owner}/{repo}/pulls/{number}")
            resp.raise_for_status()
            d = resp.json()
            if d.get("merged"):
                return True, "PR merged"
            if d.get("state") == "closed":
                return True, "PR closed"
            reviews = self.client.get(f"{API}/repos/{owner}/{repo}/pulls/{number}/reviews")
            reviews.raise_for_status()
            if any((rv.get("user") or {}).get("login", "").lower() == self.user.lower()
                   for rv in reviews.json()):
                return True, "you reviewed it"
            return False, ""
        resp = self.client.get(f"{API}/repos/{owner}/{repo}/issues/{number}")
        resp.raise_for_status()
        return (resp.json().get("state") == "closed", "issue closed")

    # ── notifications (feeds the digest task) ───────────────────────
    def fetch_notifications(self, since: datetime) -> list[dict]:
        """Return the owner's GitHub notifications updated since `since`.

        Walks up to two pages of /notifications and flattens each into a compact
        dict (repo, reason, subject type/title, timestamp, and an HTML URL
        resolved from the API url). Feeds the digest task rather than the profile.
        """
        notifications = []
        for page in range(1, 3):
            resp = self.client.get(
                f"{API}/notifications",
                params={
                    "since": since.astimezone(timezone.utc).isoformat(timespec="seconds"),
                    "per_page": 50,
                    "page": page,
                },
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            for n in batch:
                subject = n.get("subject", {})
                notifications.append(
                    {
                        "id": n.get("id"),
                        "repo": n.get("repository", {}).get("full_name", "?"),
                        "reason": n.get("reason", ""),
                        "type": subject.get("type", ""),
                        "title": subject.get("title", ""),
                        "updated_at": n.get("updated_at", ""),
                        "url": _api_to_html_url(subject.get("url"))
                        or n.get("repository", {}).get("html_url"),
                    }
                )
        return notifications

    # ── bootstrap helpers ────────────────────────────────────────────
    def fetch_identity(self) -> dict:
        """Return the authenticated user's /user record — used to bootstrap the profile identity."""
        resp = self.client.get(f"{API}/user")
        resp.raise_for_status()
        return resp.json()

    def fetch_recent_repos(self, limit: int = 30) -> list[dict]:
        """Return the owner's most recently pushed repos (up to `limit`).

        Queries /user/repos (authenticated) rather than /users/{name}/repos so
        private-repo work isn't silently hidden from the enrich backfill.
        """
        # /user/repos (authenticated) rather than /users/{name}/repos: the
        # latter only lists public repos, silently hiding private-repo work
        # (e.g. bde-private) from the enrich backfill.
        resp = self.client.get(
            f"{API}/user/repos",
            params={"sort": "pushed", "per_page": limit, "type": "owner"},
        )
        resp.raise_for_status()
        return resp.json()


def summarize_commits(repo: str, commits: list[dict], top_subjects: int = 3) -> list[dict]:
    """Aggregate raw commit dicts into one observation per repo-month —
    direct-push work (no PR trail) still becomes profile signal."""
    by_month: dict[str, list[dict]] = defaultdict(list)
    for commit in commits:
        date = ((commit.get("commit") or {}).get("author") or {}).get("date", "")
        if date:
            by_month[date[:7]].append(commit)
    observations = []
    for month in sorted(by_month):
        batch = sorted(by_month[month], key=lambda c: c["commit"]["author"]["date"],
                       reverse=True)
        subjects = "; ".join(
            c["commit"].get("message", "").splitlines()[0] for c in batch[:top_subjects])
        observations.append({
            "source": "github",
            "ts": batch[0]["commit"]["author"]["date"],
            "kind": "commits_summary",
            "title": f"Pushed {len(batch)} commit(s) to {repo} in {month}: {subjects}"[:300],
            "url": f"https://github.com/{repo}",
            "entities": [repo],
            "raw": {"month": month, "count": len(batch)},
        })
    return observations


def _split_item_url(html_url: str | None) -> tuple[str, str, str, str] | None:
    """Parse a PR/issue HTML URL into (owner, repo, kind, number), or None if it
    doesn't match — the callers use this to route to the pulls vs issues API."""
    match = re.search(r"github\.com/([^/]+)/([^/]+)/(pull|issues)/(\d+)", html_url or "")
    return match.groups() if match else None


def _api_to_html_url(api_url: str | None) -> str | None:
    """Rewrite an api.github.com subject URL into its human github.com URL,
    fixing the pulls→pull, commits→commit, and releases/{id}→releases path
    differences. None passes through."""
    if not api_url:
        return None
    url = api_url.replace("https://api.github.com/repos/", "https://github.com/")
    url = re.sub(r"/pulls/(\d+)$", r"/pull/\1", url)
    url = re.sub(r"/commits/([0-9a-f]+)$", r"/commit/\1", url)
    url = re.sub(r"/releases/\d+$", "/releases", url)
    return url
