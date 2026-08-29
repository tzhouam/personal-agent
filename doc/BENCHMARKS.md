# Benchmarks — the functionality→benchmark map, and the PA-Mix design (rev 3)

Two things live here: (1) a verified map from this agent's functionalities to
published agent benchmarks (each link checked against its canonical page,
2026-08), and (2) the design for **PA-Mix**, the evaluation that mixes those
benchmarks with agent-specific golden suites. Rev 2 incorporates the design
review: adapted tracks are now labeled **derived** and may never be reported
under the source benchmark's name; the memory track matches the actual
session/profile architecture; the phase surface is dropped (LangGraph nodes
are nested closures that continue downstream — including publish); results
get their own store (never `events.db`); budgets are marked as estimates;
statistics (holdout, repetitions, paired-delta tests, judge calibration) and
a mandatory dependency-injected sandbox are specified; and v1 scope is cut to
two PA-golden tracks plus ONE upstream runner. Rev 3 (final review round)
adds: the concrete executor-injection seam, full credential/network
isolation, paired-bootstrap alerting over per-item deltas, zero-scored
timeouts with classified-infra-failure exclusion rules, `official-subset`
labeling, raw-output privacy/retention, and drops events.db publishing
entirely.

---

## 1. The map

Each row is tagged: **[official]** the benchmark can run on this system under
its own protocol; **[derived]** only an adapted variant is feasible (results
must be labeled `derived-from-X`, never as X scores); **[reference]** useful
for ideas/comparison only (e.g. requires a browser this agent doesn't drive).

### Core agentic machinery

| Functionality (module) | Benchmarks |
|---|---|
| Typed action registry / tool calls (`agent/actions/`) | [BFCL v4](https://gorilla.cs.berkeley.edu/leaderboard.html) **[derived]** — BFCL scores AST-conformance against ITS tool schemas; our registry exposes prompt examples + required-field validation, so only a derived call-formatting track is honest · [τ²-bench](https://github.com/sierra-research/tau2-bench) **[official, Tier 3]** — runs standalone against a model; its conversing-under-policy shape is the closest public analogue to chat-driven actions · [GAIA](https://arxiv.org/abs/2311.12983) **[official, Tier 3]** |
| Agentic task runner (`task_runner.py`) | [TheAgentCompany](https://github.com/TheAgentCompany/TheAgentCompany) **[reference]** — its graded-checkpoint style informs our partial-credit scoring (verify the official evaluator before citing specifics) · [AgentBoard](https://hkust-nlp.github.io/agentboard/) **[reference]** — progress-rate metric idea · [AppWorld](https://github.com/StonyBrookNLP/appworld) **[derived]** — running it against a mock of OUR registry abandons AppWorld's apps/state env; that is a custom task-runner test, labeled as such |
| Profile memory (`profile_store.py`, `events_store.py`) | [LongMemEval](https://github.com/xiaowu0162/LongMemEval) **[official (model-level), Tier 3]** — NOTE: replaying it through `handle_turn` does NOT follow its protocol (SessionStore windows to ~10 turns/48h and chat facts don't flow into the profile), so agent-level memory is covered by the PA-golden suite instead · [LoCoMo](https://github.com/snap-research/locomo) **[official (model-level), Tier 3]** · [MemBench](https://arxiv.org/abs/2506.21605) **[reference]** |
| Self-evolution (`lessons_store.py`) | [PrefEval](https://prefeval.github.io/) **[official (model-level)]**; the lessons-store variant (preference stated → `learn_preference` → applied later) is **[derived]** and EASIER than PrefEval's implicit-inference task — both facts stated wherever reported · [LLF-Bench](https://arxiv.org/abs/2312.06853) **[reference]** · [LifelongAgentBench](https://arxiv.org/abs/2505.11942) **[reference]** |
| Workflows & routines (`workflow_store.py`, `routines.py`) | [WorkBench](https://github.com/olly-styles/WorkBench) **[reference]** — outcome-centric DB-state checking is the scoring idea our golden suite copies · [FlowBench](https://arxiv.org/abs/2406.14884) **[reference]** |

### Domain functionalities

| Functionality (module) | Benchmarks |
|---|---|
| Email chat (`email_channel.py`) — distinct from digest triage | [EmailBench](https://www.proofpoint.com/uk/blog/engineering-insights/introducing-emailbench-open-source-benchmark-email-understanding) **[official (model-level)]** — email understanding, not urgency ranking · [OfficeBench](https://arxiv.org/abs/2407.19056) **[reference]** |
| GitHub-notification digest triage (`tasks/github_digest.py`) | No suitable public benchmark identified — 🔴/🟡/⚪ urgency vs. a personal profile has no labeled public ground truth ([MIND](https://msnews.github.io/)'s click labels are engagement, not urgency — recasting them would fabricate ground truth). Covered by the S-layer live triage-precision proxy + a small hand-labeled golden set |
| Research phase (`research/`) | [LitSearch](https://arxiv.org/abs/2407.18940) **[official (model-level)]** · [ResearchArena](https://arxiv.org/abs/2406.10291) **[reference]** · [LaMP](https://lamp-benchmark.github.io/) **[official (model-level)]** — personalization probes · [MIND](https://msnews.github.io/) **[reference]** |
| `web_search` action (`platform/search.py`) | [BrowseComp](https://openai.com/index/browsecomp/) / [AssistantBench](https://assistantbench.github.io/) **[derived]** — live search is non-reproducible; runnable only against frozen recorded search fixtures, or as relative same-day A/B · [WebArena](https://webarena.dev/), [Mind2Web 2](https://osu-nlp-group.github.io/Mind2Web-2/) **[reference]** — browser-driving, out of scope |
| Finance ledger (`finance_store.py`) | [PersonaLedger](https://arxiv.org/abs/2601.03149) **[official (model-level), verify license]** · [FinBen](https://arxiv.org/abs/2402.12659) **[reference]** — general financial reasoning, not ledger correctness |
| Receipt/label/photo understanding (`platform/vision.py`) | [SROIE](https://rrc.cvc.uab.es/?ch=13) / [CORD](https://github.com/clovaai/cord) **[derived]** — mapped onto `log_transaction` params; note CORD's merchant field maps to our free-text `note`, so merchant scoring is containment, not exact-match · [DocVQA](https://www.docvqa.org/) **[official (model-level), Tier 3]** · [Nutrition5k](https://github.com/google-research-datasets/Nutrition5k) **[reference]** (RGB-D; our photos are RGB) |
| Health tracking (`health_store.py`) | [NutriBench](https://mehak126.github.io/nutribench.html) **[official (model-level)]** — macros from NL meal text, the best direct fit for the `log_meal` extraction step · [HealthBench](https://openai.com/index/healthbench/) **[official (model-level), Tier 3]** — health DIALOGUE quality, not store behavior · [QEVD](https://arxiv.org/abs/2407.08101) **[reference]** |
| Chinese assistant dialogue | [AlignBench](https://github.com/THUDM/AlignBench) **[official (model-level)]** · [SmartBench](https://github.com/vivo-ai-lab/SmartBench) **[official (model-level)]** · [SuperCLUE-Agent](https://github.com/CLUEbenchmark/SuperCLUE-Agent) **[reference]** |
| Reminders & scheduling (`notify.py`, `routines.py`) | [NATURAL PLAN](https://github.com/google-deepmind/natural-plan) **[official (model-level)]** — scheduling REASONING only; firing/delivery/idempotency are covered by this repo's own tests and the PA-golden suite · [ToolTalk](https://github.com/microsoft/ToolTalk) **[reference]** · [AppWorld](https://github.com/StonyBrookNLP/appworld) **[official, Tier 3]** |

### What no public benchmark covers (and what does instead)

"No suitable benchmark identified" (not a claim of nonexistence):

1. **Notification prioritization vs. a personal profile** → S-layer live
   triage precision + hand-labeled golden set.
2. **Personal expense categorization from NL** → PersonaLedger (new, verify)
   + golden set.
3. **Proactive/scheduled firing, delivery, retries, idempotency** → already
   deterministically covered by `test_routines.py`, `test_notify.py`,
   `test_durable_delivery.py`; PA-Mix does NOT duplicate them.
4. **Approval gates** → covered by task-tier tests; ditto.
5. **This repo's defining stateful properties** — dedup/temporal identity,
   never-delete, typed-op profile safety, delivery recovery — these ARE the
   product; they get the PA-golden suite (§2.3), not a public proxy.

Meta-reference: [Holistic Agent Leaderboard](https://hal.cs.princeton.edu/)
for sanity-checking Tier-3 numbers against public runs. (MemoryAgentBench was
suggested in review as a further memory benchmark — verify its canonical page
before adding.)

---

## 2. PA-Mix — the evaluation design

### 2.1 Measurement layers

- **M (model probes).** Official-protocol benchmark runs against whatever
  model an `LLM_ROLES` role routes to — the benchmark's own runner/format,
  no agent code. Reportable under the benchmark's name.
- **A (agent-surface tasks).** OUR machinery under test: prompts through
  `handle_turn`, actions through the registry executor — on hermetic scratch
  state. Everything here is either **derived-from-X** (adapted public items;
  never reported as X) or **PA-golden** (hand-built for this agent).
- **S (live observation).** Read-only snapshots of the production metrics the
  agent already records (`PIPELINE_METRICS.md`). Not a benchmark run; the
  hermeticity rule below applies to M/A runs, while S by definition *reads*
  live data and never writes or replays it.

A task's layer is part of its manifest; hybrid labels are not allowed (the
former "A+S triage" is two entries: a golden A task and an S snapshot).

### 2.2 Track contract (every track defines all of these)

Each track's `manifest.json` pins: source + version/content hash + license
note; layer + `official|derived|golden` label; sample item-ids + seed;
input contract (exact prompt/turn construction); output contract (what is
parsed, what a missing/timeout/infra-failure output scores — infra failures
score as `null` and exclude the item from the mean with a count reported,
never as 0); scorer (exact / AST / MAE mapped to [0,1] by a stated tolerance
curve / state-check / judge) including partial-credit rules and multi-valid-
answer handling; aggregation (per-item mean; dialogue-level tasks aggregate
per dialogue); repetitions; and the `LLM_ROLES` snapshot of the run.

### 2.3 The PA-golden suite (the heart of the A layer)

Hand-built scenarios asserting what this agent uniquely promises, scored by
END-STATE on scratch stores (the WorkBench idea) plus action-trace checks:

- **action-selection**: ~60 owner messages (zh-majority) → the right registry
  action with the right params (incl. "no action" cases and lesson-parameter
  application à la the derived-PrefEval flow).
- **dedup & temporal identity**: repeated/reworded/receipt-duplicate
  transactions and meals → exactly-one-record outcomes.
- **retrieval-compose**: month/period queries → composed answers citing
  retrieved records (and raw-records fallback on compose failure).
- **triage golden set**: ~50 hand-labeled GitHub notifications against a
  fixture profile → reported as macro-F1 + full confusion matrix + class
  distribution (never bare agreement — ⚪ dominates); labels carry a written
  rubric, and items where the owner's own two labeling passes disagreed are
  adjudicated or dropped (rubric + adjudication notes committed).
- **memory-through-the-agent**: multi-turn scratch sessions where facts are
  stated, then probed inside/outside the session window — asserting the
  DESIGNED behavior, not LongMemEval's protocol. Oracle: in-window probes
  must contain the fact; out-of-window probes pass iff the reply either
  (a) contains the fact via a store-backed retrieval action in the trace, or
  (b) contains no fabricated value and states it doesn't have/can't recall
  it (judged by string rules first, pinned judge only for the honesty arm).

Golden determinism rules: every run captures and persists one aware
`benchmark_now` in the configured `ZoneInfo` (without mutating process-global
TZ), injects it into temporal anchors, context/store defaults, and relative
fixture oracles, and retains the derived benchmark date beside each golden raw
turn for later re-scoring. Every scenario resets scratch state and scores
actions under explicit parameter-equivalence rules
(e.g. amount 45 == 45.0; date defaulting per the store's stated-or-auto
semantics).

Unit-test territory (routine firing, approval gates, delivery recovery) is
deliberately NOT duplicated here — `test/` already covers it
deterministically.

### 2.4 Tiers (budgets are estimates until measured; step 1 reports actuals)

- **T1 smoke** (every routing/prompt change): derived call-formatting (100),
  PA-golden action-selection (60), NutriBench **official-subset** (100),
  derived-CORD receipt extraction (50), derived-lessons preference flow (30),
  AlignBench **official-subset** (50). 390 items × n=3 repetitions ≈ 1.2k
  item-evaluations BEFORE judges/repairs — call it ~2k calls as the working
  estimate; step 3 measures actuals and, if over budget, T1 shrinks by
  cutting N (never n). `official-subset` is a distinct manifest label:
  subset scores are never comparable to full-benchmark or leaderboard
  numbers and the report card says so on the row.
- **T2 weekly**: full PA-golden suite; LitSearch + LaMP official subsets;
  frozen-fixture web track; S snapshot diffing.
- **T3 per-model-swap** (budget-gated, externally quotable): τ²-bench retail,
  GAIA validation, LongMemEval-S, DocVQA, HealthBench sample, BrowseComp
  sample — official protocols, unmodified runners.

### 2.5 Statistics (what makes a delta believable)

- **Repetitions & resampling unit**: every LLM-dependent track runs n≥3;
  repetitions are averaged **per item first** (the item is the resampling
  unit — treating N×n outputs as independent would pseudoreplicate).
  Report mean ± bootstrap 95% CI over item means. Tracks with too few items
  for a stable CI are marked *directional* and never alert.
- **Alerting**: regression is tested with a **paired bootstrap over
  per-item deltas** against the reference run (same items, same seeds) — a
  track alerts when the delta CI excludes zero, never on overlapping
  marginal CIs and never on a fixed point-drop. Card-level error policy:
  with ~15 tracks, expect false alarms; an alert is "confirmed" only if it
  reproduces on an immediate rerun (fresh repetitions), and the card labels
  unconfirmed alerts as such.
- **Failures**: a missing output or model timeout scores **0** (a degraded
  provider must not improve its mean by dropping hard items). Only
  objectively classified INFRA failures (harness crash, HTTP 5xx from the
  bench's own tooling, fixture-load errors) are excluded as `null`, with the
  excluded count on the row; a run with <90% item coverage on any track is
  invalid and never becomes a reference.
- **Holdout**: each sampled track keeps a *regression* half (inspected
  freely when debugging) and a *holdout* half. Governance, since a committed
  half is not blind: the holdout manifest stores only item IDS (content
  fetched from the upstream source at run time); per-item holdout outputs
  are not retained (scores only); holdouts rotate quarterly to
  never-before-used IDs and used IDs are recorded so they are never
  reselected. This deters — it cannot cryptographically prevent —
  overfitting in a single-operator repo, and says so.
- **Judges**: pinned model + prompt hash, ~10 committed calibration examples
  scored on every run (judge drift shows up as calibration drift), periodic
  human spot-checks; and since provider weights can drift under a pinned
  name, judged tracks are treated as relative even within a tier.
- **Attribution**: "the model changed" vs. "our code changed" is answered
  only by paired runs — same code across two routings, or same routing across
  two code versions. The report card links each delta to which variable moved
  (both moved ⇒ unattributed, says so).

### 2.6 Isolation & safety (mandatory, before any A-layer run)

- Fresh scratch `DATA_DIR` per run and `Settings(_env_file=None, …)` — the
  Settings class otherwise auto-reads the repo/CWD `.env` (config.py's
  `env_file` default), which would leak real credentials into a "blanked"
  profile. The bench profile explicitly empties EVERY outward credential:
  GitHub, SMTP/IMAP, Resend, website/marks/résumé remotes, WeCom, openclaw,
  announce, vision API, search APIs, Chrome history path.
- A **dependency-injected action sandbox** — a concrete seam, since
  `handle_turn` calls the registry's module-level `execute` directly (so a
  "separate executor" would be dead code): build-order step 1 adds an
  `executor_override` ContextVar to `agent/actions` that `execute` consults;
  the bench sets it to a sandboxed executor (deny-by-default allowlist;
  outward/risky actions replaced by recording fakes). Because repair rounds
  and the task runner call the same `execute`, the override covers them by
  construction; it is contextvar-scoped, so concurrent/normal use is
  untouched, and an isolation test asserts the override never leaks outside
  a bench run.
- **Network deny-by-default at the transport**: bench runs set proxy/socket
  guards (the pytest-style socket blocker) allowing only the LLM endpoints
  (+ the recorded-fixture transport for the web track) — handler fakes and
  blanked credentials alone do not bound every code path.
- No `phase_run` surface: LangGraph nodes are nested closures that continue
  through downstream nodes (including website publish and delivery). Pipeline
  behavior is benchmarked only through the pure functions phases call (e.g.
  `build_digest` with injected deps), never by entering the graph.
- Network: only the LLM endpoint(s); the web track substitutes a recorded
  search-fixture transport.

### 2.7 Results & reporting

- Results live in `bench/results/<run-id>/` — manifest snapshot, raw model
  outputs (retained for re-scoring), per-item scores (JSONL), and a summary
  card. NOT in `events.db`. Raw outputs are sensitive-adjacent (they can
  quote fixture content and model behavior): the directory is git-ignored,
  created 0700, secrets-masked through the existing `mask_secrets` pass,
  and pruned past 10 runs (summaries kept indefinitely).
- The report card is per-track (no composite number), fixed order, each row:
  score ± CI, n×N, layer/label, route served, delta vs. reference run with
  attribution tag.
- No production-store publishing at all (an earlier `bench publish` into
  `events.db` idea contradicted both the shared-store rule and the
  delete-`bench/`-to-roll-back claim — dropped). The report card is a file;
  the owner reads it with `assistant bench report`.
- A `BENCH_ENABLED` guard plus CLI-only entry (`assistant bench`) — nothing
  in the daemon or pipeline ever triggers benchmarks; rollback is deleting
  `bench/` and the `executor_override` seam commit (which defaults to
  no-op), leaving zero residue in production stores.

### 2.8 v1 scope (cut per review) and build order

v1 = **PA-golden action-selection + dedup tracks** (pure A-layer, fake-LLM
tests for the harness itself) + **ONE upstream runner** (NutriBench
official-subset — proves the adapter/provenance/labeling mechanism) + the
isolated results store and report card. Everything else follows only after
v1's budgets, CIs, and the isolation tests are validated:

1. `bench/` scaffolding: surfaces (`role_probe`, `chat_turn`), the
   `executor_override` seam in `agent/actions` + sandboxed executor +
   isolation tests, network guard, results store, manifest format.
2. PA-golden action-selection + dedup (fixtures committed).
3. NutriBench official-subset runner; measure real T1 cost (AlignBench
   joins in step 4 once the judge-calibration machinery exists).
4. Derived tracks (call-formatting, CORD, lessons) with `derived` labeling.
5. T2 (LitSearch/LaMP, triage golden set, memory-through-the-agent,
   frozen-fixture web) and the paired-run attribution tooling.
6. T3 official runners, HAL cross-check, `bench publish`.


---

## 3. v1 implementation status

Built (`src/assistant/bench/`, `assistant bench run|report`, gated on
`BENCH_ENABLED`): the executor_override seam, hermetic bench settings (LLM
config preserved, outward credentials blanked, key-free route fingerprint),
deny-by-default action sandbox + IP-resolving network guard, role_probe/
chat_turn surfaces, §2.5 statistics (per-item repetition averaging, seeded
bootstrap CIs, paired-delta with comparability gating on fixture-hash +
route fingerprint), the two PA-golden tracks (action-selection with seeds
and bench-strict success scoring, all-or-nothing dedup) and the NutriBench
runner (always `derived` — custom prompt/scorer, provenance validated
against the data hash + ids), isolated results with retained masked raw
outputs and a permanent summary archive. Two GPT design rounds + two code
rounds; all must-fixes applied.

Accepted residuals (recorded, not fixed in v1):
- `network_guard`/`RunStore.prune` are single-run safe, not concurrency-safe
  — the CLI runs one bench at a time; concurrent runs are out of scope.
- Credential blanking is heuristic over field-name patterns; a future oddly
  named credential could slip. Mitigated by the sandbox (outward actions are
  faked regardless) and the network guard (deny-by-default), so a leaked
  credential still can't be used. A fail-closed allowlist of LLM-only fields
  is the follow-up.
- `_run_reps` classifies all setup exceptions as infra and all body
  exceptions as agent-failure; setup here is only object construction (no
  network), so this is defensible but coarse.
- `mask_secrets` masks known token formats only; raw outputs are additionally
  protected by the blanked-credential profile (they should not contain
  secrets in the first place) and 0700 dirs.
- No official-protocol runners yet (τ²/GAIA/DocVQA Tier 3) — v1 is the two
  golden tracks + the one derived NutriBench runner, per the cut scope.
