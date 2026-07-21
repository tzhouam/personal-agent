"""Git-versioned, two-layer profile memory — the owner's curated self-model.

`ProfileStore` reads/writes profile.yaml in its own git repo, committing every
change so the history is auditable and revertible. Writes happen only through
typed patch ops (`apply_ops`) over an evidence log: ops are evidence-gated,
protected sections are off-limits, and nothing is ever fabricated or hard-deleted
(entries go dormant/merged instead). Also exports the initiative-alias join key
(against fragmentation), the ops log helpers that give the daily pass multi-day
context, and the deterministic markdown/summary renderers.
"""

import json
import re
import subprocess
from datetime import date, timedelta
from pathlib import Path

import yaml

from assistant.platform.locks import locked_transaction

# Sections the LLM may never touch — manually curated facts.
PROTECTED_SECTIONS = {"identity", "education", "experience", "preferences"}
# The daily updater's write surface (profile-v2: merge/move added so
# correlated work can converge instead of fragmenting).
DAILY_OPS = {
    "bump_last_seen",
    "add_evidence",
    "add_skill",
    "add_interest",
    "add_project",
    "update_highlight",
    "mark_dormant",
    "merge_projects",
    "move_evidence",
}
# rewrite_entry is reserved for the weekly consolidation pass — per the 2026
# memory literature, wholesale rewriting must be scheduled and gated, never
# part of the per-interaction loop (doc/RESEARCH_AGENT_MEMORY_2026.md §4).
CONSOLIDATE_OPS = DAILY_OPS | {"rewrite_entry"}
ALLOWED_OPS = DAILY_OPS  # backwards-compatible default
_KEY_FIELD = {"skills": "name", "interests": "topic", "projects": "name"}
_MAX_EVIDENCE = 12
_MAX_HIGHLIGHTS = 8
# Entries re-confirmed this many times are "stable": a rewrite must cite at
# least as many distinct URLs as the text it replaces (capability-preserving
# evolution, arXiv:2605.09315).
_STABLE_CONFIRMATIONS = 3


class ProfileStore:
    """profile.yaml in its own git repo; every save is a commit → auditable diffs."""

    def __init__(self, profile_dir: Path):
        """Bind the store to `profile_dir` and the profile.yaml / PROFILE.md paths within it."""
        self.dir = profile_dir
        self.yaml_path = profile_dir / "profile.yaml"
        self.md_path = profile_dir / "PROFILE.md"
        self._lock_file = profile_dir.parent / "write.lock"

    # ── git plumbing ────────────────────────────────────────────────
    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        """Run a git command in the profile dir, capturing output; `check` raises on failure."""
        return subprocess.run(
            ["git", *args], cwd=self.dir, capture_output=True, text=True, check=check
        )

    def ensure_repo(self) -> None:
        """Create the profile dir and initialize its git repo (with a local
        agent identity) if it isn't one yet — idempotent, so save() can call it freely."""
        self.dir.mkdir(parents=True, exist_ok=True)
        if not (self.dir / ".git").exists():
            self._git("init", "-q")
            self._git("config", "user.name", "personal-agent")
            self._git("config", "user.email", "personal-agent@local")

    # ── load / save ─────────────────────────────────────────────────
    def exists(self) -> bool:
        """True if a profile.yaml has been written yet."""
        return self.yaml_path.exists()

    def load(self) -> dict:
        """Return the parsed profile.yaml, or {} when the file is empty/absent."""
        return yaml.safe_load(self.yaml_path.read_text()) or {}

    @locked_transaction
    def save(self, profile: dict, message: str) -> str:
        """Write yaml + rendered md, commit, return the commit's diff (empty if no change)."""
        self.ensure_repo()
        tmp = self.yaml_path.with_name(self.yaml_path.name + ".tmp")
        tmp.write_text(
            yaml.safe_dump(profile, sort_keys=False, allow_unicode=True, width=100)
        )
        tmp.replace(self.yaml_path)  # atomic — readers never see a torn file
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
    def apply_ops(self, profile: dict, ops: list[dict], today: str | None = None,
                  allowed: set | None = None):
        """Apply the LLM's typed patch `ops` to `profile` in place, gated.

        This is the only write surface. `allowed` defaults to DAILY_OPS (the
        weekly pass widens it to include rewrite_entry); an op is rejected if its
        kind is outside `allowed`, if it targets a PROTECTED_SECTION, or if it
        tries to merge away an initiative-owning (canonical, per aliases.yaml)
        entry. Each surviving op is dispatched to `_apply_one`; a malformed op is
        caught and rejected rather than allowed to kill the run. Returns the
        mutated profile plus the applied and rejected (with reasons) op lists.
        """
        today = today or date.today().isoformat()
        allowed = allowed or DAILY_OPS
        # initiative-owning entries are canonical: fragments merge INTO them,
        # they are never merged away (the consolidator once merged
        # vllm-omni-copilot — the automation initiative's own entry — into
        # vLLM-Omni; owner had to unmerge it)
        canonical = {str(i.get("entry", i["name"])).strip().lower()
                     for i in load_aliases(self.dir)}
        applied, rejected = [], []
        for op in ops:
            kind = op.get("op")
            section = op.get("section", "")
            if kind not in allowed or section in PROTECTED_SECTIONS:
                rejected.append({**op, "reason": "disallowed op or protected section"})
                continue
            if kind == "merge_projects" \
                    and str(op.get("from", "")).strip().lower() in canonical:
                rejected.append({**op, "reason": "initiative-owning entry may not be merged away"})
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
        """Execute a single already-vetted op of type `kind`, returning whether it
        changed anything (False = target missing, duplicate, or a gate refused it).

        Covers the full op vocabulary: touch/confirm entries (bump_last_seen,
        add_evidence), create entries (add_skill/interest/project, always born
        active with capped evidence), append highlights, retire (mark_dormant —
        never delete), and the consolidation ops. merge_projects folds a source
        into a canonical entry and leaves a "merged" pointer stub; move_evidence
        shifts matching evidence between projects; rewrite_entry replaces
        highlights/evidence under strict gates — it must add something, may not
        drop cited URLs when the entry is stable (>= _STABLE_CONFIRMATIONS), and
        superseded highlights are archived to `history` rather than deleted.
        """
        if kind == "bump_last_seen":
            entry = _find(profile, op["section"], op["name"])
            if entry is None:
                return False
            entry["last_seen"] = today
            entry["status"] = "active"
            entry["confirmations"] = entry.get("confirmations", 0) + 1
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
            entry["confirmations"] = entry.get("confirmations", 0) + 1
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

        if kind == "merge_projects":
            into = _find(profile, "projects", op["into"])
            source = _find(profile, "projects", op["from"])
            if into is None or source is None or into is source:
                return False
            for item in source.get("evidence", []):
                if item not in into.setdefault("evidence", []):
                    into["evidence"].append(item)
            for h in source.get("highlights", []):
                if h not in into.setdefault("highlights", []) \
                        and len(into["highlights"]) < _MAX_HIGHLIGHTS:
                    into["highlights"].append(h)
            # the source becomes a pointer stub — never deleted, but carries
            # nothing that could fragment again
            source_name = source.get("name")
            source.clear()
            source.update({"name": source_name, "status": "merged",
                           "merged_into": into.get("name"), "merged_at": today})
            into["last_seen"] = today
            return True

        if kind == "move_evidence":
            src = _find(profile, "projects", op["from"])
            dst = _find(profile, "projects", op["to"])
            match = str(op.get("match", "")).strip()
            if src is None or dst is None or src is dst or not match:
                return False
            moving = [e for e in src.get("evidence", []) if match.lower() in e.lower()]
            if not moving:
                return False
            src["evidence"] = [e for e in src["evidence"] if e not in moving]
            for item in moving:
                if item not in dst.setdefault("evidence", []):
                    dst["evidence"].append(item)
            dst["last_seen"] = today
            return True

        if kind == "rewrite_entry":
            entry = _find(profile, op["section"], op["name"])
            new_highlights = [str(h) for h in op.get("highlights", [])][:_MAX_HIGHLIGHTS]
            if entry is None:
                return False
            # a rewrite must bring something — it can never merely strip content
            if not new_highlights and not op.get("evidence") and not op.get("level"):
                return False
            old_highlights = entry.get("highlights", [])
            # stability gate: a well-confirmed entry may not be rewritten into
            # something citing fewer sources than it had
            effective_highlights = new_highlights or old_highlights
            if entry.get("confirmations", 0) >= _STABLE_CONFIRMATIONS \
                    and len(_urls(effective_highlights
                                  + [str(e) for e in op.get("evidence") or entry.get("evidence", [])])) \
                    < len(_urls(old_highlights + entry.get("evidence", []))):
                return False
            if new_highlights:  # empty = keep existing (evidence/level-only rewrite)
                superseded = [h for h in old_highlights if h not in new_highlights]
                if superseded:  # audit rows, never deletion (TOKI-style)
                    history = entry.setdefault("history", [])
                    history.extend(h for h in superseded if h not in history)
                entry["highlights"] = new_highlights
            if op.get("evidence"):  # optional pruned/deduped evidence list —
                # only accepted if it loses no cited URL
                new_evidence = [str(e) for e in op["evidence"]][:_MAX_EVIDENCE]
                if _urls(new_evidence) >= _urls(entry.get("evidence", [])):
                    entry["evidence"] = new_evidence
            if op.get("part_of"):
                entry["part_of"] = op["part_of"]
            if op.get("level") and op["section"] == "skills" \
                    and op["level"] in ("emerging", "working", "expert"):
                entry["level"] = op["level"]
            entry["last_seen"] = today
            return True

        return False


def _urls(texts: list) -> set:
    """Distinct http(s) URLs cited across a list of strings."""
    found = set()
    for text in texts:
        found.update(re.findall(r"https?://\S+", str(text)))
    return found


# ── initiatives (profile-v2 P1): the join key against fragmentation ──────
# Owner-editable aliases.yaml in the profile repo maps repos/keywords to
# initiative umbrellas so correlated work converges on one entry.

ALIASES_TEMPLATE = """\
# Initiative aliases — owner-editable. Each initiative groups repos/keywords
# that belong to ONE line of work; the daily updater and the weekly
# consolidation pass use these to attach evidence to the right entry instead
# of fragmenting it. `entry` names the profile project that owns the work.
initiatives: []
#  - name: Example initiative
#    entry: example-project        # profile projects entry that owns this work
#    patterns: [example-repo, "RFC #123", some keyword]
"""


def load_aliases(profile_dir: Path) -> list[dict]:
    """Return the owner-defined initiatives from aliases.yaml (only those with a
    name), or [] if the file is absent or unparseable — a bad aliases file
    degrades to "no join key", never an error."""
    path = profile_dir / "aliases.yaml"
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text()) or {}
        return [i for i in data.get("initiatives", []) if i.get("name")]
    except yaml.YAMLError:
        return []


def render_initiatives(aliases: list[dict]) -> str:
    """Prompt block listing the known initiatives and their signals."""
    if not aliases:
        return "(none defined yet)"
    return "\n".join(
        f"- {i['name']} → profile entry '{i.get('entry', i['name'])}'"
        f" (signals: {', '.join(str(p) for p in i.get('patterns', []))})"
        for i in aliases
    )


# ── ops log (profile-v2 P4): the daily pass's multi-day context ──────────

def append_ops_log(profile_dir: Path, ops: list[dict], today: str) -> None:
    """Append each applied op, stamped with `today`, as a JSONL line to
    ops_log.jsonl — the durable trail that gives the daily pass multi-day
    context. No-op when `ops` is empty."""
    if not ops:
        return
    profile_dir.mkdir(parents=True, exist_ok=True)
    with (profile_dir / "ops_log.jsonl").open("a") as fh:
        for op in ops:
            fh.write(json.dumps({"date": today, **op}, ensure_ascii=False) + "\n")


def recent_ops(profile_dir: Path, days: int = 7, limit: int = 40) -> list[dict]:
    """Return the most recent op-log records from the last `days` (at most
    `limit`, newest-trailing), skipping malformed lines. Feeds the daily pass its
    recent-history context; [] when no log exists."""
    path = profile_dir / "ops_log.jsonl"
    if not path.exists():
        return []
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    entries = []
    for line in path.read_text().splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("date", "") >= cutoff:
            entries.append(record)
    return entries[-limit:]


def _find(profile: dict, section: str, name: str) -> dict | None:
    """Return the entry in `section` whose key field matches `name`
    case-insensitively, or None. `section` must be one of the keyed sections
    (skills/interests/projects); anything else returns None."""
    key = _KEY_FIELD.get(section)
    if key is None:
        return None
    name_lower = name.strip().lower()
    for entry in profile.get(section, []):
        if str(entry.get(key, "")).strip().lower() == name_lower:
            return entry
    return None


def render_markdown(profile: dict) -> str:
    """Render `profile` to the human-readable PROFILE.md committed beside the yaml.

    Deterministic (no LLM): emits an identity header then Skills/Interests/
    Projects sections, each entry with its level/weight/role and a dormant tag,
    followed by its highlights. Merged pointer stubs are skipped.
    """
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
            if e.get("status") == "merged":  # pointer stubs are not listed
                continue
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
        """Comma-joined `key` values of the active entries in `section` (≤max_items)."""
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
