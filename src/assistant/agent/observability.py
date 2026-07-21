"""Agent-side observability wiring: the durable MoA metrics sink.

`platform.llm` records one numeric `moa` row per Mixture-of-Agents call but is
agent-free — it holds only the sink contract and asks for an implementation.
This module supplies it (events.db is agent-owned) and registers it as the
default at import, so any `LLM(...)` built without an explicit `metrics_sink`
still writes the row. Import this module once at a composition root (`cli.main`,
tests via conftest) to activate it.
"""

from assistant.platform import llm as _llm
from assistant.agent.events_store import EventsStore


def moa_metrics_sink(settings, run_id: str, step: str, values: dict) -> None:
    """Persist one metrics row to the (per-user) events.db. Best-effort — the
    caller (`LLM._record_moa_metrics`) already swallows exceptions, but we still
    close the store on every path."""
    events = EventsStore(settings.events_db)
    try:
        events.record_metrics(run_id, step, values)
    finally:
        events.close()


def register() -> None:
    """Make `moa_metrics_sink` the default for every LLM lacking an explicit one."""
    _llm.set_default_metrics_sink(moa_metrics_sink)


register()
