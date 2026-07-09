"""Collector registry — each collector turns one data source into Observations.

Adding a source (gmail, chrome, calendar, …) = one module registering itself here;
the orchestrator iterates the registry and never changes.
"""

from typing import Callable

REGISTRY: dict[str, Callable] = {}


def register(name: str):
    def decorator(factory):
        REGISTRY[name] = factory
        return factory

    return decorator


from . import chrome, github, gmail  # noqa: E402,F401  — populate the registry
