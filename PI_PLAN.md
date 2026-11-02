# Personal agent on actual Pi

## Goal and source

Develop on actual Pi, not a Python loop inspired by Pi. Pi must own model/tool
execution, sessions, providers and compaction. Preserve personal memory, supported
work as skills, Slack/QQ channels and durable project execution. Remove LangGraph
from the daily workflow and dependency requirements.

- Application: `tzhouam/personal-agent`, branch `refactor/agent-runtime-v2`, draft
  PR #36; starting revision for this correction is `ecc8d5b`.
- Canonical Pi: `earendil-works/pi` (formerly `badlogic/pi-mono`), inspected at
  `c9f4c7b9fcd0f3e6b5bbc35c7c9b6cda35580eb8`.
- Actual SDK: `@earendil-works/pi-coding-agent`, pinned to **1.4.0**.
- User-created fork: https://github.com/tzhouam/pi, verified as a fork of
  `earendil-works/pi` at the exact revision above. The packaged host records this
  provenance and consumes the pinned published SDK; no Pi core changes are needed.

Live verification uses direct DeepSeek V4.1 Flash only. Do not use Codex for
verification. Keep the supplied key out of files, logs, fixtures and commits.
No deployment, production migration or real Slack/QQ messaging is authorized.

## Architecture

1. **Actual Pi SDK host.** Package a small Node host around `createAgentSession`
   and `SessionManager`. Trusted configuration enters on stdin; JSONL messages
   carry individual tool requests/results between Pi and Python. Pi performs the
   model/tool loop; Python only dispatches authorized domain/workspace tools.
2. **Pi-owned context.** Persist scoped Pi JSONL sessions under each owner's data
   directory. Use Pi's session manager and finalized messages, including its
   compaction/recovery lifecycle. Preserve the old personal stores and import
   legacy conversation context explicitly without destructive migration.
3. **Personal extensions.** Expose memory, profiles, research, finance/health,
   todos/reading, reminders, website/resume and projects as declared tools and
   skills. Markdown supplies progressive instructions, not executable authority.
4. **Channels and jobs.** Reuse tested Slack Socket Mode/QQ OneBot adapters,
   allowlists, durable ingress/outbox and project queues. Project coding also
   runs through Pi, using confined workspace tools and explicit verification.
5. **Approval boundary.** Preserve exact session-scoped one-use grants. An
   approval-required tool immediately pauses/aborts the Pi run before another
   tool executes. Failed, aborted or truncated runs cannot claim completion.
6. **No LangGraph.** Retain daily domain handlers as a deterministic business
   workflow callable as a Pi skill. Replace StateGraph with a sequential driver
   preserving checkpoints/resume/delivery gates, then remove the dependency.
7. **Models.** API routes use Pi's provider/model runtime. Preserve optional
   authenticated CLI integrations as provider/extension adapters where supported;
   do not keep a Python model controller as a fallback when Pi is missing.

## Work ownership

- `pi_sdk_host`: canonical packaged Node host, pinned dependency lock, real-Pi
  faux-provider tests, JSONL protocol and session lifecycle.
- `pi_remove_langgraph`: daily workflow, application/CLI composition and packaging.
- `pi_adoption_review`: project execution migration and runtime documentation.
- `pi_bridge_tests`: Python bridge, installation, privacy and real-SDK local API tests.
- `pi_legacy_llm`: retained skill model routes and compatibility regressions.
- `pi_kernel_tests`: personal policy and CLI completion isolation tests.
- Parent: replacement kernel, configuration, integration review, live verification
  and delivery.

Preserve `agent/ -> platform/` dependency direction and exclusive file ownership.
The Node package lives in `src/assistant/platform/runtime/pi_host/` so the Python
wheel can ship its package/lock/source without shipping node_modules. Installation
is explicit and uses `npm ci --ignore-scripts`.

## Acceptance and delivery

- Default chat and projects call actual `createAgentSession`; no parallel Python
  model/tool loop remains in default/fallback paths.
- Pi owns tool execution lifecycle, session persistence, compaction and providers.
- Existing personal data and supported skills remain usable.
- LangGraph is absent from imports/dependencies; a clean installation runs without
  it installed, including the retained daily skill.
- Tests cover real Pi with a faux provider, host tool dispatch, approval pause,
  cancellation, errors/truncation, persisted context, channels and projects.
- Direct live DeepSeek demonstrates actual Pi memory/tool use and scratch project
  coding with independently successful verification. Unit tests use no paid APIs.
- Update PR #36 around the final architecture, source/dependencies, validation and
  limits. Do not merge or deploy.

Current resumable progress is in [PI_TODO.md](PI_TODO.md). Update both documents
whenever scope, contracts, ownership, evidence or blockers change.
