"""Owner-authored reusable workflows: named text-described step sequences.

`workflows.yaml` lives in the profile git repo — versioned like todos and
lessons, local-only, and never-delete (a workflow is *retired*, not removed).
A workflow v1 IS a saved text plan `{steps ≤6, verify}` in exactly the format
the task runner executes with milestones, tier budgets, and the risky-action
approval gate — creating one saves a procedure; running one replays it through
the same machinery (`run_workflow` → a pre-planned task record).

Fail-closed store: a file that exists but doesn't parse — or parses to the
wrong shape — raises `WorkflowStoreError` and is never overwritten; mutations
refuse until the owner recovers the file (git history is the best-effort
audit trail, the YAML is the source of truth).
"""

import subprocess
from datetime import datetime
from pathlib import Path

import yaml

from assistant.platform.locks import locked_transaction

MAX_ACTIVE = 20
MAX_STEPS = 6           # aligned with the task planner's step cap
MAX_STEP_CHARS = 200
MAX_NAME = 60
MAX_TEXT = 300          # description / verify


class WorkflowStoreError(RuntimeError):
    """workflows.yaml exists but is unreadable/mis-shaped — fail closed."""


def valid_steps(steps) -> list[str] | None:
    """Structurally validate `steps` (the generic action validator can't check
    containers): a list of 1–6 non-blank strings. Returns the normalized list
    or None."""
    if not isinstance(steps, list) or not steps or len(steps) > MAX_STEPS:
        return None
    out = []
    for s in steps:
        if not isinstance(s, str) or not s.strip():
            return None
        out.append(s.strip()[:MAX_STEP_CHARS])
    return out


class WorkflowStore:
    """`workflows.yaml`: `{next_id, workflows: [...]}` — active|retired,
    never deleted; every mutation git-commits alongside the profile
    (best-effort)."""

    FILENAME = "workflows.yaml"

    def __init__(self, repo_dir: Path):
        """Bind to `workflows.yaml` inside `repo_dir` (the profile git repo)."""
        self.repo_dir = repo_dir
        self.path = repo_dir / self.FILENAME
        self._lock_file = repo_dir.parent / "write.lock"

    def load(self) -> dict:
        """Parsed store, or an empty scaffold when the file is missing/empty.

        Fail-closed: a file that raises on parse, or parses to something that
        is not `{next_id: int, workflows: list-of-dicts}`, raises
        `WorkflowStoreError` — the file is preserved untouched and mutators
        refuse, so a corrupt store can never be silently replaced (which
        would discard workflows and recycle ids)."""
        if not self.path.exists():
            return {"next_id": 1, "workflows": []}
        try:
            data = yaml.safe_load(self.path.read_text())
        except yaml.YAMLError as exc:
            raise WorkflowStoreError(f"workflows.yaml unreadable: {exc}") from exc
        if data in (None, ""):
            return {"next_id": 1, "workflows": []}
        if not (isinstance(data, dict) and isinstance(data.get("next_id"), int)
                and isinstance(data.get("workflows"), list)
                and all(isinstance(w, dict) for w in data["workflows"])):
            raise WorkflowStoreError("workflows.yaml has an unexpected shape — "
                                     "fix or restore it (git history has every version)")
        return data

    def _save(self, data: dict, message: str) -> None:
        """Atomic write + best-effort git commit."""
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
        tmp.replace(self.path)
        if (self.repo_dir / ".git").exists():
            subprocess.run(["git", "add", self.FILENAME], cwd=self.repo_dir,
                           capture_output=True)
            subprocess.run(["git", "commit", "-q", "-m", message], cwd=self.repo_dir,
                           capture_output=True)

    # ── reads ────────────────────────────────────────────────────────
    def active(self) -> list[dict]:
        """Workflows not retired."""
        return [w for w in self.load()["workflows"] if w.get("status") == "active"]

    def get(self, workflow_id: str) -> dict | None:
        """The ACTIVE workflow with `workflow_id` (execution path), or None."""
        return next((w for w in self.active() if w["id"] == workflow_id), None)

    def get_any(self, workflow_id: str) -> dict | None:
        """The workflow with `workflow_id` regardless of status (inspection)."""
        return next((w for w in self.load()["workflows"]
                     if w["id"] == workflow_id), None)

    def by_name(self, name: str) -> dict | None:
        """Case-insensitive ACTIVE-name lookup (names are unique among active)."""
        needle = str(name).strip().lower()
        return next((w for w in self.active()
                     if w["name"].lower() == needle), None)

    # ── mutations (locked transactions) ──────────────────────────────
    @locked_transaction
    def create(self, name: str, description: str, steps, verify: str = "",
               source: str = "owner") -> tuple[str, dict | None]:
        """Create a workflow → `("created", wf)`. `("invalid", None)` on bad
        input, `("duplicate", existing)` when an active workflow already has
        the (case-insensitive) name, `("full", None)` at the active cap."""
        name = str(name or "").strip()[:MAX_NAME]
        description = str(description or "").strip()[:MAX_TEXT]
        normalized = valid_steps(steps)
        if not name or not description or normalized is None:
            return "invalid", None
        existing = self.by_name(name)
        if existing:
            return "duplicate", existing
        data = self.load()
        if sum(1 for w in data["workflows"] if w.get("status") == "active") >= MAX_ACTIVE:
            return "full", None
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        wf = {"id": f"wf{data['next_id']}", "name": name,
              "description": description, "steps": normalized,
              "verify": str(verify or "").strip()[:MAX_TEXT],
              "status": "active", "created": now, "updated": now,
              "last_run": None, "last_status": None, "last_task_id": None,
              "run_count": 0, "source": str(source or "owner")[:20]}
        data["next_id"] += 1
        data["workflows"].append(wf)
        self._save(data, f"workflows: create {wf['id']} {name[:40]}")
        return "created", wf

    @locked_transaction
    def update(self, workflow_id: str, name: str | None = None,
               description: str | None = None, steps=None,
               verify: str | None = None) -> tuple[str, dict | None]:
        """Edit an active workflow → `("updated", wf)`; `("invalid", None)` on
        bad fields, `("conflict", other)` when renaming onto another active
        name, `("missing", None)` for an unknown/retired id."""
        data = self.load()
        wf = next((w for w in data["workflows"]
                   if w["id"] == workflow_id and w.get("status") == "active"), None)
        if wf is None:
            return "missing", None
        if name is not None:
            name = str(name).strip()[:MAX_NAME]
            if not name:
                return "invalid", None
            other = next((w for w in data["workflows"]
                          if w.get("status") == "active" and w["id"] != workflow_id
                          and w["name"].lower() == name.lower()), None)
            if other:
                return "conflict", other
            wf["name"] = name
        if description is not None:
            description = str(description).strip()[:MAX_TEXT]
            if not description:
                return "invalid", None
            wf["description"] = description
        if steps is not None:
            normalized = valid_steps(steps)
            if normalized is None:
                return "invalid", None
            wf["steps"] = normalized
        if verify is not None:
            wf["verify"] = str(verify).strip()[:MAX_TEXT]
        wf["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        self._save(data, f"workflows: update {workflow_id}")
        return "updated", wf

    @locked_transaction
    def retire(self, workflow_id: str) -> bool:
        """Retire (never delete) an active workflow. True if one was retired."""
        data = self.load()
        for w in data["workflows"]:
            if w["id"] == workflow_id and w.get("status") == "active":
                w["status"] = "retired"
                w["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                self._save(data, f"workflows: retire {workflow_id}")
                return True
        return False

    @locked_transaction
    def mark_ran(self, workflow_id: str, status: str, task_id: str) -> bool:
        """Record a completed run (`status`: done|partial). **Idempotent by
        `task_id`** — the runner calls this BEFORE persisting the terminal
        task record, so a crash between the two replays the finish and lands
        here again with the same task id, which is skipped: the counter is
        exactly-once. Tolerates a retired workflow (audit truth). `run_count`
        counts completed runs; `last_status` carries done vs partial."""
        data = self.load()
        for w in data["workflows"]:
            if w["id"] != workflow_id:
                continue
            if w.get("last_task_id") == task_id:
                return True   # replayed finish — already counted
            w["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            w["last_status"] = "done" if status == "done" else "partial"
            w["last_task_id"] = task_id
            w["run_count"] = int(w.get("run_count") or 0) + 1
            self._save(data, f"workflows: {workflow_id} ran ({w['last_status']})")
            return True
        return False


def render_workflow(wf: dict) -> str:
    """One human-readable block for chat replies / show_workflow."""
    lines = [f"[{wf['id']}] {wf['name']}"
             + (" (retired)" if wf.get("status") != "active" else "")
             + f" — {wf['description']}"]
    lines += [f"  {i}. {s}" for i, s in enumerate(wf["steps"], 1)]
    if wf.get("verify"):
        lines.append(f"  verify: {wf['verify']}")
    if wf.get("run_count"):
        lines.append(f"  runs: {wf['run_count']} · last: {wf.get('last_run')} "
                     f"({wf.get('last_status')})")
    return "\n".join(lines)