# Actual Pi runtime validation

Validated on 2026-11-02 in isolated scratch directories. No production deployment,
production data migration, or actual Slack/QQ message was performed. The live
verification credential entered through non-echoing stdin and remained in process
memory; it is absent from source, configuration, fixtures and this report.

## Runtime and source

The default chat and project paths invoke the actual Pi SDK's
`createAgentSession`; Python dispatches reviewed domain/workspace tools over JSONL.
Pi owns the model/tool loop, provider transport, retries, compaction and session
history. Retained skill model calls also use Pi. The daily workflow now uses a
deterministic phase driver with its existing checkpoint and delivery semantics.
LangGraph is absent from source imports and package requirements.

The host pins `@earendil-works/pi-coding-agent` and `pi-ai` to **1.4.0**. The
user-created [tzhouam/pi fork](https://github.com/tzhouam/pi) was verified at
`c9f4c7b9fcd0f3e6b5bbc35c7c9b6cda35580eb8`, matching canonical upstream
`earendil-works/pi`. The host consumes published pinned packages and records fork,
upstream and revision metadata. No Pi core changes were required.

## Automated checks

- Python 3.12 full suite: **1,090 passed in 109.11 seconds**, before the final
  five QQ startup regressions. All 24 final channel tests also passed on Python
  3.11 and 3.12. [Current full CI checks](https://github.com/tzhouam/personal-agent/pull/36/checks)
  run the complete suite on the minimum supported Python 3.11.
- Actual Pi Node suite: **18 passed**. Tests exercise native HTTP/SSE, tool sequencing, session resume,
  approval batch interruption, truncation, step/time limits, vision replay,
  credential redaction, external-receipt deduplication, stdio lifecycle and
  environment proxy/NO_PROXY behavior.
- Bridge integration tests run the actual SDK against a local synthetic provider,
  including token budgets, CLI completion RPC, cancellation, scoped paths,
  installation readiness and private session persistence.
- Domain regressions cover Slack/QQ normalization and authorization, durable inbox
  and reply delivery, project queues/cancellation/verification, exact one-use
  approvals, image logging restrictions, preserved stores/skills, multi-owner
  routing and deterministic daily resume/delivery behavior.
- `pip check`, Python compilation and whitespace checks passed.
- Added a test-only pull-request workflow that installs the locked SDK, runs Node
  and Python checks, and asserts LangGraph is absent. It needs no model credentials.
- CI exposed a Python 3.11 QQ login/message scheduling race. Incoming messages now
  remain in a bounded FIFO until the bot identity is authenticated; API echoes
  continue flowing during the handshake. Five regressions cover ordering,
  pre-login admission, bounded buffering and invalid login identities.

The Python tests use deterministic transports or loopback providers; CLI adapter
checks use fake executable fixtures. No model CLI ran for verification.

## Clean installation without LangGraph

Built a wheel and installed it with dependencies in a fresh virtual environment.
Imports resolved from that environment's `site-packages`, outside the editable
checkout. `importlib.util.find_spec('langgraph')` returned `None`.

The wheel includes the Node host source, package metadata and exact lockfile,
plus the 28 existing skill documents. It excludes `node_modules`. Explicit
`assistant pi install` installed dependencies with `npm ci --ignore-scripts`.
The installed package successfully ran a real Pi turn against a local streaming
provider, wrote a private Pi JSONL transcript, and invoked the framework-free daily
phase driver. Dependency consistency passed in the fresh environment. All 148
packaged source/assets match the final checkout, including five Pi host assets.

## Direct live DeepSeek verification

Used `https://api.deepseek.com` and the account's actual `deepseek-flash` model ID.
Only synthetic facts and a temporary coding project were sent.

| Check | Observed outcome |
| --- | --- |
| Pi OpenAI-compatible provider | Exact requested marker; successful terminal result |
| Pi Anthropic-compatible provider | Exact requested marker; successful terminal result |
| Personal memory tool | Pi called `memory_remember`; fresh kernel and separate session retrieved the sourced fact |
| Durable sessions | Actual Pi JSONL transcripts were created under the scratch owner data directory |
| Project execution | Pi read `calc.py` and its tests, changed only `calc.py`, and returned a report |
| Independent verification | Real unittest argv exited 0; task recorded `verified: true`, artifact hash and evidence path |
| Retained skill | Created a procedure draft from that evidence, rechecked proof and activated the instruction-only skill |
| Credential scan | Scratch text artifacts/transcripts contained no credential-shaped values |
| Embedded SDK proxy support | A final live marker passed with the Node proxy flag unset after explicit proxy dispatcher initialization |

The project began with subtraction in `add(a, b)`. Its unchanged tests asserted
`add(2, 3) == 5` and `add(-1, 1) == 0`. Pi repaired the implementation through the
confined workspace tool, then the Python host independently ran the configured
checks. The verification receipt accurately labels execution as `trusted-host`.

## Boundaries

Live verification used **direct DeepSeek only**, as requested. No Codex model
verification was used. Slack/QQ adapter behavior was checked locally; actual
accounts, channel credentials and network delivery remain to be configured by
the owner. Native Slack/QQ attachments remain metadata-only; explicit CLI and
retained bridge images use Pi's API vision path. Optional CLI model transports
currently reject images explicitly.

Registered verification commands execute project code on the trusted host; Pi
itself is not an OS sandbox. Gemini's optional CLI adapter retains its documented
external-isolation opt-in. Reminder/routine delivery continues through existing
configured outbound integrations. The PR remains unmerged and undeployed.

[Runtime guide](RUNTIME_V2.md) · [Current plan](../PI_PLAN.md) ·
[Current checklist](../PI_TODO.md)
