"""Shared pipeline state and its on-disk persistence.

Defines `AssistantState`, the TypedDict threaded through every LangGraph node,
plus `load_state`/`persist_state` for the small `state.json` checkpoint that
survives across runs so `--resume` knows which phase to re-enter."""

import json
import operator
from pathlib import Path
from typing import Annotated, Any, TypedDict


class AssistantState(TypedDict, total=False):
    """Mutable state passed between pipeline phases; every field is optional
    (`total=False`) since each node contributes only its own outputs. `phase`
    names the phase to (re)enter; `errors` uses an `operator.add` reducer so
    each node's errors accumulate across the graph rather than overwrite."""

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
    """Read the persisted checkpoint from `state_file`, returning its parsed
    dict or `None` when the file is missing or unreadable/corrupt — degrading
    to a fresh run rather than crashing on bad JSON."""
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
