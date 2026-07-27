# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Workflow rule (repo-wide)

`.cursor/rules/grill-by-default.mdc`: before implementing a plan, feature, architecture, refactor, or
other non-trivial change, run a **grilling** session first (`grilling` / `grill-me` skill) — one
question at a time, recommend an answer with each, look facts up in the environment, put decisions to
the owner, and do not start implementing until the owner confirms shared understanding. Skip it for
trivial questions, pure lookups, or an explicit "just implement".

## Commands

```bash
pip install -e .                          # editable install; provides the `assistant` CLI
.venv/bin/python -m pytest -q             # full suite (527 tests, ~40 s)
.venv/bin/python -m pytest test/test_chat.py -q          # one file
.venv/bin/python -m pytest test/test_chat.py::test_x -q  # one test
```

There is no linter/formatter/type-checker configured — pytest is the only gate.

Runtime CLI (full table in the README): `assistant init [--check]` (setup wizard / config doctor),
`assistant run [--dry-run|--resume]`, `assistant run-phase <phase>`, `assistant ask "…"`,
`assistant task "…"`, `assistant serve` (loopback HTTP daemon the WeChat bridge talks to),
`assistant admin …` (multi-tenant operator tools).

Scripts under `scripts/` and `systemd/` are written for the **deployed** container (`/rebase/personal-agent`,
`/rebase/.venv`), not this checkout — don't "fix" those paths.

## Architecture

Full map: `doc/DESIGN.md`. Multi-tenant: `doc/DESIGN_MULTI_USER.md`. Runtime topology: `doc/DESIGN_SERVICE_LAYER.md`.

### The one hard structural rule

`src/assistant/` splits into two layers:

- **`platform/`** — the runtime that hosts an agent: config, LLM client, serve daemon, job queue +
  worker pool, tenancy (identity/registry/onboarding/admin), notify/search/vision, locks/tracing.
- **`agent/`** — one owner's personal agent: the daily pipeline, stores, action registry, chat,
  collectors, research, website, tasks.

**`agent/` may import `platform/`; `platform/` must never import `agent/`.** Enforced by
`test/test_boundary.py` (which also requires platform modules to use absolute imports only). When the
platform needs agent behavior it declares a setter/contract and the agent registers an implementation
in `agent/wiring.py` — importing that module once at a composition root wires everything. Composition
roots (`cli/`, `init_wizard.py`) sit at the package root and may import both.

### The daily pipeline

`agent/orchestrator.py` builds a LangGraph `StateGraph(AssistantState)` of nine phases:
`collect → profile → resume → digest → todos → research → website → deliver → curate`.
`AssistantState` (`agent/state.py`) is the shared bag. Discipline: `state.json` holds the phase to
re-enter (advanced only on success), each phase writes a JSON artifact under `runs/<run_id>/`,
`--resume` rehydrates and restarts at that phase, one `flock` serializes runs. Phases catch their own
failures into `state["errors"]` — **degrade, never crash**; the seen-store only advances after the
email actually sends.

### Memory & write safety

Two layers: an append-only SQLite evidence log (`events.db`) beneath a small, git-versioned
`profile/profile.yaml`. The profile is never freely rewritten by an LLM — `profile_store.apply_ops`
accepts a fixed set of typed patch ops applied in code, with invariants enforced in code, not prompt:
protected sections (`identity`/`education`/`experience`/`preferences`) reject every op, nothing is
ever deleted (entries go dormant/merged/outdated), and a stability gate blocks evidence-shrinking
rewrites. `rewrite_entry`/`merge_projects` are weekly-consolidation-only.

Personal sub-stores (`finance_store.py`, `health_store.py`, and sessions) follow the same shape:
YAML in the profile git repo, **sharded into per-day files by event date** (`finance/YYYY-MM-DD.yaml`)
with date-encoded ids and a one-time migration from the legacy single file; never-delete (wrong
entries are voided/recategorized); stated-or-auto time identity for dedup; all summary numbers
**computed in code** and injected as a context block the LLM is told to cite. Adding another sub-store
means copying that pattern and wiring its joins into `agent/insights.py`.

Every mutating store method holds the per-user write lock (`platform/locks.py::locked_transaction`,
reentrant, `data_dir/write.lock`) across the whole load→mutate→save+commit, with atomic
tmp+`os.replace` saves. Locks are never held across LLM or network calls.

### Chat, actions, tasks

`agent/actions/registry.py` holds `ACTIONS` — one table that is the single source of truth for what
the agent can do, driving the chat prompt, the executor, slash commands, and the HTTP surface. Adding
a capability = one `Action` entry + handler; nothing else changes. Handlers return a human-readable
line describing what the code actually did, and replies are built from those, never from LLM claims;
failed outcomes get up to two LLM repair rounds.

`agent/task_runner.py` is a bounded ReAct loop for requests with no built-in pipeline. Depth adapts by
assessed tier (simple/medium/complex), and outward-effect actions flagged `risky` in the registry pause
the task as `awaiting_approval` until the owner approves. `execute_task`/`plan_task`/`trigger_run`/
`approve_task` and the workflow actions are excluded from a task's own action set.

### Config & LLM routing

`platform/config.py` is a Pydantic `Settings` over `.env` (repo root, then CWD). `LLM_ROLES` routes
roles (chat/pipeline/research/task/evolve) to different models *and endpoints*; `LLM_MIXTURE` runs
Mixture-of-Agents on the offline reasoning roles; `LLM_REVIEW` is the strongest-reasoning slot used
only by `scripts/review_plan.py`. All three parse tolerantly (`NoDecode` validator → `{}` on malformed
JSON) because every command builds `Settings()` and a bad optional knob must never break startup.
Interactive paths (chat, `plan_task`, `web_search`) and pure judges stay single-model for latency.

In `multi_tenant` mode, the shared `.env` carries **global infra only**; per-user identity and
credentials live in `users/<uid>/config.env` and never inherit from the shared `.env`
(`PERSONAL_ENV_FIELDS`) — no credential is ever copied between users.

## Conventions

- **Docstrings everywhere** — module, class, function, method. Rationale-first, terse voice; see
  `doc/DOCSTRING_STYLE.md` for the house form. Never touch `tracing.py`'s module docstring (kept
  byte-identical across sibling repos).
- **Tests use scratch dirs only** — never read-modify the live `~/.personal-agent/`. `test/conftest.py`
  gives a `settings` fixture pinned to `tmp_path` with `_env_file=None`, and imports
  `assistant.agent.wiring` so platform contracts are filled.
- **Git flow** — work on a `feat/…` `fix/…` `refactor/…` branch, then a `--no-ff` merge commit
  (`Merge feat/x: summary`); Conventional-Commit subjects (`fix(chat): …`).
- **Skills as runbooks** — when you resolve a recurring operational failure, distill it into
  `skills/<name>/SKILL.md` (Diagnose / Fix / Verification / Anti-patterns). Read the matching one
  before re-deriving a fix; `skills/README.md` indexes them.
- **Extension points** (all in `agent/`): collector = module in `collectors/` + `@register("name")`;
  chat action = an `ACTIONS` entry; pipeline phase = node in `orchestrator.py` + `_PHASES` + artifact +
  a `metrics.EXTRACTORS` entry; research source = a line in `config/sources.yaml`.
- **Safety invariants that shape code review** — the website render is deterministic (no LLM output
  reaches a public page), résumé pushes are approval-gated, private pages are client-side encrypted,
  and health data never reaches the website or digest.
