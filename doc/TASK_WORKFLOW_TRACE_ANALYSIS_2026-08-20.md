# Autonomous task workflow trace analysis — 2026-08-20

Scope: the retained autonomous-task records/traces in the active tenant. This
document intentionally omits owner content, meeting identifiers, and private
URLs.

## Observed failures

| Trace | Outcome recorded | What actually happened | Cost signal |
|---|---|---|---|
| Older-conversation lookup | `done` / `partial` | No action ran. The model said history was unavailable although matching retained session/reminder records existed. | 4 LLM calls, about 124 s |
| Multi-source event research | `aborted` | Sequential search repeated a query, never produced the requested report, and inferred an unsupported event year. | 26 LLM calls, about 309 s; 12 searches |

The daily nine-phase pipeline was not the weak link: it already uses durable
artifacts and phase gates. The general task runner was open-ended and steered
from prose action outcomes, so it could miss a capability, multiply synthesis
calls, repeat work, and still report a misleading terminal status.

## Implemented workflow

1. Route known older-history requests deterministically to a tenant-scoped
   retrieval action before model reasoning.
2. For other multi-step tasks, draft a short plan whose steps name concrete
   registry actions and observable outputs; validate action availability and
   required parameters before execution.
3. Execute the first validated read-only step directly. Batch up to eight web
   queries concurrently and return exact source records instead of synthesizing
   every query separately.
4. Feed structured action results (`ok`, data, confidence, provenance) to a
   single-model controller. Suppress exact duplicate successful actions and
   bound accumulated controller context.
5. Gate completion on milestone coverage and evidence use. Reject a retrieved
   report once if it has no citation or introduces a year/URL absent from the
   evidence; terminal status is `done/full`, `partial`, or `blocked`.
6. Record tool spans plus task coverage/evidence/failure/duplicate metrics, and
   feed partial/blocked task evidence and task-trace latency back into weekly
   self-evolution.

## Expected effect and measurement

The historical-lookup shape now needs one controller call after deterministic
retrieval, instead of four model calls with no retrieval. A planned web-research
shape needs assessment + plan + final synthesis (three model calls) and one
parallel batch action, rather than a model turn and synthesis per query. These
are structural call-count reductions; end-to-end production latency should be
compared from task traces after deployment.

Track over a rolling window:

- full / partial / blocked rate;
- milestone coverage and cited-evidence count;
- failed steps and duplicate actions suppressed;
- task wall time, LLM calls, and tool calls;
- unsupported-year/URL and missing-citation correction turns.

Acceptance target: improve full-completion rate without increasing unapproved
side effects; lower median LLM calls and wall time; no report may claim full
completion with incomplete milestones or uncited retrieved evidence.
