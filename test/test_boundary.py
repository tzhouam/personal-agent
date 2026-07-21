"""Architectural boundary: the platform layer must not import agent code.

The split is now physical — `src/assistant/platform/` (system) and
`src/assistant/agent/` (one owner's personal agent) — with one rule: `agent/`
may import `platform/`; `platform/` must never import `agent/`. This test scans
every module under `platform/` and fails on any import that resolves under
`assistant.agent`. (Composition roots — `cli`, `init_wizard` — may import both
and live at the package root, outside `platform/`, so they are not scanned.)
"""

import ast
import pathlib

_PLATFORM = pathlib.Path(__file__).resolve().parents[1] / "src" / "assistant" / "platform"


def _imported_modules(path: pathlib.Path):
    """Every module an import statement in `path` references. Platform modules use
    absolute imports, so `ast` alone resolves them; a `from X import y` yields both
    `X` and `X.y` so a submodule imported by name is caught too."""
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module
            for alias in node.names:
                yield f"{node.module}.{alias.name}"


def _is_agent(mod: str) -> bool:
    return mod == "assistant.agent" or mod.startswith("assistant.agent.")


def test_platform_never_imports_agent():
    violations = {}
    for path in sorted(_PLATFORM.rglob("*.py")):
        bad = sorted({m for m in _imported_modules(path) if _is_agent(m)})
        if bad:
            violations[str(path.relative_to(_PLATFORM.parent))] = bad
    assert not violations, (
        "platform modules import agent code (agent/ may import platform/, never "
        f"the reverse):\n{violations}")


def test_platform_uses_absolute_imports_only():
    """The scan above relies on platform imports being absolute (no relative
    import escapes an ast.ImportFrom.level==0 check). Guard that invariant."""
    offenders = {}
    for path in sorted(_PLATFORM.rglob("*.py")):
        rel = [f"level-{n.level} {n.module or ''}"
               for n in ast.walk(ast.parse(path.read_text()))
               if isinstance(n, ast.ImportFrom) and n.level > 0]
        if rel:
            offenders[path.name] = rel
    assert not offenders, f"platform modules must use absolute imports: {offenders}"
