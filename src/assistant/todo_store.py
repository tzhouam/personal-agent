"""Persistent todo + reading-list stores.

Both live as YAML files inside the profile git repo, so every change is
versioned alongside the profile. Items carry a stable ``key`` for dedup
(notification URL, arXiv id, …) and a short display ``id`` (t1/r1…) for the
CLI: ``assistant todo done t3``.
"""

import subprocess
from datetime import date
from pathlib import Path

import yaml

from .locks import locked_transaction

# Todo display grouping, shared by the digest email and the website todos
# page: first predicate that matches wins, so the catch-all must stay last.
_TODO_GROUPS = (
    ("🔍 PR reviews", lambda t: t.get("type") == "PullRequest"),
    ("💬 Issues / RFCs", lambda t: t.get("type") == "Issue"),
    ("⚙️ CI failures", lambda t: t.get("type") == "CheckSuite"),
    ("📌 Personal / other", lambda t: True),
)


def group_todos(todos: list[dict]) -> list[tuple[str, list[dict]]]:
    """Split the (already urgency-sorted) open todos into the `_TODO_GROUPS`
    sections, dropping empty groups. Order within a group is preserved."""
    grouped: dict[str, list[dict]] = {label: [] for label, _ in _TODO_GROUPS}
    for todo in todos:
        label = next(l for l, match in _TODO_GROUPS if match(todo))
        grouped[label].append(todo)
    return [(label, items) for label, items in grouped.items() if items]


class _YamlItems:
    """Base for the git-versioned YAML item stores. Subclasses set ``FILENAME``
    and the display-``id`` ``ID_PREFIX``; every mutation rewrites the file and
    commits it (when the dir is a git repo) so the history is auditable.
    Mutating methods hold the per-user write lock for their whole
    load→mutate→save transaction (locks.py) — chat actions, pipeline phases,
    and background tasks all write these files and the shared git index."""

    FILENAME = "items.yaml"
    ID_PREFIX = "x"

    def __init__(self, repo_dir: Path):
        """Bind to ``FILENAME`` inside ``repo_dir`` (the profile git repo)."""
        self.repo_dir = repo_dir
        self.path = repo_dir / self.FILENAME
        self._lock_file = repo_dir.parent / "write.lock"

    def load(self) -> dict:
        """Return the parsed store, or an empty ``{next_id, items}`` scaffold
        when the file is missing or empty."""
        if not self.path.exists():
            return {"next_id": 1, "items": []}
        return yaml.safe_load(self.path.read_text()) or {"next_id": 1, "items": []}

    def _save(self, data: dict, message: str) -> None:
        """Write ``data`` back to the file and, if the dir is a git repo,
        stage and commit it with ``message`` — keeping items versioned
        alongside the profile. Git failures are swallowed (capture_output)
        so persistence never crashes the caller."""
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
        tmp.replace(self.path)  # atomic — an unlocked reader never sees a torn file
        if (self.repo_dir / ".git").exists():  # versioned alongside the profile
            subprocess.run(["git", "add", self.FILENAME], cwd=self.repo_dir,
                           capture_output=True)
            subprocess.run(["git", "commit", "-q", "-m", message], cwd=self.repo_dir,
                           capture_output=True)

    @locked_transaction
    def upsert(self, key: str, **fields) -> dict | None:
        """Add an item unless an open item with the same key already exists."""
        data = self.load()
        if any(i.get("key") == key and i["status"] == "open" for i in data["items"]):
            return None
        item = {"id": f"{self.ID_PREFIX}{data['next_id']}", "key": key,
                "status": "open", "created": date.today().isoformat(), **fields}
        data["next_id"] += 1
        data["items"].append(item)
        self._save(data, f"{self.FILENAME}: add {item['id']} {fields.get('title', '')[:50]}")
        return item

    @locked_transaction
    def mark_done(self, item_id: str) -> bool:
        """Close the open item with display id ``item_id``, stamping
        ``done_at``. Returns whether a matching open item was found."""
        data = self.load()
        for item in data["items"]:
            if item["id"] == item_id and item["status"] == "open":
                item["status"] = "done"
                item["done_at"] = date.today().isoformat()
                self._save(data, f"{self.FILENAME}: done {item_id}")
                return True
        return False

    @locked_transaction
    def close_by_key(self, key: str) -> bool:
        """Auto-close (e.g. a resume-approval todo once it's been approved)."""
        data = self.load()
        for item in data["items"]:
            if item.get("key") == key and item["status"] == "open":
                item["status"] = "done"
                item["done_at"] = date.today().isoformat()
                item["closed"] = "auto"
                self._save(data, f"{self.FILENAME}: auto-close {item['id']}")
                return True
        return False

    def open_items(self) -> list[dict]:
        """The still-open items (what the CLI, website, and email render)."""
        return [i for i in self.load()["items"] if i["status"] == "open"]

    @locked_transaction
    def expire_stale(self, days: int = 30, today: date | None = None) -> list[dict]:
        """Mark fully-stale open items as outdated (never delete — the status
        change removes them from the open list, website, and email).

        Urgency-metric semantics (src/assistant/urgency.py): speculative
        undated items decay out over ``days``; committed items — red
        priority or blocking someone — get the longer COMMITTED window (45
        days) before expiring, and a due date protects until a month past
        due. A lingering review request is surfaced and warned about first,
        but after six untouched weeks it is auto-cleared as outdated."""
        # ``days`` is kept for call-site clarity; urgency FADE_END /
        # COMMITTED_FADE_END govern.
        from .urgency import staleness

        today = today or date.today()
        data = self.load()
        expired = []
        for item in data["items"]:
            if item["status"] != "open":
                continue
            if staleness(item, today) <= 0.0:
                item["status"] = "outdated"
                item["outdated_at"] = today.isoformat()
                expired.append(item)
        if expired:
            self._save(data, f"{self.FILENAME}: {len(expired)} item(s) outdated (>{days}d stale)")
        return expired


class TodoStore(_YamlItems):
    """Actionable todos (``todos.yaml``); display ids ``t1``, ``t2``, …"""

    FILENAME = "todos.yaml"
    ID_PREFIX = "t"


class ReadingList(_YamlItems):
    """Surfaced reading items (``reading_list.yaml``); ids ``r1``, ``r2``, …
    Adds negative-feedback tracking the research scorer learns from."""

    FILENAME = "reading_list.yaml"
    ID_PREFIX = "r"

    @locked_transaction
    def mark_unrelated(self, item_id: str) -> bool:
        """Negative feedback: the owner says this should never have been
        surfaced. Removed from the open list AND recorded so the research
        scorer penalizes similar items next run."""
        data = self.load()
        for item in data["items"]:
            if item["id"] == item_id and item["status"] != "unrelated":
                item["status"] = "unrelated"
                item["unrelated_at"] = date.today().isoformat()
                self._save(data, f"{self.FILENAME}: {item_id} marked unrelated")
                return True
        return False

    def unrelated_titles(self, limit: int = 20) -> list[str]:
        """Most recent negative marks first — the research scorer's context."""
        return [i.get("title", "") for i in reversed(self.load()["items"])
                if i["status"] == "unrelated"][:limit]
