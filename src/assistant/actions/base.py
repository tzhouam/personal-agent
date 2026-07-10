"""The action framework: the typed `Action` descriptor and its param validator.
An `Action` binds a name to its handler, human-facing description, param spec,
and the metadata that drives the three surfaces (chat prompt, executor, CLI/HTTP).
"""

from dataclasses import dataclass, field
from typing import Callable

from ..config import Settings


@dataclass(frozen=True)
class Action:
    """One thing the agent can do. `handler(settings, params) -> str` returns a
    human-readable outcome line. `llm` exposes the action to the chat LLM (with
    `prompt_example` shown verbatim in the system prompt); `slash` names its
    OpenClaw slash-command family; `params` maps each param name to
    `{"required": bool, "desc": str}`."""

    name: str
    description: str
    handler: Callable[[Settings, dict], str]
    # param name -> {"required": bool, "desc": str}; values are strings
    params: dict = field(default_factory=dict)
    llm: bool = False            # exposed to the chat LLM as an emittable action
    prompt_example: str = ""     # exact line shown in the chat system prompt
    slash: str | None = None     # OpenClaw slash-command family ("todo", …)


def validate(action: Action, params: dict) -> str | None:
    """Return an error line, or None when params satisfy the action's spec."""
    for name, spec in action.params.items():
        if spec.get("required") and not str(params.get(name, "")).strip():
            return f"action {action.name}: missing required {name!r}"
    return None
