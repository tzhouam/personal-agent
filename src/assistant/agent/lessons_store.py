"""Learned behavior: the agent's self-evolution stores.

Two layers (doc/DESIGN_MULTI_USER.md §12b):

- **Personal** — `lessons.yaml` in each user's profile git repo. One durable
  behavioral rule per entry with provenance — `owner` (stated directly in chat:
  "以后…", "别再…", a correction) or `evolve` (distilled by the weekly
  self-analysis of that user's chats and task runs). Ids `L1, L2, …`.
- **Shared** (multi_tenant) — `shared/lessons/lessons.yaml`, deployment-global:
  user-agnostic rules distilled weekly from ALL users' evidence
  (`tasks/global_evolve.py`, under the users' mutual authorization). Ids
  `G1, G2, …` so the chat `retire_preference` action (which only touches the
  personal store) can never collide. Injected into every user's prompts
  **before** the personal block — personal rules take precedence.

Active lessons are injected into the system prompt of every chat and task turn,
so learning here changes behavior immediately; retiring a lesson (never
deleting) reverts it, and git history (where the dir is a repo) makes every
change in how the agent behaves auditable.

Bounded on purpose: rules are length-capped, the active set is capped (oldest
evolve-sourced lessons retire first when full), and near-duplicate rules are
rejected so the prompt never silts up.
"""

import logging
import subprocess
from datetime import date
from pathlib import Path

import yaml

from assistant.platform.locks import locked_transaction

log = logging.getLogger("assistant")

MAX_ACTIVE = 25
MAX_RULE_CHARS = 240
SHARED_MAX_ACTIVE = 12   # shared rules ride in EVERY user's prompts — keep small

SHARED_HEADER = ("\n\nShared rules learned across all users of this assistant "
                 "(user-agnostic operational lessons — follow them; the owner's "
                 "personal rules below take precedence on any conflict):\n")


class LessonsStore:
    """`lessons.yaml`: `{next_id, lessons: [...]}` — active|retired, never
    deleted; every mutation git-commits alongside the profile."""

    FILENAME = "lessons.yaml"

    def __init__(self, repo_dir: Path, id_prefix: str = "L",
                 max_active: int = MAX_ACTIVE):
        """Bind to `lessons.yaml` inside `repo_dir` (the profile git repo for
        the personal store; a plain dir works too — git commit is best-effort).
        `id_prefix` distinguishes stores in rendered prompts (`L*` personal,
        `G*` shared); `max_active` caps the injected set."""
        self.repo_dir = repo_dir
        self.path = repo_dir / self.FILENAME
        self.id_prefix = id_prefix
        self.max_active = max_active
        self._lock_file = repo_dir.parent / "write.lock"

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

    @locked_transaction
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
        if len(active) >= self.max_active:
            evolved = [l for l in active if l.get("source") == "evolve"]
            if not evolved:
                return None  # full of owner rules — owner must retire one
            evolved[0]["status"] = "retired"
        lesson = {"id": f"{self.id_prefix}{data['next_id']}", "rule": rule,
                  "why": str(why or "")[:200],
                  "source": "evolve" if source == "evolve" else "owner",
                  "status": "active", "created": date.today().isoformat()}
        data["next_id"] += 1
        data["lessons"].append(lesson)
        self._save(data, f"lessons: learn {lesson['id']} ({lesson['source']})")
        return lesson

    @locked_transaction
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

    def prompt_block(self, header: str | None = None) -> str:
        """The system-prompt injection: numbered active rules, '' when none.
        `header` overrides the personal-store default (the shared store passes
        `SHARED_HEADER`)."""
        active = self.active()
        if not active:
            return ""
        lines = "\n".join(f"- [{l['id']}] {l['rule']}" for l in active)
        if header is None:
            header = ("\n\nLearned rules from the owner's feedback and your own "
                      "past mistakes — follow them; the owner can retire any by "
                      "id:\n")
        return header + lines


def shared_store(settings) -> LessonsStore:
    """The deployment-global lessons store (multi_tenant): user-agnostic rules
    under `shared_dir/lessons/` — a subdir so a later `git init` for audit
    history never entangles `jobs.db`. Ids `G*`, small cap (they ride in every
    user's prompts)."""
    return LessonsStore(settings.shared_dir / "lessons",
                        id_prefix="G", max_active=SHARED_MAX_ACTIVE)


def combined_prompt_block(settings) -> str:
    """Everything the prompts should learn from: the shared block (multi_tenant
    only) **then** the personal block — personal last, so on any conflict the
    user's own rules win by both the header contract and recency. Each store is
    separately guarded: a broken shared store must never cost the user their
    personal rules (and vice versa). In single_user this is byte-identical to
    the legacy personal `prompt_block()`."""
    parts = []
    if settings.deployment_mode == "multi_tenant":
        try:
            parts.append(shared_store(settings).prompt_block(header=SHARED_HEADER))
        except Exception:
            log.exception("shared lessons injection failed")
    try:
        parts.append(LessonsStore(settings.profile_dir).prompt_block())
    except Exception:
        log.exception("personal lessons injection failed")
    return "".join(parts)


def _similar(a: str, b: str) -> bool:
    """Near-duplicate check: normalized containment either way."""
    na, nb = " ".join(a.lower().split()), " ".join(b.lower().split())
    return bool(na and nb) and (na in nb or nb in na)
