# Daily-Pipeline Metrics — per-step measurement plan

Status: researched & designed 2026-07-09; not yet instrumented.
Sources: two verified sweeps — pipeline/LLM observability (Google SRE golden
signals & pipeline SLOs, Monte Carlo data-observability pillars, RAGAS,
LLM-as-judge/Zheng 2023, Langfuse cost conventions, BFCL, τ-bench pass^k,
Copilot acceptance rate, Mem0/MemoryAgentBench) and domain metrics (NDCG/MRR
& recsys survey arXiv:2312.16015, scikit-learn classification conventions,
SRE alerting precision, Kanban Guide flow metrics, DORA, Apple-MPP email
caveats, StreamProfileBench/HorizonBench/PERMA). Full citations in the
research transcripts; key URLs inline.

**Design rule:** every metric below is computable from artifacts the agent
already writes — `runs/<id>/*.json`, `events.db`, `profile/` git history,
`ops_log.jsonl`, `todos.yaml`/`reading_list.yaml` history, `daily-run.log`,
send/publish results — plus the owner's *implicit* actions (todo done,
reading done, reopens, chat replies). No explicit ratings required. The few
LLM-judge metrics need one cheap extra LLM call per artifact.

## 0. Cross-cutting run health (every phase)

| Metric | Definition | Source | Alert |
|---|---|---|---|
| Step success rate | successful executions / runs, over 7/30d — count "exit 0 but degenerate artifact" (empty observations, 0-section digest) as failure per SRE's *implicit errors* | `errors[]` in state + artifact sanity | any step <100% twice in a week |
| Step duration p50/p95 | wall-clock per phase, success vs failure tracked separately | timestamps in daily-run.log (add per-phase log lines) | p95 > 2× 30d baseline |
| Freshness SLO | "digest email delivered within Y min of 07:00, 99% of days" (SRE pipeline template) | resend/SMTP result time | breach |
| LLM cost/step | tokens in/out per call, per phase, trended (Langfuse convention: mutually exclusive buckets) | wrap `LLM.complete` to log usage | step cost > 3× baseline |
| Output-format validity | valid-JSON parse rate AND schema-compliance rate per LLM call (they differ; truncation shows up here first) | log in `complete_json` | any retry-then-fail |

## 1. collect

- **Per-source freshness**: `now − max(obs.ts)` per source (github/chrome/gmail) vs 26h lookback — a source with no fresh events two runs straight is silently broken (Monte Carlo pillar 1).
- **Volume anomaly**: observations per source per run vs rolling median ± k·MAD; zero-row collection is the classic silent failure (pillar 3). Today's `collector X: N observations` log lines already carry the number.
- **Collector error rate**: `errors[]` entries tagged `collect/*` per 30d.

## 2. profile update

- **Ops acceptance rate**: applied / (applied+rejected) per run — the Copilot-acceptance-rate analog; a falling rate means the prompt and the store have drifted apart. Source: `profile_update.json` + `ops_log.jsonl`.
- **Faithfulness (RAGAS-style)**: share of op evidence strings whose claims are supported by that day's observations — judge call over (ops, observations). Catches invention; our #1 safety property.
- **Staleness rate (HorizonBench-style)**: fraction of active profile facts contradicted by newer evidence (judge pass over profile vs last-30d events, monthly). StreamProfileBench's warning: systems over-retain stale interests — this is the counter-metric to our add-bias.
- **Contradiction rate (Mem0/MemoryAgentBench-style)**: assertions in profile.yaml that conflict with each other (e.g. the RFC #4366/#4534 incident) — judge pass over the rendered profile, weekly with consolidation.

## 3. resume sync

- **Compile pass rate**: latexmk success / attempts (already gated; make it a counted metric).
- **Approval acceptance rate**: approved / proposed updates — if the owner rejects half the proposed diffs, the relevance gate is miscalibrated.
- **Pending-age**: days a `resume_pending.json` sits unapproved (WIP-age analog; also feeds a todo).

## 4. github digest (triage)

- **Red precision (SRE alerting precision)**: reds the owner acted on within N days / total reds. "Acted on" = todo done, PR/issue interaction observed in later collects, or link click. The Workbook's alert-fatigue result is the justification: unactionable reds train the owner to ignore red.
- **Red recall (proxy)**: items triaged yellow/white that the owner nonetheless acted on ⇒ missed-red count (implicit false negatives).
- **Suppression effectiveness**: seen-store suppressed / total notifications — dedup doing its job (`suppressed_seen` already computed).

## 5. todos

- **Flow metrics (Kanban Guide)**: WIP (open count — already in stats), throughput (closed/week), work-item age distribution, stale-item rate (open > 21d — the ⏳ badge count).
- **Auto-close false-positive rate**: auto-closed todos that get re-created/reopened within 14d / auto-closed (implicit ground truth = reopen).
- **Expiry regret**: expired items later re-added manually — measures whether 30d staleness is too aggressive.

## 6. research digest

- **Done-rate (Precision@k analog)**: reading-list items marked done / items surfaced, per window — the digest's single most meaningful quality number (implicit relevance label).
- **MRR**: mean reciprocal rank of the first item per digest the owner eventually marks done — are the best papers on top?
- **NDCG@10** (binary rel: done=1) once enough done-marks accumulate: `DCG = Σ rel_i/log₂(i+1)` normalized by ideal ordering.
- **Backlog pressure**: reading-list open count trend (already in stats) — rising forever means over-surfacing (novelty/volume miscalibrated).
- **Source coverage & health**: share of configured sources that returned items (already tracked as `source_health` — promote to a metric with a 3-day-dead alert, which the design doc promised).

## 7. website

- **Publish success rate & change-fail** (DORA): `pushed` / attempts; interventions needed after publish.
- **Freshness**: hours since last successful publish vs daily cadence.
- **Diff-size anomaly** (volume-pillar transfer, labeled as convention not standard): alert when the rendered site diff is empty for N days (stale pipeline) or 10× normal (render bug).

## 8. deliver (email)

- **Delivery rate**: delivered/sent via Resend API responses + bounces — the only MPP-proof number besides clicks. Open rate is explicitly *not* tracked (Apple MPP preloads pixels; meaningless for one recipient).
- **Time-to-send** after 07:00 (feeds the freshness SLO).
- Optional later: per-link click tracking would upgrade digest metrics (P@k on industry items), at a privacy/complexity cost — decide explicitly.

## 9. curate

- **Decay activity**: entries decayed per run + reactivation rate (decayed then reactivated within 30d = premature decay — the consolidator-marked-interest-dormant incident is the motivating example).
- **Consolidation acceptance**: weekly consolidate ops applied/rejected + owner reverts of consolidation commits in the profile repo (git history = free audit label).

## Implementation sketch (when instrumented)

One `metrics.py` writing a row per (run, step, metric, value) into `events.db`
(new `metrics` table); phase nodes already have the numbers in hand — most
metrics are 1-line `record()` calls. Judge-based metrics (faithfulness,
staleness, contradiction) run inside the weekly consolidation slot, not
daily. A `## Health` footer section in the digest email surfaces the 7-day
view: step success, red precision, done-rate, ops acceptance, WIP/age,
freshness breaches. pass^k reruns and golden-data fixtures are CI-style
extras, only worth adding if a step turns flaky.
