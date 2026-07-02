import json
import operator
from pathlib import Path
from typing import Annotated, Any, TypedDict


class AssistantState(TypedDict, total=False):
    run_id: str
    phase: str  # collect | profile | digest | research | deliver | curate | done — phase to (re)enter
    dry_run: bool
    observations: list[dict]
    notifications: list[dict]
    profile_diff: str
    profile_ops: list[dict]
    digest: dict
    research: dict
    resume: dict
    todos: dict
    reading: list[dict]
    website: dict
    curated: dict
    email_sent: bool
    digest_path: str
    errors: Annotated[list, operator.add]


def load_state(state_file: Path) -> dict[str, Any] | None:
    if not state_file.exists():
        return None
    try:
        return json.loads(state_file.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def persist_state(state_file: Path, **fields: Any) -> None:
    """Merge-preserving write, same discipline as the rebase agent: the
    persisted `phase` names the phase to re-enter and is only advanced by a
    node on successful completion."""
    current = load_state(state_file) or {}
    current.update(fields)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(current, indent=2, ensure_ascii=False))
    tmp.replace(state_file)
