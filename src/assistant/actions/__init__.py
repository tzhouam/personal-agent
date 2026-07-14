"""Typed action registry — the single source of truth for what the agent can DO.

One table drives three surfaces that used to be maintained by hand in parallel:
the chat system prompt (which actions the LLM may emit), the executor that
applies them, and the CLI/HTTP entry points. Handlers return one human-readable
line describing what the code actually did — replies are built from these
outcomes, never from LLM claims.

This was one 508-line module; it is now a package — `base` (the `Action`
descriptor + `validate`), `handlers` (the implementations, grouped by domain),
and `registry` (the `ACTIONS` table + `execute`/`run_action`/`prompt_block`
dispatch). The public surface is re-exported so importers are unchanged.
"""

from .base import Action, validate
from .registry import (ACTIONS, RETRIEVAL_ACTIONS, execute, looks_failed,
                       prompt_block, run_action)

__all__ = ["Action", "validate", "ACTIONS", "RETRIEVAL_ACTIONS", "execute",
           "looks_failed", "prompt_block", "run_action"]
