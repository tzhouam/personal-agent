# Personal-Agent Improvement Prompt

```text
/goal

Improve `/rebase/personal-agent` as a local-first daily personal assistant. Base
all decisions on this repository; do not import tasks, metrics, datasets, or
architecture from other projects.

First inspect `README.md`, `doc/DESIGN*.md`, `doc/USER_GUIDE.md`,
`doc/PIPELINE_METRICS.md`, `doc/WECHAT_OPENCLAW.md`, `src/assistant/`,
`scripts/self-improve*`, `skills/`, and tests. Map the current user journey,
failure modes, incomplete commitments, and baseline before changing code.

Prioritize:

1. **Installation:** improve the existing `assistant init` and `init --check`
   path so a minimal installation works quickly, optional integrations can
   remain disabled, malformed `LLM_ROLES`/`LLM_MIXTURE` are diagnosed, and
   errors have actionable recovery instructions. Never expose or edit `.env`.

2. **Natural interaction:** users should communicate normally over WeChat,
   email, or CLI without learning internal action names. Keep the typed action
   registry as the only mutation surface. Preserve validation, deduplication,
   repair rounds, code-computed finance/health figures, never-delete stores,
   and human gates for sensitive or outward actions.

3. **Adaptive execution:** answer simple requests or execute one action
   directly without planning or MoA. Medium tasks use a short plan; complex or
   high-risk tasks use a persisted plan, milestones, bounded retries, recovery,
   verification, and human approval before irreversible side effects. Classify
   difficulty from action count, ambiguity, duration, external effects,
   finance/health mutations, publishing, and cross-user risk.

4. **Mandatory plan review:** add a hook using a local Cursor Agent with
   GPT-5.6. Every explicit plan from `plan_task`, planned `execute_task`,
   self-improvement, or future planning paths must be reviewed and revised
   before execution. Normal chat, single actions, and pipeline phases without a
   plan are exempt. Review intent alignment, assumptions, ordering, safety,
   privacy, side effects, recovery, verification, cost, and complexity.
   Resolve the valid model ID through the Cursor SDK; do not assume the display
   name is the API ID. Persist plan/review/revision/status/run ID/duration,
   bound retries and timeout, distinguish startup from run failure, dispose
   resources, and exclude secrets. If review remains unavailable, require
   explicit user approval before that planned task proceeds. Tests must prove
   that no plan-producing path bypasses this gate.

5. **Existing MoA:** improve, do not replace, `LLM_MIXTURE`, role routing,
   proposer failure handling, aggregation fallback, timeout, and circuit
   breaker. Activate MoA only when expected quality justifies cost and latency;
   keep chat single-model by default; preserve single-model fallback; record
   member, aggregator, token, timing, failure, and fallback data.

6. **Reliability and safety:** treat OpenClaw, the service daemon, and late
   replies as one contract. Prevent lost or duplicate replies, coordinate
   timeouts, resume interrupted work, and preserve per-user outbound routing.
   Audit concurrent writes from chat, pipeline, tasks, routines, and workers;
   extend per-user locking with concurrency tests. Never weaken tenant
   isolation, local-first privacy, evidence-backed profile writes, protected
   sections, deterministic website rendering, résumé approval, or reversible
   state.

7. **Self-evolution:** improve the existing loop using structured evidence from
   corrections, rejected/repaired actions, aborted tasks, latency/cost
   anomalies, and provider failures. Lessons remain attributable, reviewable,
   privacy-filtered, and retire-only. Code self-improvement creates a reviewable
   PR and is never auto-merged.

Use personal-agent's metrics in `doc/PIPELINE_METRICS.md`, `metrics.py`,
`tracing.py`, and `events_store.py`; do not use the PR/issue metrics in
`/rebase/JiusiCopilot_Introduction_双路径v2(1).pptx`. Measure quality,
reliability, cost, and time together: phase/chat/task success, degraded output,
p50/p95 latency, timeout and resume rates, first-attempt action success, repair
rate, schema validity, profile faithfulness/staleness/contradictions, digest red
precision/recall proxy, reading done-rate/MRR/NDCG, todo flow, delivery
success, tokens and estimated cost by run/phase/role/model/user, MoA overhead,
and GPT-5.6 review overhead. Save the baseline once and reuse it.

Research comparable public personal assistants, local-first agents, memory
systems, and multi-agent runtimes. For each useful mechanism, cite source
evidence and decide adopt/adapt/experiment/reject with privacy, cost, latency,
and maintenance tradeoffs.

Implement small evidence-backed changes, add focused tests, run targeted then
full relevant tests with `/rebase/.venv`, update docs/skills, and report
measured gains and remaining risks. Do not commit or push unless explicitly
requested. Do not begin with a broad rewrite.
```
# Personal-Agent Improvement Prompt

```text
/goal

Improve `/rebase/personal-agent` as a local-first daily personal assistant.

Do not use assumptions, tasks, metrics, datasets, or architecture from other
projects. Base every decision on the actual `personal-agent` implementation and
documentation.

## Product goal

Make the assistant:

- Easy to install and configure.
- Natural to use through WeChat, email, and CLI.
- Reliable enough for daily unattended operation.
- Effective across simple requests and complex multi-step tasks.
- More personalized through evidence-backed memory.
- Measurably better in quality, reliability, cost, and latency.
- Safe for personal data and reversible state changes.

The assistant's primary workflows are:

1. Daily nine-phase pipeline.
2. Natural-language chat and typed actions.
3. Agentic multi-step task execution.
4. Profile and personal-memory maintenance.
5. Daily digest and research recommendations.
6. Todo, reading, finance, health, reminder, and routine management.
7. Website and résumé synchronization.
8. Self-evolution from user corrections and execution failures.
9. Optional multi-user operation through OpenClaw and WeChat.

Do not redesign this as a coding agent, PR reviewer, or issue-answering system.

## Required initial investigation

Before changing code, inspect:

- `README.md`
- `doc/DESIGN.md`
- `doc/USER_GUIDE.md`
- `doc/PIPELINE_METRICS.md`
- `doc/DESIGN_SERVICE_LAYER.md`
- `doc/DESIGN_MULTI_USER.md`
- `doc/WECHAT_OPENCLAW.md`
- `src/assistant/orchestrator.py`
- `src/assistant/chat/`
- `src/assistant/actions/`
- `src/assistant/task_runner.py`
- `src/assistant/llm.py`
- `src/assistant/config.py`
- `src/assistant/metrics.py`
- `src/assistant/tracing.py`
- `scripts/self-improve.sh`
- `scripts/self_improve_prompt.md`
- Existing tests and operational skills

Document the current user journey, failure modes, latency bottlenecks, and
incomplete design commitments before proposing changes.

## Current priorities

### 1. Installation and first-run experience

The project already provides `assistant init` and `assistant init --check`.
Improve these existing paths rather than creating a parallel installer.

The target experience should be:

```bash
pip install ...
assistant init
```

The setup process should:

- Explain required and optional integrations.
- Validate configuration immediately.
- Detect malformed `LLM_ROLES` and `LLM_MIXTURE`.
- Test required services without exposing credentials.
- Provide actionable recovery instructions.
- Allow optional integrations to remain disabled.
- Leave the user with a working minimal assistant.

Do not read, print, edit, or commit existing `.env` secrets.

### 2. Natural and reliable interaction

The typed action registry must remain the single source of truth for
state-changing operations.

Improve the assistant so users can express ordinary requests without learning
action names or command syntax. The model may infer actions, but code must
execute them and report factual outcomes.

Preserve:

- Explicit user intent before mutations.
- Action validation.
- Duplicate prevention.
- Repair rounds after rejected actions.
- Code-computed finance and health summaries.
- Never-delete store semantics.
- Human approval for outward or sensitive operations.

Measure and improve:

- Action success on the first attempt.
- Repair frequency.
- Incorrect action selection.
- Chat completion latency.
- Timeout rate.
- Late-reply reliability.
- User corrections after an answer.

### 3. Adaptive task execution

Use different execution depth according to task complexity.

#### Simple requests

- Answer directly or execute one typed action.
- Do not create a plan.
- Do not invoke MoA.
- Minimize latency and cost.

#### Medium tasks

- Create a short structured plan.
- Review it through the mandatory GPT-5.6 hook.
- Execute with bounded steps and verification.

#### Complex or high-risk tasks

- Produce an explicit persisted plan.
- Review and revise it before execution.
- Use milestones, bounded retries, and recovery logic.
- Consider MoA when expected quality gains justify its cost.
- Require human approval before irreversible or external side effects.

Complexity signals should include:

- Number of required actions.
- External side effects.
- Finance or health data mutations.
- Public publishing or notifications.
- Ambiguity and missing information.
- Expected duration.
- Failure recovery difficulty.
- Cross-user risk in multi-user mode.

### 4. Mandatory GPT-5.6 plan review

Add a plan-review hook backed by a local Cursor Agent using GPT-5.6.

Every explicit plan produced by `personal-agent` must pass through this hook
before planned execution begins. This includes plans created through:

- `plan_task`
- Planned `execute_task` workflows
- Self-improvement planning
- Any new planning path introduced by this work

Ordinary chat responses, single typed actions, and pipeline phases that do not
generate plans are not plans and must not invoke the hook.

The review must evaluate:

- Whether the plan satisfies the user's actual request.
- Completeness and ordering.
- Unsupported assumptions.
- Safety and privacy risks.
- External side effects.
- Verification and recovery steps.
- Unnecessary cost or complexity.

Requirements:

- Resolve the available GPT-5.6 model identifier through the Cursor SDK rather
  than hardcoding an unverified display name.
- Run against `/rebase/personal-agent` using an explicit local runtime.
- Persist the plan, review result, revision, run ID, duration, and status.
- Incorporate actionable feedback and review the revised plan when necessary.
- Bound review retries and timeouts.
- Distinguish SDK startup failures from agent-run failures.
- Dispose of Cursor SDK resources correctly.
- Never expose secrets in the review prompt or logs.
- Do not start planned execution until review passes.
- If review remains unavailable, report it and require explicit user approval
  to continue that planned task.
- Do not block ordinary chat or the daily pipeline when no plan is involved.

Add tests proving that every plan-producing path invokes the hook and that
planned execution cannot begin before approval.

### 5. Existing MoA support

`personal-agent` already implements `LLM_MIXTURE`, role-based routing, proposer
failure handling, aggregation fallback, timeouts, and a circuit breaker.

Do not build a second MoA framework. Evaluate and improve the existing
implementation.

Focus on:

- Selecting MoA only for tasks where it improves quality.
- Keeping interactive chat single-model by default.
- Enforcing latency and cost budgets.
- Supporting independent proposer perspectives.
- Removing duplicated or unsupported conclusions during aggregation.
- Preserving surviving results when members fail.
- Recording member, aggregator, latency, token, and fallback metrics.
- Testing malformed configuration and partial provider failure.

MoA must remain optional and degrade safely to single-model execution.

### 6. Chat and OpenClaw reliability

Treat the WeChat bridge, service daemon, and asynchronous completion path as one
end-to-end contract.

Improve:

- Timeout coordination between bridge and daemon.
- "Still working" responses for long tasks.
- Delivery of eventual task results.
- Provider outage recovery.
- Duplicate response prevention.
- Restart and resume behavior.
- Per-user outbound routing.

Interactive requests must not silently disappear when execution exceeds the
initial response window.

### 7. State consistency and concurrency

Investigate concurrent writes from:

- Chat actions.
- Daily pipeline runs.
- Background tasks.
- Routines and reminders.
- Multi-user workers.

Ensure that writes to profile YAML, todos, finance, health, lessons, and related
git state cannot interleave or corrupt data.

Extend locking carefully and add concurrency tests. Preserve per-user
isolation.

### 8. Self-evolution

Improve the existing self-evolution loop rather than replacing it.

Use structured evidence from:

- Failed or repaired actions.
- User corrections.
- Aborted tasks.
- Timeout and latency anomalies.
- Repeated provider failures.
- Rejected profile operations.
- Cost anomalies.

Preserve these constraints:

- Personal lessons remain attributable and reviewable.
- Global lessons must not expose user-specific information.
- Lessons are retired rather than silently deleted.
- Code-level self-improvement creates a reviewable PR.
- Self-improvement changes are never auto-merged.
- No code change should be made without concrete evidence.

## Evaluation metrics

Do not use the PR-review or issue-answering metrics from
`/rebase/JiusiCopilot_Introduction_双路径v2(1).pptx`. Those metrics evaluate a
different product and different tasks.

Use the personal-agent metric design in:

`/rebase/personal-agent/doc/PIPELINE_METRICS.md`

and the existing implementation in:

- `src/assistant/metrics.py`
- `src/assistant/tracing.py`
- `src/assistant/events_store.py`

### Cross-cutting metrics

Record:

- Step success and degraded-output rate.
- Duration p50 and p95.
- End-to-end daily-run duration.
- Chat latency and timeout rate.
- Action first-attempt success.
- Repair-round rate.
- Output-format and schema validity.
- Resume success after interruption.

### Cost metrics

Record by run, phase, role, model, and user where applicable:

- Input and output tokens.
- Number of LLM calls.
- MoA proposer and aggregator calls.
- GPT-5.6 plan-review calls.
- Estimated monetary cost.
- Mean, median, and p95 cost.
- Cost per successful chat turn, task, and pipeline run.

### Personal-memory metrics

Measure:

- Profile-operation acceptance.
- Evidence faithfulness.
- Unsupported-claim rate.
- Contradiction rate.
- Staleness rate.
- Owner reverts or corrections.

### Digest and research metrics

Measure:

- Red-notification precision and recall proxy.
- Seen-store suppression effectiveness.
- Reading done rate.
- MRR and NDCG when enough implicit feedback exists.
- Source freshness and health.
- Backlog pressure.

### Todo and delivery metrics

Measure:

- Todo throughput, age, and stale rate.
- Auto-close false-positive rate.
- Expiry regret.
- Email and WeChat delivery success.
- Time to deliver after the scheduled run.

Do not optimize a single metric in isolation. Report quality, reliability,
cost, and latency together. Establish and save a baseline once, then reuse it
for later comparisons.

## Public-system research

Study relevant public personal assistants, agent runtimes, memory systems,
multi-agent frameworks, and local-first automation systems.

For each applicable mechanism, report:

- The problem it solves.
- How it works.
- Evidence from source code or official documentation.
- Applicability to `personal-agent`.
- Privacy implications.
- Cost and latency impact.
- Integration complexity.
- Adopt, adapt, experiment, or reject.

Research must inform concrete decisions, not produce a generic feature list.

## Hard constraints

- Preserve the local-first privacy model.
- Preserve typed action execution.
- Preserve evidence-backed profile claims.
- Preserve protected profile sections.
- Preserve deterministic public website rendering.
- Preserve résumé approval gates.
- Preserve never-delete and reversible-state conventions.
- Preserve graceful degradation for optional integrations.
- Never weaken multi-user isolation.
- Never auto-merge self-improvement changes.
- Do not modify `.env` or expose credentials.
- Do not commit or push unless explicitly requested.
- Use `/rebase/.venv` for Python commands.
- Every behavior change requires focused tests.

## Implementation process

1. Produce an evidence-backed architecture and gap assessment.
2. Establish the current metric baseline.
3. Prioritize improvements by user impact, risk, and implementation cost.
4. Implement small, coherent changes.
5. Add focused tests for every behavior change.
6. Run targeted tests during development.
7. Run the full relevant test suite before completion.
8. Measure quality, reliability, cost, and latency.
9. Update documentation and operational skills.
10. Report verified improvements, regressions, and remaining risks.

Do not start with a broad rewrite. Begin by identifying the highest-impact
shortcomings in the existing daily-assistant experience.
```
