"""Collector registry — each collector turns one data source into Observations.

Adding a source (gmail, chrome, calendar, …) = one module registering itself here;
the orchestrator iterates the registry and never changes.
"""

from typing import Callable

REGISTRY: dict[str, Callable] = {}


def register(name: str):
    """Decorator factory that files a collector class under `name` in REGISTRY.

    Applied as `@register("<name>")` on each collector so importing the module
    is the only wiring needed — the orchestrator reads REGISTRY and never learns
    the concrete classes.
    """

    def decorator(factory):
        """Record `factory` under the captured `name` and return it unchanged."""
        REGISTRY[name] = factory
        return factory

    return decorator


from . import chrome, github, gmail  # noqa: E402,F401  — populate the registry
