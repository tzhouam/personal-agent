# Rewrite checklist and handoff

Source of design decisions: [REWRITE_PLAN.md](REWRITE_PLAN.md).

## Current checkpoint

Branch: `refactor/agent-runtime-v2`; base: `73ecfd1`.
Status: implementation, full regression, clean installation and direct API
verification complete. Published for review in [draft PR #36](https://github.com/tzhouam/personal-agent/pull/36).
Repository access is restored. The PR is not merged or deployed. Live verification
used DeepSeek only, per the user's final direction.

## Discovery and decisions

- [x] Read repo conventions and establish a clean isolated checkout.
- [x] Verify baseline (854 tests passed during the initial repository review).
- [x] Confirm local Codex is installed and authenticated without exposing credentials.
- [x] Confirm user wants all backends and local Codex verification.
- [x] Create working branch and persistent plan/checklist.
- [x] Delegate convention discovery; referenced grilling skill is absent.
- [x] Finish inventory of supported workflows and memory compatibility.
- [x] Confirm canonical Pi and provider command contracts.
- [x] Confirm Slack and QQ transport design and test requirements.
- [x] Record owner confirmation of the proposed architecture.

## Implementation (do not mark complete without evidence)

- [x] Define shared typed contracts and exclusive agent ownership.
- [x] Implement API and local CLI backends with bounded execution and fixture tests.
- [x] Implement Slack Socket Mode adapter with allowlists, event ACK and thread routing.
- [x] Implement QQ OneBot v11 WebSocket adapter with allowlists and replies.
- [x] Implement durable conversations, event deduplication and run/project state.
- [x] Implement project scheduling, cancellation and conservative restart recovery.
- [x] Implement small bounded kernel and typed tools with scoped approvals.
- [x] Implement scoped durable memory and compatibility retrieval from existing stores.
- [x] Implement executable skill catalog and wrappers for all existing supported work.
- [x] Implement reviewed skill drafts for reusable successful project procedures.
- [x] Integrate default CLI and daemon while preserving explicit legacy entrypoints.
- [x] Update dependencies, example configuration, README and migration documentation.

## Verification and review

- [x] Run provider, channel, storage, memory, skills and policy contract tests.
- [x] Exercise both channel fixtures through the complete runtime.
- [x] Verify a real DeepSeek-backed coding task in a scratch project.
- [x] Verify conversation/memory/project persistence after restart.
- [x] Verify unauthorized inputs, duplicate delivery, approvals and cancellation.
- [x] Run full regression suite and compare with the 854-test baseline.
- [x] Check clean installation, CLI help/doctor and final diff.
- [x] Record material limitations and live integrations not verified.
- [x] Commit completed implementation and prepare reviewable local delivery.
- [x] Publish GitHub draft PR once repository write access is granted: [#36](https://github.com/tzhouam/personal-agent/pull/36).

## Work log

- Delivery complete: the user installed the connector on the personal account;
  Git push succeeded and draft PR #36 was created. Implementation commit is
  `003a8f9`; this checklist update records the restored access and PR link.
- Discovery: existing skills are runbook Markdown, not an executable framework.
- Risk inventory: research/todos/consolidate CLI phases can publish the website;
  approval policy must cover indirect effects, not only the website phase name.
- Live verification available: Codex CLI 0.163.0, authenticated via ChatGPT.
- Slack/QQ credentials are not present; do not send messages to real channels.
- Canonical Pi is `earendil-works/pi`; use its minimal kernel/context/skill ideas.
  Its CLI has no OS sandbox, and current RPC completes at `agent_settled`.
- Slack/QQ research: use outbound WebSockets, one dedicated transport reader,
  immediate protocol ACK, durable event identity and separate task execution.
- Design review requested through the pending question; implementation waits for
  owner confirmation as required by CLAUDE.md.
- Owner confirmed the proposed architecture; implementation is authorized.
- Exclusive work packages completed: provider adapters + kernel QA; Slack/QQ
  transports + legacy serve seam; memory + executable skill catalog; durable
  projects/store + prior configuration-write race fix. Parent owns integration.
- Combined initial runtime selection: 126 passed in 6.97s; later provider/kernel
  and transport hardening added further passing regressions.
- Existing serve/tenant/WeCom/delivery compatibility selection: 121 passed.
- Latest real Codex probe: failed with stale-auth 401/turn.failed; device login
  was started for the user. Do not claim live execution passed before retry.
- Hardened default Codex route ignores inherited plugins/MCP config while keeping
  auth; custom provider routing must be explicit. Gemini requires explicit
  unrestricted/external-isolation opt-in even for controller requests.
- API and Slack credential-shaped values are now redacted from durable runtime
  records; TOML permission flags must be actual booleans.
- Native channel adapters commit to the durable acceptor before Slack ACK; the
  volatile callback queue is not the authority for accepted work.
- Explicit operator-registered verification argv executes project code on the
  trusted host; receipts label this separately from the coding backend sandbox.
- User changed live verification to direct DeepSeek V4.1 Flash API and explicitly
  prohibited Codex verification. The previous device login has timed out.
- Direct live DeepSeek `deepseek-flash` checks passed: model availability,
  OpenAI-compatible and Anthropic-compatible calls, tool-based sourced memory
  persisted across restart, a scratch calc.py repair with real unittest success,
  and a verified skill draft/activation. Credential stayed in process memory and
  was not written to repository files or runtime configuration.
- Full regression checkpoint: 984 passed in 95.43s; later app/CLI integration
  suite added 12 passing tests. A final full run follows remaining changes.
- Final full suite: **1011 passed in 83.96s**. Fresh wheel installation passed;
  installed imports and all 28 packaged skill documents were verified.
- Final review fixes include per-session FIFO (rowid tie-break), shutdown
  cancellation of thread-backed project work, private-data/workspace overlap
  rejection, legacy history seeding, and explicit failure on truncated completions.
- Detailed live and automated evidence: [validation report](doc/VALIDATION_RUNTIME_V2.md).
- Delivery attempt: GitHub create-tree returned 403 "Resource not accessible by
  integration"; a separate authenticated Git push dry-run also returned 403.
  Repository content has not been published or deployed. Local patch and bundle
  are exported under `/workspace/shared/downloads/` for portable delivery.
