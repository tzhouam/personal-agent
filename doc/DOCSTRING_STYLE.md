# Docstring style — personal-agent

The house convention for docstrings, grounded in [PEP 257](https://peps.python.org/pep-0257/)
(structure) and the [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html)
(what to document), adapted to this codebase's terse, rationale-first voice.

**Every module, class, function, and method carries a docstring.** It explains
the *logic* (what it does and the non-obvious *why*) and the *inputs/outputs* —
not a restatement of the signature.

## Form (PEP 257)
- Triple double-quotes. The module docstring is the file's first statement.
- Summary line: one imperative sentence, ≤ ~88 cols, ending in a period. For
  obvious cases the summary line alone is the whole docstring (one-liner).
- Non-trivial cases: summary line, blank line, then the body.

## What each kind documents
- **Module** — the one thing the file owns + a note of what it exports. For a
  re-export `__init__.py`, say what the package groups and that the surface is
  re-exported.
- **Class** — its role in one line; note the key attribute or invariant it
  upholds. `@dataclass` field meaning goes in the class docstring or an inline
  `#` comment.
- **Function/method** — summary of behavior, then the inputs → output and the
  non-obvious logic, **in prose**. Name the parameters that carry meaning and
  say what the return represents. Reserve `Args:`/`Returns:` blocks for many-arg
  functions where prose would be harder to read.

## Voice (match the existing code)
- Rationale-first: explain *why* over narrating *what* the code already shows.
  Keep the project's framing — this agent "degrades, never crashes"; profile
  writes are evidence-gated; the website render is deterministic (no LLM). Call
  those contracts out where a function upholds them.
- Don't invent behavior. Describe only what the body actually does — never guess
  thresholds, ids, or branches not present.

## Examples (from this repo)
```python
def urgency(todo: dict, today: date) -> float:
    """Taskwarrior-style urgency score for `todo` as of `today`: combines
    priority, age, and deadline proximity into one float the todo list and the
    calendar gate sort by. Higher = more urgent."""
```
```python
class ProfileStore:
    """Git-versioned profile.yaml under a curated two-layer memory: typed patch
    ops applied over an evidence log, every change committed so it is auditable
    and revertible. Never fabricates — a fact needs an observation."""
```

## Don't
- Add `Args:`/`Returns:` scaffolding to trivial accessors — a one-liner is
  correct and PEP-257-preferred.
- Change code, signatures, or existing (correct) docstrings while adding new
  ones. Docstrings are additive.
- Touch `tracing.py`'s module docstring — it is a portable file kept
  byte-identical across sibling agent repos.
