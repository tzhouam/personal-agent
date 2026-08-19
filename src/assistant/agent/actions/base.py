"""The action framework: the typed `Action` descriptor and its param validator.
An `Action` binds a name to its handler, human-facing description, param spec,
and the metadata that drives the three surfaces (chat prompt, executor, CLI/HTTP).
"""

from dataclasses import dataclass, field
from typing import Any, Callable

from assistant.platform.config import Settings


@dataclass
class ActionResult:
    """Machine-readable result from one action execution.

    Handlers may return this directly when they have structured data or a
    reliable success verdict.  Legacy handlers may keep returning strings;
    the registry wraps them so chat/CLI callers retain their existing text
    API while the task runner no longer has to steer solely from prose.
    ``provenance`` contains compact source descriptors/ids, never credentials.
    """

    ok: bool
    text: str
    data: Any = None
    error: str = ""
    confidence: float | None = None
    provenance: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class Action:
    """One thing the agent can do. `handler(settings, params) -> str` returns a
    human-readable outcome line. `llm` exposes the action to the chat LLM (with
    `prompt_example` shown verbatim in the system prompt); `slash` names its
    OpenClaw slash-command family; `params` maps each param name to
    `{"required": bool, "desc": str}`. `risky` marks outward/irreversible
    effects an *autonomous task* must not execute without owner approval —
    either a plain flag or a params-predicate for actions whose riskiness
    depends on the params (`run_phase website` publishes, `run_phase todos`
    doesn't). Direct chat actions are the owner's explicit intent and are not
    gated by it."""

    name: str
    description: str
    handler: Callable[[Settings, dict], str | ActionResult]
    # param name -> {"required": bool, "desc": str}; values are strings
    params: dict = field(default_factory=dict)
    llm: bool = False            # exposed to the chat LLM as an emittable action
    prompt_example: str = ""     # exact line shown in the chat system prompt
    slash: str | None = None     # OpenClaw slash-command family ("todo", …)
    risky: "bool | Callable[[dict], bool]" = False  # outward/irreversible in a task
    read_only: bool = False      # safe to run without the per-user mutation lock


def validate(action: Action, params: dict) -> str | None:
    """Return an error line, or None when params satisfy the action's spec."""
    for name, spec in action.params.items():
        if spec.get("required") and not str(params.get(name, "")).strip():
            return f"action {action.name}: missing required {name!r}"
    return None
