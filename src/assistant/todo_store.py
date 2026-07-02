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


class _YamlItems:
    FILENAME = "items.yaml"
    ID_PREFIX = "x"

    def __init__(self, repo_dir: Path):
        self.repo_dir = repo_dir
        self.path = repo_dir / self.FILENAME

    def load(self) -> dict:
        if not self.path.exists():
            return {"next_id": 1, "items": []}
        return yaml.safe_load(self.path.read_text()) or {"next_id": 1, "items": []}

    def _save(self, data: dict, message: str) -> None:
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        self.path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
        if (self.repo_dir / ".git").exists():  # versioned alongside the profile
            subprocess.run(["git", "add", self.FILENAME], cwd=self.repo_dir,
                           capture_output=True)
            subprocess.run(["git", "commit", "-q", "-m", message], cwd=self.repo_dir,
                           capture_output=True)

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

    def mark_done(self, item_id: str) -> bool:
        data = self.load()
        for item in data["items"]:
            if item["id"] == item_id and item["status"] == "open":
                item["status"] = "done"
                item["done_at"] = date.today().isoformat()
                self._save(data, f"{self.FILENAME}: done {item_id}")
                return True
        return False

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
        return [i for i in self.load()["items"] if i["status"] == "open"]


class TodoStore(_YamlItems):
    FILENAME = "todos.yaml"
    ID_PREFIX = "t"


class ReadingList(_YamlItems):
    FILENAME = "reading_list.yaml"
    ID_PREFIX = "r"
