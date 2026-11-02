# Personal agent rewrite

The current architecture is specified in [PI_PLAN.md](PI_PLAN.md), following the
user's instruction to develop on actual Pi. The plan below records the earlier
custom-runtime proposal and is retained as implementation history.

## Goal and current state

Replace the pipeline-first assistant with a small, extensible personal-agent runtime:
chat through Slack and QQ, execute ongoing personal projects, retain evidence-backed
memory, and expose existing supported work as discoverable, executable skills.
Support API providers and authenticated local agent CLIs; verify the real runtime
using the direct DeepSeek V4.1 Flash API. The user explicitly requested that the
parent act as orchestrator and delegate implementation to subagents.

- Starting commit: `73ecfd14a1efd355cc11073389ffd7f9ece716d9`.
- Working branch: `refactor/agent-runtime-v2`.
- Baseline: 854 existing tests pass in the isolated checkout.
- Local Codex: 0.163.0, authenticated through ChatGPT; no API keys in this checkout.
- Reference sources: bothouse `a7508e3` and canonical Pi (`earendil-works/pi`, formerly
  `badlogic/pi-mono`). Use concepts and documented interfaces, not copied product code.
- No deployment, production-data migration, or real Slack/QQ messages are authorized.

## Proposed design for owner review

1. **A new default agent kernel.** Replace the default `ask`, `task`, and serve/chat
   execution paths with a bounded tool loop, durable conversations/run events,
   cancellation, explicit completion/failure, and a small typed tool registry.
   Keep the old CLI workflows available explicitly during migration.
2. **Interchangeable model backends.** One contract for Anthropic-compatible and
   OpenAI-compatible APIs and authenticated Codex, Claude Code, Gemini, and Pi CLIs.
   Provider/model choices are configuration, not agent logic. Missing executables,
   credentials, unsupported capabilities, and quota errors are explicit failures.
   Test all adapters with fixtures; exercise DeepSeek against a scratch project for real.
3. **Channel-independent conversations.** Slack Socket Mode and QQ OneBot v11
   forward WebSocket (NapCat/LLOneBot) normalize into one trusted event contract.
   Retain sender, account, room, thread, and event identity; configure allowlists;
   authorize before dispatch; acknowledge transport events promptly; deduplicate
   deliveries and reply in the original thread. QQ official-bot API is a different
   protocol and is not implied by OneBot support.
4. **Durable project execution.** Register named local project directories and
   task goals; track queued/running/awaiting-approval/done/failed/cancelled states,
   attempts, progress, and verification evidence. Execute through a chosen coding
   backend in the project's explicit workspace. Separate long work from channel
   ingestion so chat remains responsive. Recover interrupted work visibly and
   conservatively; never silently replay outward actions.
5. **Preserved memory.** Keep existing profile YAML, daily finance/health shards,
   SQLite evidence, lessons, todos, reading lists, and conversations readable in
   place. Add project/session facts with provenance and retrieval; do not flatten
   structured records into a wiki or migrate real data on startup. Scope memory
   by owner/project; make proposed learned procedures reviewable before activation.
6. **Executable skills.** Add a validated SKILL.md catalog with explicit tools,
   parameters and effect metadata. Convert existing daily digest, research,
   profile, website, resume, todos/reading, finance/health, reminders/routines,
   search, and evolution into callable skills backed by the existing tested
   implementations. Preserve current operational runbooks. Save newly learned
   procedures as drafts for owner review. Never execute arbitrary Markdown as code.
7. **Action policy.** Bind approvals to an exact action and arguments, owner,
   workspace and expiry. Protect publication, messages and other external effects;
   account for indirect publication (research/todos/consolidate can push the site).
   Use bounded subprocesses and normal backend sandbox modes; do not copy YOLO
   defaults from reference projects. Preserve existing image/health/profile gates.

## Boundaries and implementation ownership

Preserve `agent/ -> platform/` dependency direction. Generic backend/channel/storage
contracts belong to platform; personal memory, tools, skills and project behavior
belong to agent; CLI and service composition may import both.

Planned work packages (exclusive file ownership assigned after design confirmation):

- Backend adapters and contract tests.
- Slack/QQ adapters, transport lifecycle and fixture tests.
- Memory retrieval and skill catalog/preservation wrappers.
- Durable run/project store and worker lifecycle.
- Parent: kernel integration, policy, CLI/configuration, docs, end-to-end validation.

## Acceptance criteria

- All existing data remains readable and the existing regression suite passes.
- A Slack fixture and QQ fixture traverse authorization -> dedup -> agent -> reply,
  including threads, unauthorized senders, reconnects and duplicate events.
- Every advertised backend has executable code and adapter contract tests; no
  placeholder provider, fabricated response or silent provider substitution.
- A real DeepSeek-backed task changes only a temporary project, runs its verification,
  records evidence, and can retrieve durable context after runtime restart.
- Project tasks remain inspectable/cancellable and interrupted state is recoverable.
- Existing supported work is discoverable and callable through documented skills.
- External-effect approval cannot be bypassed via aliases or nested skills.
- A credential-free quickstart, example configuration, setup diagnostics and honest
  distinction between fixture-tested and live-verified integrations are documented.

## Validation and delivery

Use scratch directories and fake network boundaries in automated tests. Do not run
legacy daily pipelines against live email/GitHub services. Record commands and
results in REWRITE_TODO.md. Run relevant new tests, the full old suite, a clean
installation/CLI check, and real DeepSeek integration. Review the final diff and commit
to the working branch; provide a reviewable GitHub branch/PR if available within
the user's requested implementation scope. Deployment and real channel connection
require separate credentials and explicit authorization to communicate.

## Decisions and open items

- User: support all model access methods; verify using local Codex.
- Updated user direction: use direct DeepSeek V4.1 Flash API for verification;
  do not use Codex for verification. Direct API tests are authoritative.
- User: maintain a local plan and checklist throughout the work.
- Confirmed delivery shape: new default runtime, preserved existing stores
  and legacy workflows as skills, Slack Socket Mode + QQ OneBot first.
- Delivery: [draft PR #36](https://github.com/tzhouam/personal-agent/pull/36),
  implementation commit `003a8f9`. Repository write access was restored after the
  user installed the connector on their personal account. No merge or deployment.
- Repo requires shared design understanding before a nontrivial rewrite. Its
  referenced grilling skill is absent; follow the stated one-question-at-a-time
  review rule directly rather than inventing a missing skill.

Update this document when a contract, scope or design decision changes; update
REWRITE_TODO.md whenever a work package starts, completes, fails or is blocked.
