import subprocess
from datetime import date
from pathlib import Path

import yaml

# Sections the LLM may never touch — manually curated facts.
PROTECTED_SECTIONS = {"identity", "education", "experience", "preferences"}
ALLOWED_OPS = {
    "bump_last_seen",
    "add_evidence",
    "add_skill",
    "add_interest",
    "add_project",
    "update_highlight",
    "mark_dormant",
}
_KEY_FIELD = {"skills": "name", "interests": "topic", "projects": "name"}
_MAX_EVIDENCE = 12
_MAX_HIGHLIGHTS = 8


class ProfileStore:
    """profile.yaml in its own git repo; every save is a commit → auditable diffs."""

    def __init__(self, profile_dir: Path):
        self.dir = profile_dir
        self.yaml_path = profile_dir / "profile.yaml"
        self.md_path = profile_dir / "PROFILE.md"

    # ── git plumbing ────────────────────────────────────────────────
    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=self.dir, capture_output=True, text=True, check=check
        )

    def ensure_repo(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        if not (self.dir / ".git").exists():
            self._git("init", "-q")
            self._git("config", "user.name", "personal-agent")
            self._git("config", "user.email", "personal-agent@local")

    # ── load / save ─────────────────────────────────────────────────
    def exists(self) -> bool:
        return self.yaml_path.exists()

    def load(self) -> dict:
        return yaml.safe_load(self.yaml_path.read_text()) or {}

    def save(self, profile: dict, message: str) -> str:
        """Write yaml + rendered md, commit, return the commit's diff (empty if no change)."""
        self.ensure_repo()
        self.yaml_path.write_text(
            yaml.safe_dump(profile, sort_keys=False, allow_unicode=True, width=100)
        )
        self.md_path.write_text(render_markdown(profile))
        self._git("add", "-A")
        if not self._git("status", "--porcelain").stdout.strip():
            return ""
        self._git("commit", "-q", "-m", message)
        has_parent = self._git("rev-parse", "HEAD~1", check=False).returncode == 0
        if not has_parent:
            return "(initial profile version)"
        return self._git("diff", "HEAD~1", "HEAD", "--", "profile.yaml").stdout

    # ── typed patch ops (the LLM's only write surface) ──────────────
    def apply_ops(self, profile: dict, ops: list[dict], today: str | None = None):
        today = today or date.today().isoformat()
        applied, rejected = [], []
        for op in ops:
            kind = op.get("op")
            section = op.get("section", "")
            if kind not in ALLOWED_OPS or section in PROTECTED_SECTIONS:
                rejected.append({**op, "reason": "disallowed op or protected section"})
                continue
            try:
                if self._apply_one(profile, op, kind, today):
                    applied.append(op)
                else:
                    rejected.append({**op, "reason": "target not found or duplicate"})
            except Exception as exc:  # a malformed op must never kill the run
                rejected.append({**op, "reason": f"error: {exc}"})
        return profile, applied, rejected

    def _apply_one(self, profile: dict, op: dict, kind: str, today: str) -> bool:
        if kind == "bump_last_seen":
            entry = _find(profile, op["section"], op["name"])
            if entry is None:
                return False
            entry["last_seen"] = today
            entry["status"] = "active"
            return True

        if kind == "add_evidence":
            entry = _find(profile, op["section"], op["name"])
            if entry is None:
                return False
            evidence = entry.setdefault("evidence", [])
            if op["evidence"] in evidence or len(evidence) >= _MAX_EVIDENCE:
                return False
            evidence.append(op["evidence"])
            entry["last_seen"] = today
            return True

        if kind == "add_skill":
            skills = profile.setdefault("skills", [])
            if _find(profile, "skills", op["name"]) is not None:
                return False
            skills.append(
                {
                    "name": op["name"],
                    "level": "emerging",  # new skills always start emerging
                    "evidence": op.get("evidence", [])[:3],
                    "first_seen": today,
                    "last_seen": today,
                    "status": "active",
                }
            )
            return True

        if kind == "add_interest":
            interests = profile.setdefault("interests", [])
            if _find(profile, "interests", op["topic"]) is not None:
                return False
            interests.append(
                {
                    "topic": op["topic"],
                    "weight": min(float(op.get("weight", 0.5)), 0.7),
                    "last_seen": today,
                    "status": "active",
                }
            )
            return True

        if kind == "add_project":
            projects = profile.setdefault("projects", [])
            if _find(profile, "projects", op["name"]) is not None:
                return False
            projects.append(
                {
                    "name": op["name"],
                    "role": op.get("role", "contributor"),
                    "period": {"start": today[:7], "end": None},
                    "highlights": op.get("highlights", [])[:3],
                    "evidence": op.get("evidence", [])[:3],
                    "last_seen": today,
                    "status": "active",
                }
            )
            return True

        if kind == "update_highlight":
            entry = _find(profile, "projects", op["name"])
            if entry is None:
                return False
            highlights = entry.setdefault("highlights", [])
            if op["highlight"] in highlights or len(highlights) >= _MAX_HIGHLIGHTS:
                return False
            highlights.append(op["highlight"])
            entry["last_seen"] = today
            return True

        if kind == "mark_dormant":
            entry = _find(profile, op["section"], op["name"])
            if entry is None:
                return False
            entry["status"] = "dormant"  # never delete — dormant is recoverable
            return True

        return False


def _find(profile: dict, section: str, name: str) -> dict | None:
    key = _KEY_FIELD.get(section)
    if key is None:
        return None
    name_lower = name.strip().lower()
    for entry in profile.get(section, []):
        if str(entry.get(key, "")).strip().lower() == name_lower:
            return entry
    return None


def render_markdown(profile: dict) -> str:
    ident = profile.get("identity", {})
    lines = [f"# Profile — {ident.get('name', '?')}", ""]
    lines.append(f"GitHub: `{ident.get('github', '?')}` · Emails: {', '.join(ident.get('emails', []))}")
    if ident.get("affiliations"):
        lines.append(f"Affiliations: {', '.join(ident['affiliations'])}")
    for section, key in (("skills", "name"), ("interests", "topic"), ("projects", "name")):
        entries = profile.get(section, [])
        if not entries:
            continue
        lines += ["", f"## {section.title()}", ""]
        for e in entries:
            status = "" if e.get("status", "active") == "active" else " _(dormant)_"
            extra = ""
            if section == "skills":
                extra = f" — {e.get('level', '?')}"
            elif section == "interests":
                extra = f" — weight {e.get('weight', '?')}"
            elif section == "projects":
                extra = f" — {e.get('role', '?')}"
            lines.append(f"- **{e.get(key, '?')}**{extra}{status}")
            for h in e.get("highlights", []):
                lines.append(f"  - {h}")
    return "\n".join(lines) + "\n"


def render_summary(profile: dict, max_items: int = 8) -> str:
    """Compact plain-text profile summary for LLM prompts."""

    def actives(section, key):
        items = [e for e in profile.get(section, []) if e.get("status", "active") == "active"]
        return ", ".join(str(e.get(key)) for e in items[:max_items])

    ident = profile.get("identity", {})
    avoid = profile.get("preferences", {}).get("avoid_topics", [])
    summary = (
        f"Owner: {ident.get('name')} (github: {ident.get('github')}), "
        f"affiliations: {', '.join(ident.get('affiliations', []) or ['?'])}\n"
        f"Skills: {actives('skills', 'name') or '(none yet)'}\n"
        f"Interests: {actives('interests', 'topic') or '(none yet)'}\n"
        f"Projects: {actives('projects', 'name') or '(none yet)'}"
    )
    if avoid:
        summary += f"\nExplicitly NOT interested in (exclude from digests): {', '.join(avoid)}"
    return summary
