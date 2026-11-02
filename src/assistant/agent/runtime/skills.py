"""Progressively loaded instructions around a code-owned personal skill catalog.

Markdown documents explain procedures; only handlers and schemas defined here
grant executable capabilities. The runtime host authorizes exact arguments
before calling ``execute``. Learned procedures remain evidence-backed drafts
until an owner explicitly approves them.
"""

from __future__ import annotations

from argparse import Namespace
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Callable
import uuid

import yaml

from assistant.agent.actions import ACTIONS, ActionResult, is_risky, looks_failed
from assistant.agent.runtime.memory import Memory, _redact
from assistant.platform.config import Settings
from assistant.platform.locks import data_write_lock


_LIBRARY = Path(__file__).with_name("skill_library")
_REPO_RUNBOOKS = Path(__file__).resolve().parents[4] / "skills"
_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_PHASES = {"research", "website", "todos", "resume", "curate", "consolidate"}
_FULL_RUN = {"all", "run", "daily", "digest", "collect", "profile", "deliver"}
_PUBLISHING_PHASES = _FULL_RUN | {"website", "research", "todos", "consolidate"}
_FAMILIES = {
    "todos": ("add_todo", "done_todo", "list_todos"),
    "reading": ("done_reading", "list_reading", "unrelated_reading"),
    "research": ("web_search", "web_research"),
    "profile": ("show_profile",),
    "daily": ("run_status", "run_phase"),
    "website": ("build_personal_website",),
    "finance": ("log_transaction", "recategorize_transaction", "void_transaction",
                "list_transactions", "finance_summary", "query_transactions"),
    "health": ("log_meal", "log_exercise", "log_weight", "set_health_profile",
               "add_health_need", "done_health_need", "health_summary", "query_health"),
    "reminders": ("set_reminder", "list_reminders", "cancel_reminder"),
    "routines": ("create_routine", "list_routines", "cancel_routine"),
    "preferences": ("learn_preference", "retire_preference", "list_preferences", "self_evolve"),
    "workflows": ("create_workflow", "show_workflow", "update_workflow", "retire_workflow"),
    "memory": ("search_personal_data",),
}
_NUMBERS = {"amount", "weight_kg", "height_cm", "calories_kcal", "protein_g",
            "carbs_g", "fat_g", "duration_min"}
_ARRAYS = {"steps", "queries"}
_ALWAYS_APPROVAL = {"set_reminder", "create_routine", "build_personal_website", "self_evolve"}


def _schema(properties: dict | None = None, required: tuple = ()) -> dict:
    """Build strict JSON schemas so Markdown never chooses argument types."""
    return {"type": "object", "properties": properties or {},
            "required": list(required), "additionalProperties": False}


def _action_schema(name: str) -> dict:
    """Project typed legacy action parameters into the new tool protocol."""
    props, required = {}, []
    for key, spec in ACTIONS[name].params.items():
        kind = "string"
        if key in _NUMBERS:
            kind = "number"
        elif key in {"limit", "birth_year"} or (name == "health_summary" and key == "days"):
            kind = "integer"
        elif key in _ARRAYS:
            kind = "array"
        props[key] = {"type": kind, "description": spec.get("desc", "")}
        if kind == "array":
            props[key]["items"] = {"type": "string"}
        if spec.get("required"):
            required.append(key)
    if name == "run_phase":
        props["phase"]["enum"] = sorted(_PHASES | _FULL_RUN)
    return _schema(props, tuple(required))


def _validate(arguments: dict, schema: dict) -> None:
    """Validate the bounded schema vocabulary emitted by this module."""
    if not isinstance(arguments, dict):
        raise ValueError("skill arguments must be an object")
    props = schema["properties"]
    unknown = set(arguments) - set(props)
    if unknown:
        raise ValueError("unknown skill arguments: " + ", ".join(sorted(unknown)))
    for key in schema["required"]:
        if key not in arguments or arguments[key] in (None, ""):
            raise ValueError(f"missing required skill argument {key!r}")
    for key, value in arguments.items():
        spec, valid = props[key], False
        kind = spec["type"]
        if kind == "string":
            valid = isinstance(value, str) and len(value) <= 20_000
        elif kind == "boolean":
            valid = isinstance(value, bool)
        elif kind == "integer":
            valid = isinstance(value, int) and not isinstance(value, bool)
        elif kind == "number":
            import math
            valid = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        elif kind == "array":
            valid = isinstance(value, list) and len(value) <= 50 and all(
                isinstance(item, str) and len(item) <= 20_000 for item in value)
        if not valid:
            raise ValueError(f"skill argument {key!r} must be {kind}")
        if "enum" in spec and value not in spec["enum"]:
            raise ValueError(f"invalid value for skill argument {key!r}")


@dataclass(frozen=True)
class _Skill:
    """One trusted operation and its progressively loaded instruction family."""

    name: str
    description: str
    family: str
    parameters: dict
    handler: Callable[[Settings, dict], object]
    approval: bool = False


def _phase(settings: Settings, args: dict) -> ActionResult:
    """Run in the current worker and preserve actual phase/publication outcomes."""
    phase = args["phase"]
    if phase in _FULL_RUN:
        return _daily(settings, {})
    from assistant.agent.profile_store import ProfileStore
    from assistant.platform.llm import LLM
    store = ProfileStore(settings.profile_dir)
    profile = store.load() if store.exists() else {}
    if phase == "website":
        result = _sync_site(settings, profile)
        return _phase_result(phase, result)
    if phase == "curate":
        from assistant.agent.tasks.curate import curate
        return _phase_result(phase, curate(store))
    if phase == "resume":
        from assistant.agent.tasks.resume import sync_resume
        return _phase_result(phase, sync_resume(LLM(settings), settings, profile, args.get("profile_diff", "")))
    if phase == "consolidate":
        from assistant.agent.tasks.profile_consolidate import consolidate_profile
        if not store.exists():
            return ActionResult(False, "no profile yet; bootstrap the profile first", error="profile not configured")
        result = consolidate_profile(LLM(settings), store, settings)
        result["website"] = _sync_site(settings, store.load())
        return _phase_result(phase, result)
    from assistant.agent.events_store import EventsStore
    events = EventsStore(settings.events_db)
    try:
        if phase == "research":
            from assistant.agent.research.pipeline import run_research
            from assistant.agent.todo_store import ReadingList
            result = run_research(LLM(settings), profile, events, settings)
            reading = ReadingList(settings.profile_dir)
            for paper in result.get("papers", []):
                reading.upsert(paper["seen_id"], title=paper["title"], url=paper["url"],
                               source="arxiv", why=paper.get("why", ""))
            events.mark_seen(result.get("seen_ids", []), context="runtime skill research")
        elif phase == "todos":
            from assistant.agent.collectors.github import GitHubCollector
            from assistant.agent.marks import collect_marks
            from assistant.agent.tasks.todos import update_todos
            from assistant.agent.todo_store import TodoStore
            collect_marks(settings, events)
            github = GitHubCollector(settings) if settings.github_token else None
            result = update_todos(TodoStore(settings.profile_dir), digest={}, resume={},
                                  github=github, llm=LLM(settings))
        else:
            raise ValueError("unknown phase")
    finally:
        events.close()
    result["website"] = _sync_site(settings, profile)
    return _phase_result(phase, result)


def _sync_site(settings: Settings, profile: dict) -> dict:
    """Preserve deterministic publishing while exposing its success/failure."""
    if not settings.website_repo or not settings.github_token:
        return {"status": "not_configured"}
    from assistant.agent.todo_store import TodoStore, ReadingList
    from assistant.agent.urgency import urgency
    from assistant.agent.website import sync_website
    todos = sorted(TodoStore(settings.profile_dir).open_items(), key=urgency, reverse=True)
    return sync_website(settings, profile, todos, reading=ReadingList(settings.profile_dir).open_items())


def _phase_result(phase: str, result: dict) -> ActionResult:
    """Never turn a failed website sync into a successful workflow claim."""
    failed = result.get("status") in {"failed", "not_configured"}
    failed = failed or (result.get("website") or {}).get("status") == "failed"
    return ActionResult(not failed, json.dumps({"phase": phase, **result}, ensure_ascii=False, default=str),
                        data=result, error=f"{phase} failed" if failed else "")


def _daily(settings: Settings, args: dict) -> ActionResult:
    """Execute the retained pipeline; dry-run still permits its website phase."""
    from assistant.agent.orchestrator import run
    rc = run(settings, dry_run=args.get("dry_run", False), resume=args.get("resume", False))
    return ActionResult(rc == 0, f"daily pipeline: {'completed' if rc == 0 else 'incomplete'}",
                        data={"exit_code": rc})


def _bootstrap(settings: Settings, args: dict) -> ActionResult:
    """Retain first-time GitHub profile seeding through its tested entrypoint."""
    from assistant.cli.commands import cmd_bootstrap
    rc = cmd_bootstrap(settings)
    return ActionResult(rc == 0, "profile bootstrap completed" if rc == 0 else "profile bootstrap failed")


def _enrich(settings: Settings, args: dict) -> ActionResult:
    """Backfill evidence; optional consolidation may send an email."""
    from assistant.cli.commands import cmd_enrich_profile
    rc = cmd_enrich_profile(settings, Namespace(since=args["since"],
                            include_comments=args.get("include_comments", False),
                            no_consolidate=args.get("no_consolidate", False)))
    return ActionResult(rc == 0, "profile backfill completed" if rc == 0 else "profile backfill failed")


def _workflows(settings: Settings, args: dict) -> ActionResult:
    """Retrieve saved procedures without recursively launching an old task loop."""
    from assistant.agent.workflow_store import WorkflowStore
    rows = WorkflowStore(settings.profile_dir).active()
    return ActionResult(True, json.dumps(rows, ensure_ascii=False), data={"workflows": rows})


def _website_build(settings: Settings, args: dict) -> ActionResult:
    """Preserve overwrite confirmation while executing in the current worker."""
    from assistant.agent.tasks.build_website import build_website, site_repo_status
    status = site_repo_status(settings)
    state = status.get("state")
    if state in {"missing_token", "no_login"}:
        return ActionResult(False, "GitHub configuration is required before building a website",
                            error="GitHub not configured")
    confirm = str(args.get("confirm", "")).strip().lower() in {"1", "true", "yes", "y", "overwrite"}
    if state == "nonempty" and not confirm:
        return ActionResult(False, "an existing website requires explicit overwrite confirmation",
                            data=status, error="overwrite confirmation required")
    result = build_website(settings, overwrite=confirm)
    ok = result.get("status") not in {"failed", "not_configured"}
    return ActionResult(ok, json.dumps(result, ensure_ascii=False), data=result,
                        error="" if ok else result.get("note", "website build failed"))


class SkillCatalog:
    """Trusted personal skills, preserved runbooks and approved learned procedures."""

    def __init__(self, settings: Settings):
        """Resolve code-owned handlers; never import a Markdown-supplied name."""
        self.settings = settings
        self.root = Path(settings.data_dir).resolve()
        self._skills: dict[str, _Skill] = {}
        for family, names in _FAMILIES.items():
            for name in names:
                action = ACTIONS[name]
                handler = (_phase if name == "run_phase" else _website_build
                           if name == "build_personal_website" else action.handler)
                self._skills[name] = _Skill(name, action.description, family,
                                           _action_schema(name), handler,
                                           name in _ALWAYS_APPROVAL)
        self._add_specials()

    def _add_specials(self) -> None:
        """Bind family-level compatibility skills to explicit implementations."""
        text = lambda description: {"type": "string", "description": description}
        boolean = {"type": "boolean"}
        entries = [
            _Skill("daily", "Run the daily collection, profile, digest and website pipeline", "daily",
                   _schema({"dry_run": boolean, "resume": boolean}), _daily, True),
            _Skill("bootstrap_profile", "Seed the profile from GitHub evidence", "profile", _schema(), _bootstrap),
            _Skill("enrich_profile", "Backfill activity and optionally consolidate the profile", "profile",
                   _schema({"since": text("YYYY-MM"), "include_comments": boolean,
                            "no_consolidate": boolean}, ("since",)), _enrich, True),
            _Skill("workflows", "List the owner's saved reusable procedures", "workflows", _schema(), _workflows),
            _Skill("recall_memory", "Search sourced facts and existing personal memory", "memory",
                   _schema({"query": text("What to retrieve"), "project": text("Optional project scope"),
                            "limit": {"type": "integer"}}, ("query",)), self._recall),
            _Skill("remember_memory", "Append a sourced owner/project fact", "memory",
                   _schema({"text": text("Fact to retain"), "source": text("Evidence or conversation reference"),
                            "project": text("Optional project scope"), "kind": text("Fact category")},
                           ("text", "source")), self._remember),
        ]
        for name, phase in (("research", "research"), ("website", "website"),
                            ("consolidate", "consolidate"), ("curate", "curate")):
            entries.append(_Skill(name, f"Run the retained {phase} workflow", "profile" if phase in {"curate", "consolidate"} else phase,
                                  _schema(), lambda settings, args, p=phase: _phase(settings, {"phase": p}),
                                  phase in _PUBLISHING_PHASES))
        entries.append(_Skill("profile", "Read the evidence-backed owner profile", "profile", _schema(),
                              ACTIONS["show_profile"].handler))
        entries.append(_Skill("resume", "Propose a local résumé edit from evidence-backed changes", "resume",
                              _schema({"profile_diff": text("Evidence-backed change summary")}),
                              lambda settings, args: _phase(settings, {"phase": "resume", **args})))
        self._skills.update((skill.name, skill) for skill in entries)

    @staticmethod
    def _recall(settings: Settings, args: dict) -> ActionResult:
        """Return memory data as evidence, not as an invented summary."""
        rows = Memory(settings).recall(args["query"], args.get("project", ""), args.get("limit", 8))
        return ActionResult(True, json.dumps(rows, ensure_ascii=False), data={"hits": rows},
                            provenance=[{"id": row["id"], "source": row["source"]} for row in rows])

    @staticmethod
    def _remember(settings: Settings, args: dict) -> ActionResult:
        """Persist explicit sourced facts through the append-only memory API."""
        row = Memory(settings).remember(args["text"], args["source"], args.get("project", ""), args.get("kind", "fact"))
        return ActionResult(True, f"remembered {row['id']}", data=row, provenance=[{"id": row["id"], "source": row["source"]}])

    def _documents(self) -> dict[str, Path]:
        """Find documentation without executing scripts or accepting handler metadata."""
        result = {}
        for runbooks in (_REPO_RUNBOOKS, _LIBRARY / "runbooks"):
            if runbooks.is_dir():
                for path in sorted(runbooks.glob("*/SKILL.md")):
                    if not path.is_symlink() and _NAME.fullmatch(path.parent.name):
                        result.setdefault("runbook_" + path.parent.name, path)
        try:
            learned = self._under_root(Path("runtime/skills"))
        except ValueError:
            return result
        if learned.is_dir() and not learned.is_symlink():
            for path in sorted(learned.glob("*/SKILL.md")):
                if (not path.is_symlink() and not path.parent.is_symlink()
                        and _NAME.fullmatch(path.parent.name) and path.parent.name not in self._skills):
                    result[path.parent.name] = path
        return result

    def list(self) -> list[dict]:
        """Return compact discovery metadata; instructions load only on demand."""
        rows = [{"name": skill.name, "description": skill.description, "family": skill.family,
                 "parameters": skill.parameters, "kind": "builtin", "executable": True,
                 "requires_approval": skill.approval or skill.name == "run_phase"}
                for skill in self._skills.values()]
        for name, path in self._documents().items():
            rows.append({"name": name, "description": f"Read the {path.parent.name} procedure",
                         "family": "runbooks" if name.startswith("runbook_") else "learned",
                         "parameters": _schema(), "kind": "documentation", "executable": False,
                         "requires_approval": False})
        return sorted(rows, key=lambda row: row["name"])

    def describe(self, name: str) -> dict:
        """Load one trusted skill's instruction page and its exact input schema."""
        if name in self._skills:
            skill = self._skills[name]
            path = _LIBRARY / skill.family / "SKILL.md"
            instructions = path.read_text() if path.is_file() else skill.description
        else:
            path = self._documents().get(name)
            if path is None:
                raise KeyError(name)
            instructions = path.read_text()[:40_000]
        row = next(row for row in self.list() if row["name"] == name)
        return {**row, "instructions": instructions, "source": str(path)}

    def requires_approval(self, name: str, arguments: dict) -> bool:
        """Classify all indirect outward effects, including legacy phase aliases."""
        if name not in self._skills:
            self.describe(name)
            _validate(arguments, _schema())
            return False
        skill = self._skills[name]
        _validate(arguments, skill.parameters)
        if name == "run_phase":
            return arguments["phase"] in _PUBLISHING_PHASES
        if name == "enrich_profile":
            return not arguments.get("no_consolidate", False)
        return skill.approval or is_risky(name, arguments)

    def execute(self, name: str, arguments: dict) -> ActionResult:
        """Run a trusted operation after the host has checked its approval policy.

        Documentation-only procedures return instructions for the current host
        loop. They cannot add a handler, grant permissions or run embedded code.
        """
        try:
            if name not in self._skills:
                doc = self.describe(name)
                _validate(arguments, doc["parameters"])
                return ActionResult(True, doc["instructions"], data={"instructions": doc["instructions"],
                                    "documentation_only": True}, provenance=[{"source": doc["source"]}])
            skill = self._skills[name]
            _validate(arguments, skill.parameters)
            raw = skill.handler(self.settings, arguments)
            if isinstance(raw, ActionResult):
                return raw
            text = str(raw)
            failed = looks_failed(text)
            return ActionResult(not failed, text, error=text if failed else "")
        except (KeyError, ValueError) as exc:
            text = _redact(str(exc))
            return ActionResult(False, text, error=text)
        except Exception as exc:
            text = f"skill {name} failed: {type(exc).__name__}"
            return ActionResult(False, text, error=text)

    def _under_root(self, path: Path) -> Path:
        """Refuse symlinks and paths outside this owner's private data root."""
        candidate = path if path.is_absolute() else self.root / path
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError("skill evidence and drafts must stay in the owner's data directory")
        for part in (candidate, *candidate.parents):
            if part == self.root:
                break
            if part.is_symlink():
                raise ValueError("skill evidence and drafts must not use symlinks")
        return resolved

    def _evidence(self, references: list) -> list[dict]:
        """Require durable successful verification, not a model's prose claim."""
        if not isinstance(references, list) or not references or len(references) > 20:
            raise ValueError("a skill draft requires 1–20 verified evidence files")
        out = []
        for reference in references:
            raw = reference.get("path") if isinstance(reference, dict) else reference
            if not isinstance(raw, str) or not raw:
                raise ValueError("each evidence reference requires a file path")
            path = self._under_root(Path(raw))
            if not path.is_file() or path.stat().st_size > 1_000_000:
                raise ValueError("evidence must be a regular verification file under 1 MB")
            content = path.read_bytes()
            try:
                data = json.loads(content)
            except (ValueError, UnicodeDecodeError) as exc:
                raise ValueError("evidence must be a structured verification JSON file") from exc
            if not isinstance(data, dict) or data.get("verified") is not True:
                raise ValueError("evidence does not record successful verification")
            checks = data.get("checks")
            if not isinstance(checks, list) or not checks or any(
                    not isinstance(check, dict) or isinstance(check.get("returncode"), bool)
                    or check.get("returncode") != 0
                    or not isinstance(check.get("argv"), list) or not check["argv"]
                    for check in checks):
                raise ValueError("evidence requires successful configured verification checks")
            digest = hashlib.sha256(content).hexdigest()
            if isinstance(reference, dict) and reference.get("sha256", digest) != digest:
                raise ValueError("verification evidence changed since the draft was created")
            out.append({"path": str(path), "sha256": digest})
        return out

    def create_draft(self, name: str, instructions: str, evidence: list, project: str = "") -> dict:
        """Save a candidate learned procedure; it grants no active capability."""
        if not isinstance(name, str) or not _NAME.fullmatch(name) or name in self._skills:
            raise ValueError("invalid or reserved skill name")
        if not isinstance(instructions, str) or not instructions.strip() or len(instructions) > 40_000:
            raise ValueError("skill instructions must contain 1–40000 characters")
        if not isinstance(project, str) or len(project) > 200:
            raise ValueError("invalid skill project scope")
        if _redact(instructions) != instructions:
            raise ValueError("skill instructions must not contain credentials")
        record = {"id": "draft-" + uuid.uuid4().hex, "name": name,
                  "instructions": instructions.strip(), "evidence": self._evidence(evidence),
                  "project": project, "status": "draft"}
        with data_write_lock(self.settings.data_dir):
            path = self._under_root(Path("runtime/skill-drafts") / f"{record['id']}.json")
            path.parent.mkdir(parents=True, exist_ok=True)
            self._write_private(path, json.dumps(record, ensure_ascii=False, indent=2))
        return record

    @staticmethod
    def _write_private(path: Path, text: str) -> None:
        """Atomically write a private file without predictable shared temp names."""
        tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        tmp.replace(path)

    def approve_draft(self, draft_id: str) -> dict:
        """Owner-only promotion: recheck evidence and install instruction text."""
        if not isinstance(draft_id, str) or not re.fullmatch(r"draft-[a-f0-9]{32}", draft_id):
            raise ValueError("invalid skill draft id")
        with data_write_lock(self.settings.data_dir):
            path = self._under_root(Path("runtime/skill-drafts") / f"{draft_id}.json")
            record = json.loads(path.read_text())
            name = record.get("name", "")
            if not isinstance(name, str) or not _NAME.fullmatch(name) or name in self._skills or record.get("id") != draft_id:
                raise ValueError("invalid skill draft")
            self._evidence(record.get("evidence"))
            target = self._under_root(Path("runtime/skills") / name / "SKILL.md")
            if record.get("status") == "approved":
                return record
            if record.get("status") != "draft" or target.exists():
                raise ValueError("skill already exists or draft is not awaiting review")
            instructions = record.get("instructions", "")
            if not isinstance(instructions, str) or not instructions.strip() or _redact(instructions) != instructions:
                raise ValueError("invalid skill instructions")
            frontmatter = yaml.safe_dump({"name": name, "description": "Owner-approved learned procedure",
                                         "project": record.get("project", ""),
                                         "evidence": record["evidence"]}, sort_keys=False)
            target.parent.mkdir(parents=True, exist_ok=True)
            self._write_private(target, "---\n" + frontmatter + "---\n\n" + instructions + "\n")
            record.update(status="approved", path=str(target))
            self._write_private(path, json.dumps(record, ensure_ascii=False, indent=2))
            return record
