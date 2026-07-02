import re
from datetime import datetime, timezone

import httpx

from ..config import Settings
from . import register

API = "https://api.github.com"

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
    name = "github"

    def __init__(self, settings: Settings):
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
        observations = []
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

    # ── notifications (feeds the digest task) ───────────────────────
    def fetch_notifications(self, since: datetime) -> list[dict]:
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
        resp = self.client.get(f"{API}/user")
        resp.raise_for_status()
        return resp.json()

    def fetch_recent_repos(self, limit: int = 30) -> list[dict]:
        resp = self.client.get(
            f"{API}/users/{self.user}/repos",
            params={"sort": "pushed", "per_page": limit, "type": "owner"},
        )
        resp.raise_for_status()
        return resp.json()


def _api_to_html_url(api_url: str | None) -> str | None:
    if not api_url:
        return None
    url = api_url.replace("https://api.github.com/repos/", "https://github.com/")
    url = re.sub(r"/pulls/(\d+)$", r"/pull/\1", url)
    url = re.sub(r"/commits/([0-9a-f]+)$", r"/commit/\1", url)
    url = re.sub(r"/releases/\d+$", "/releases", url)
    return url
