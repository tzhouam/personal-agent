"""Learned behavior: the agent's self-evolution store.

`lessons.yaml` lives in the profile git repo. Each lesson is one durable
behavioral rule with provenance — `owner` (stated directly in chat: "以后…",
"别再…", a correction) or `evolve` (distilled by the weekly self-analysis of
chat sessions and task traces). Active lessons are injected into the system
prompt of every chat and task turn, so learning here changes behavior
immediately; retiring a lesson (never deleting) reverts it, and git history
makes every change in how the agent behaves auditable.

Bounded on purpose: rules are length-capped, the active set is capped (oldest
evolve-sourced lessons retire first when full), and near-duplicate rules are
rejected so the prompt never silts up.
"""

import subprocess
from datetime import date
from pathlib import Path

import yaml

MAX_ACTIVE = 25
MAX_RULE_CHARS = 240


class LessonsStore:
    """`lessons.yaml`: `{next_id, lessons: [...]}` — active|retired, never
    deleted; every mutation git-commits alongside the profile."""

    FILENAME = "lessons.yaml"

    def __init__(self, repo_dir: Path):
        """Bind to `lessons.yaml` inside `repo_dir` (the profile git repo)."""
        self.repo_dir = repo_dir
        self.path = repo_dir / self.FILENAME

    def load(self) -> dict:
        """Parsed store, or an empty scaffold when missing/empty."""
        if not self.path.exists():
            return {"next_id": 1, "lessons": []}
        return yaml.safe_load(self.path.read_text()) or {"next_id": 1, "lessons": []}

    def _save(self, data: dict, message: str) -> None:
        """Write back and git-commit (best-effort) so behavior changes are
        auditable."""
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        self.path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
        if (self.repo_dir / ".git").exists():
            subprocess.run(["git", "add", self.FILENAME], cwd=self.repo_dir,
                           capture_output=True)
            subprocess.run(["git", "commit", "-q", "-m", message], cwd=self.repo_dir,
                           capture_output=True)

    def learn(self, rule: str, why: str = "", source: str = "owner") -> dict | None:
        """Store one behavioral rule; None when empty or a near-duplicate of
        an active lesson. When the active set is full, the oldest
        evolve-sourced lesson retires to make room — owner-stated lessons are
        never evicted automatically."""
        rule = " ".join(str(rule or "").split())[:MAX_RULE_CHARS]
        if not rule:
            return None
        data = self.load()
        active = [l for l in data["lessons"] if l["status"] == "active"]
        if any(_similar(l["rule"], rule) for l in active):
            return None
        if len(active) >= MAX_ACTIVE:
            evolved = [l for l in active if l.get("source") == "evolve"]
            if not evolved:
                return None  # full of owner rules — owner must retire one
            evolved[0]["status"] = "retired"
        lesson = {"id": f"L{data['next_id']}", "rule": rule,
                  "why": str(why or "")[:200],
                  "source": "evolve" if source == "evolve" else "owner",
                  "status": "active", "created": date.today().isoformat()}
        data["next_id"] += 1
        data["lessons"].append(lesson)
        self._save(data, f"lessons: learn {lesson['id']} ({lesson['source']})")
        return lesson

    def retire(self, lesson_id: str) -> bool:
        """Retire (never delete) lesson `lesson_id`. True if one was active."""
        data = self.load()
        for lesson in data["lessons"]:
            if lesson["id"] == lesson_id and lesson["status"] == "active":
                lesson["status"] = "retired"
                self._save(data, f"lessons: retire {lesson_id}")
                return True
        return False

    def active(self) -> list[dict]:
        """Active lessons, oldest first."""
        return [l for l in self.load()["lessons"] if l["status"] == "active"]

    def prompt_block(self) -> str:
        """The system-prompt injection: numbered active rules, '' when none."""
        active = self.active()
        if not active:
            return ""
        lines = "\n".join(f"- [{l['id']}] {l['rule']}" for l in active)
        return ("\n\nLearned rules from the owner's feedback and your own past "
                "mistakes — follow them; the owner can retire any by id:\n" + lines)


def _similar(a: str, b: str) -> bool:
    """Near-duplicate check: normalized containment either way."""
    na, nb = " ".join(a.lower().split()), " ".join(b.lower().split())
    return bool(na and nb) and (na in nb or nb in na)
