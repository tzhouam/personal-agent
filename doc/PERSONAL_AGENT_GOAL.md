/goal

Improve `/rebase/personal-agent` as a local-first daily personal assistant.
Use only this repository's tasks, architecture, and metrics. Inspect its docs,
`src/assistant/`, self-improvement scripts, skills, and tests. Establish the
baseline and identify concrete friction before editing.

Mandatory plan review during this goal (amended 2026-07-18, owner directive:
NO outside tools — no cursor-agent or any third-party CLI anywhere in the
process): every plan you create while working on this goal — research,
design, implementation, testing, or evaluation — must be reviewed BEFORE
execution through the local reviewer, `scripts/review_plan.py`, which runs
the critique on the owner's own configured endpoints (the `LLM_REVIEW` slot
in `.env`; falls back to the LLM_MIXTURE aggregator, then ANTHROPIC_MODEL).
Write the plan, run the reviewer, incorporate actionable feedback, and
execute only after acceptance (exit 0 = approved; exit 1 =
approve-with-changes: incorporate the must-fixes, then proceed; exit 2 =
revise and re-review). Never skip review because a plan seems small. If the
reviewer is unavailable (exit 3), stop and report the blocker instead of
executing unreviewed. This is a development-process rule; plan review is not
a runtime feature of the assistant.

Priorities:

1. Improve the existing `assistant init` and `init --check` flow so a minimal
installation works quickly, optional integrations may stay disabled, malformed
`LLM_ROLES`/`LLM_MIXTURE` are diagnosed, and configuration failures have
actionable recovery steps. Never expose or edit `.env`.

2. Make WeChat, email, and CLI interaction natural without requiring action
names. Keep the typed action registry as the only mutation surface and preserve
validation, deduplication, repair rounds, code-computed finance/health values,
never-delete stores, and approval for sensitive or outward actions.

3. Adapt execution depth to difficulty. Simple requests answer directly or run
one action without planning or MoA. Medium tasks create a short plan. Complex
or risky tasks persist a plan with milestones, bounded retries, recovery,
verification, and approval before irreversible effects. Classify difficulty
from ambiguity, action count, duration, external effects, finance/health
changes, publishing, and cross-user risk.

4. Improve rather than replace the existing `LLM_MIXTURE`, role routing,
timeouts, circuit breaker, proposer failure handling, and aggregation fallback.
Use MoA only when expected quality warrants its cost and latency, keep chat
single-model by default, preserve single-model fallback, and record per-member
tokens, timing, failures, aggregation, and fallback.

5. Treat OpenClaw, the service daemon, and asynchronous late replies as one
reliability contract. Prevent lost or duplicate replies, coordinate timeouts,
resume interrupted work, and preserve per-user outbound routing. Audit
concurrent writes from chat, pipeline, tasks, routines, and workers; extend
per-user locking and add concurrency tests. Never weaken tenant isolation,
local-first privacy, evidence-backed profile writes, protected sections,
deterministic website rendering, résumé approval, or reversible state.

6. Improve self-evolution using structured evidence from corrections,
rejected/repaired actions, aborted tasks, provider failures, and cost/latency
anomalies. Keep lessons attributable, reviewable, privacy-filtered, and
retire-only. Code self-improvement must create a reviewable PR and never merge
automatically.

Use `doc/PIPELINE_METRICS.md` and existing metric/tracing code. Measure
quality, reliability, cost, and time together: phase/chat/task success,
p50/p95 latency, timeouts, first-attempt action success, repair/schema
validity, profile faithfulness, digest/research/todo/delivery outcomes, tokens
and estimated monetary cost by run/phase/role/model/user, cost per successful
task, end-to-end run time, and MoA overhead. Save the baseline once and reuse
it; report quality, cost, and latency together and prefer Pareto improvements.

Make small evidence-backed changes, add focused tests, run targeted and
relevant full tests with `/rebase/.venv`, update documentation/skills, and
report measured gains and remaining risks. Do not commit or push unless
requested. Do not begin with a broad rewrite.
