"""Architectural boundary test: the platform layer must not import agent code.

The rule (see the platform/agent split refactor): `agent/` may import
`platform/`; `platform/` must never import `agent/`. This test AST-scans every
platform module — physical (`src/assistant/platform/`) plus the ones still at
the legacy root during migration — and fails on any import that resolves to an
agent module.

`ALLOWLIST` holds the modules that still violate the rule and whose inversion is
scheduled for a later phase; it must only ever shrink.
"""

import ast
import pathlib

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
_PKG = _SRC / "assistant"

# Logical system layer — scanned wherever the file currently lives (many still
# sit at the legacy root while the split migrates). No `env`: the `.env` in
# config.py is the dotenv file, not a module.
PLATFORM_MODULES = {
    "jobs", "scheduler", "worker", "config", "uidsafe", "locks",
    "timeutil", "tracing", "llm", "vision", "search", "notify",
    "identity", "registry", "onboarding", "admin", "serve",
}

# Known violators, inversion scheduled for a later phase. Must only shrink.
# serve.py (phase 1) and llm.py (phase 2) were inverted — their agent behaviors
# are injected (ServeServices / metrics sink), so they are no longer allowlisted.
ALLOWLIST = {"admin", "onboarding"}


def _module_name(path: pathlib.Path) -> str:
    """Dotted module name for a file under src/ (e.g. assistant.platform.worker)."""
    rel = path.relative_to(_SRC).with_suffix("")
    return ".".join(rel.parts)


def _platform_files() -> list[pathlib.Path]:
    """Union of (a) every file physically under platform/ — so new platform
    modules like dispatch.py / __init__.py are covered automatically — and
    (b) each logical PLATFORM_MODULE's legacy-root file, if present."""
    files = {p for p in (_PKG / "platform").glob("*.py")}
    for name in PLATFORM_MODULES:
        legacy = _PKG / f"{name}.py"
        if legacy.exists():
            files.add(legacy)
    return sorted(files)


def _resolve(importer_pkg: list[str], level: int, module: str | None) -> str:
    """Resolve a relative import to an absolute dotted target.

    level 0 → absolute (`module` as-is). level N → ascend N-1 packages from the
    importer's package, then append `module` (or leave the base for `from . import x`)."""
    if level == 0:
        return module or ""
    base = importer_pkg[: len(importer_pkg) - (level - 1)] if level - 1 else list(importer_pkg)
    if module:
        base = base + module.split(".")
    return ".".join(base)


def _targets(path: pathlib.Path):
    """Yield the absolute dotted target of every import statement in `path`."""
    mod = _module_name(path)
    pkg = mod.split(".")[:-1]  # a module's package is everything but its own name
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            base = _resolve(pkg, node.level, node.module)
            if base:
                yield base
            # Each imported name may itself be a submodule (`from assistant import
            # orchestrator`, `from ..tasks import evolve`), so resolve base.name
            # too. Names that are classes/functions land on a platform module and
            # are harmless (classified by the second path component).
            for alias in node.names:
                yield f"{base}.{alias.name}" if base else alias.name


def _is_agent_import(target: str) -> bool:
    """True iff `target` is an intra-package import of agent code."""
    if not target.startswith("assistant"):
        return False  # stdlib / third-party
    parts = target.split(".")
    if len(parts) < 2:
        return False  # `import assistant` itself
    if parts[1] == "platform":
        return False  # platform → platform is allowed
    # assistant.<X>...: platform if X is a platform module, else agent.
    return parts[1] not in PLATFORM_MODULES


def test_platform_never_imports_agent():
    violations: dict[str, list[str]] = {}
    for path in _platform_files():
        key = path.stem  # allowlist is keyed by bare module name
        if key in ALLOWLIST:
            continue
        bad = [t for t in _targets(path) if _is_agent_import(t)]
        if bad:
            violations[str(path.relative_to(_SRC))] = sorted(set(bad))
    assert not violations, (
        "platform modules import agent code (agent/ may import platform/, never "
        f"the reverse):\n{violations}"
    )


def test_allowlist_only_shrinks():
    """Guard against silently re-adding a violator. Every allowlisted name must
    still be a real platform module."""
    assert ALLOWLIST <= PLATFORM_MODULES


@pytest.mark.parametrize("clean", ["worker", "jobs", "scheduler"])
def test_migrated_runtime_is_clean(clean):
    """The slice's headline claim: the relocated runtime modules import no agent
    code today (they are not on the allowlist)."""
    assert clean not in ALLOWLIST
    path = _PKG / "platform" / f"{clean}.py"
    bad = [t for t in _targets(path) if _is_agent_import(t)]
    assert not bad, f"platform/{clean}.py imports agent code: {bad}"
