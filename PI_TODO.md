# Pi adoption checklist

Design: [PI_PLAN.md](PI_PLAN.md).

## Checkpoint

Direction: actual Pi SDK, preserved personal capabilities, no LangGraph.
Live checks: direct DeepSeek only, never Codex.
Branch: `refactor/agent-runtime-v2`; PR #36 is being updated with actual Pi.
Fork: https://github.com/tzhouam/pi (verified revision `c9f4c7b`).

## Discovery

- [x] Inspect current runtime and confirm remaining LangGraph use.
- [x] Read upstream Pi conventions and SDK interfaces.
- [x] Confirm canonical upstream revision and published SDK 1.4.0.
- [x] Attempt authorized fork creation (403); SDK development remains unblocked.
- [x] Verify user-created `tzhouam/pi` fork and exact upstream revision.
- [x] Write current local plan/checklist and assign subagents exclusive files.

## Implementation

- [x] Package actual Pi SDK host and pinned dependency lock.
- [x] Implement bounded Python/Node JSONL tool/model/session bridge.
- [x] Replace Python controller loop with a thin Pi session host.
- [x] Run projects through Pi with confined workspace tools.
- [x] Route retained skill model calls through Pi.
- [x] Preserve approvals, memory, skills and channel/job behavior.
- [x] Replace daily StateGraph with a framework-free business workflow.
- [x] Remove LangGraph imports and dependency entirely.
- [x] Add explicit Pi install/doctor and wheel assets.
- [x] Update docs and configuration to describe actual runtime accurately.

## Verification and delivery

- [x] Test actual Pi SDK with a faux provider; no paid calls in unit tests.
- [x] Verify approval pause, cancellation/error/truncation and scoped sessions.
- [x] Verify channel/project composition with fake Pi transport; actual bridge separately.
- [x] Run daily regressions and full applicable Python suite (1,090 passed).
- [x] Install wheel and prove operation with no LangGraph installed.
- [x] Run direct DeepSeek memory/tool and scratch-project checks on Pi.
- [x] Review source/lock and verify no committed credentials.
- [ ] Publish updated PR #36 with accurate evidence and limits.

## Work log

- Previous custom runtime passed 1,011 tests and direct DeepSeek checks, but
  retained LangGraph and did not use Pi as its engine. Those results do not
  validate this new Pi implementation.
- Initial fork endpoint returned 403. User created the fork; GitHub metadata and
  fork `main` confirm parent and exact source revision. No upstream core edits.
- First combined daily/kernel/project/channel/boundary regressions: 83 passed.
  Expanded kernel policy suite: 40 passed. Final full Python suite: 1,090 passed in
  109.11 seconds. Actual SDK host: 18 Node tests passed.
- Fresh wheel installation ran actual Pi against a local streaming API and the
  deterministic daily driver with no LangGraph installed. Final wheel was rebuilt
  and reinstalled after review fixes; all 148 packaged source/assets match the
  final checkout, and installed Pi, skills and memory checks passed.
- Direct DeepSeek actual-Pi checks passed: both native API protocols, sourced
  memory after restart, scratch arithmetic repair with unchanged tests passing,
  verified evidence and skill retention. Scratch root:
  `/tmp/personal-agent-pi-live-q74bf26c`.
- Fixed SDK embedding's missing environment-proxy initialization with pinned
  undici, matching Pi CLI. CONNECT/NO_PROXY/explicit-disable tests passed. A final
  live DeepSeek marker passed without `NODE_USE_ENV_PROXY`.
- Independent review fixed project workers retaining stale owner credentials,
  bounded project commands by the Pi session deadline, and refused FIFO reads.
- Added test-only pull-request CI: pinned Node/Python actions, locked SDK install
  with lifecycle scripts disabled, real-SDK local-provider tests and no LangGraph.
