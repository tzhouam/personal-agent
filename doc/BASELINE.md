# Metric baseline — 2026-07-17 (30-day window)

The saved reference point the goal work measures against ("save the baseline
once and reuse it"). Raw numbers: [`metrics-baseline-2026-07-17.json`](metrics-baseline-2026-07-17.json).
Computed read-only from the live deployment's `users/<uid>/events.db` metrics
table, `tasks/*.json` records, and `runs/*/trace.jsonl` LLM spans; tenants are
anonymized (`user-1` = the active owner, `user-2` = a freshly added tenant).
Counts, durations, and token totals only — no personal content.

## Headline numbers (user-1, the active tenant)

| Area | Baseline |
|---|---|
| Daily runs (30d) | 12 runs · median 12.1 min · p95 20.3 min · 1 run with errors |
| Slowest phases (p50) | research 436 s · todos 152 s · profile 95 s · digest 77 s |
| Chat latency | **p50 41 s · p95 197 s · max 508 s** — 44/123 turns > 60 s, 14 > the 120 s bridge wait |
| Chat quality | 123 turns · 73 with actions · **2 repair rounds total** (first-attempt success ≈ 98%) · 2 turns with unresolved failures |
| Tasks | 3 total, 3 done, median 5 steps |
| Profile ops | 102 applied / 22 rejected (82% acceptance) |
| Digest | 323 reds / 161 suppressed by seen-store |
| Delivery | website 11/12 pushed · **email 9/12 sent** |
| LLM cost proxy | 14.4 calls/run · ~50k prompt + ~42k completion tokens/run · prompt-cache read ratio **7%** |

user-2's single run failed across all phases (the pre-`PERSONAL_ENV_FIELDS`
credential-isolation incident: no personal creds → every collector/deliver step
errored); it contributes no meaningful latency/cost data.

## What the baseline says (friction ranking)

1. **Chat latency dominates UX pain** — a third of turns exceed a minute; the
   p95 sits above the bridge's 120 s wait, exercising the late-reply path.
2. **Task execution pays full depth for everything** — the fixed 12-turn loop +
   MoA-configured `task` role has no cheap path for trivial tasks and no
   approval gate for outward effects.
3. **Action selection is already reliable** — 2 repair rounds in 123 turns;
   improving first-attempt success is not the bottleneck.
4. **Email delivery** (9/12) and the ~7% prompt-cache read ratio are the next
   reliability/cost items.

## Recompute

Run the same read-only sweep (any later date) and compare against this file:

- metrics rows: `SELECT run_id, step, name, value, ts FROM metrics WHERE ts >= <cutoff>`
  per `users/<uid>/events.db`; p50/p95 over `duration_s` by step; sums over
  `ops_applied/ops_rejected`, `red/suppressed`, `pushed/email_sent`;
  `chat_turn` rows grouped per turn for latency/repair/failure counts.
- tasks: status + step counts over `users/<uid>/tasks/task-*.json`.
- tokens: sum `prompt_tokens`/`completion_tokens`/`cache_read_tokens` over
  `name == "llm"` spans in `users/<uid>/runs/run-*/trace.jsonl`.

Never write to the live data dir while doing this (owner rule: tests and
experiments use scratch dirs only).
