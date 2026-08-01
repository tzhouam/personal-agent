# Benchmarks — the functionality→benchmark map, and the mixed suite design

Two things live here: (1) a verified map from this agent's functionalities to
published agent benchmarks (each link checked against its canonical page,
2026-08), and (2) the design for **PA-Mix**, a composite evaluation that mixes
those benchmarks into something that actually measures *this* agent — its
action registry, its memory design, its model routing — rather than a bare
model.

---

## 1. The map

### Core agentic machinery

| Functionality (module) | Benchmarks |
|---|---|
| Typed action registry / tool calls (`agent/actions/`) | [BFCL v4](https://gorilla.cs.berkeley.edu/leaderboard.html) — schema-conformant function calling, AST-scored · [τ²-bench](https://github.com/sierra-research/tau2-bench) — tool use *while conversing under policy* (closest shape to chat-driven actions) · [GAIA](https://arxiv.org/abs/2311.12983) — general-assistant questions needing tools + browsing |
| Agentic task runner (`task_runner.py`) | [TheAgentCompany](https://github.com/TheAgentCompany/TheAgentCompany) — long-horizon tasks, checkpointed partial credit · [AgentBoard](https://hkust-nlp.github.io/agentboard/) — progress-rate metric for incremental adaptation |
| Profile memory (`profile_store.py`, `events_store.py`) | [LongMemEval](https://github.com/xiaowu0162/LongMemEval) — multi-session memory incl. temporal reasoning and knowledge updates · [LoCoMo](https://github.com/snap-research/locomo) — ~300-turn dialogues · [MemBench](https://arxiv.org/abs/2506.21605) |
| Self-evolution (`lessons_store.py`, `tasks/evolve.py`) | [PrefEval](https://prefeval.github.io/) — stated preferences applied many turns later · [LifelongAgentBench](https://arxiv.org/abs/2505.11942) · [LLF-Bench](https://arxiv.org/abs/2312.06853) — improving from verbal feedback |
| Workflows & routines (`workflow_store.py`, `routines.py`) | [WorkBench](https://github.com/olly-styles/WorkBench) — outcome-centric DB-state checks over calendar/email/todo tools · [FlowBench](https://arxiv.org/abs/2406.14884) — adherence to explicit workflow specs |

### Domain functionalities

| Functionality (module) | Benchmarks |
|---|---|
| Email chat + digest triage (`email_channel.py`, `tasks/github_digest.py`) | [WorkBench](https://arxiv.org/abs/2405.00823) · [OfficeBench](https://arxiv.org/abs/2407.19056) · [EmailBench](https://www.proofpoint.com/uk/blog/engineering-insights/introducing-emailbench-open-source-benchmark-email-understanding) (2026, dedicated email understanding) |
| Research phase (`research/`) | [LitSearch](https://arxiv.org/abs/2407.18940) — literature retrieval from natural questions · [ResearchArena](https://arxiv.org/abs/2406.10291) — discover + importance-rank papers · [LaMP](https://lamp-benchmark.github.io/) — personalized news/headlines · [MIND](https://msnews.github.io/) — news relevance vs. user history |
| `web_search` action (`platform/search.py`) | [BrowseComp](https://openai.com/index/browsecomp/) · [AssistantBench](https://assistantbench.github.io/) — realistic personal web questions · [WebArena](https://webarena.dev/) / [Mind2Web 2](https://osu-nlp-group.github.io/Mind2Web-2/) |
| Finance ledger (`finance_store.py`) | [PersonaLedger](https://arxiv.org/abs/2601.03149) — personal-transaction QA/categorization · [FinBen](https://arxiv.org/abs/2402.12659) as a general backstop |
| Receipt/label/photo understanding (`platform/vision.py`) | [SROIE](https://rrc.cvc.uab.es/?ch=13) + [CORD](https://github.com/clovaai/cord) — receipt key-information extraction · [DocVQA](https://www.docvqa.org/) · [Nutrition5k](https://github.com/google-research-datasets/Nutrition5k) — calories/macros from food photos |
| Health tracking (`health_store.py`) | [NutriBench](https://mehak126.github.io/nutribench.html) — macros from natural-language meal descriptions (the single best fit for `log_meal`) · [HealthBench](https://openai.com/index/healthbench/) — physician-rubric health dialogue · [QEVD](https://arxiv.org/abs/2407.08101) — fitness coaching |
| Chinese assistant dialogue (WeChat surface) | [SmartBench](https://github.com/vivo-ai-lab/SmartBench) — Chinese phone-assistant scenarios incl. notification management · [AlignBench](https://github.com/THUDM/AlignBench) — Chinese response quality · [SuperCLUE-Agent](https://github.com/CLUEbenchmark/SuperCLUE-Agent) |
| Reminders & scheduling (`notify.py`, `routines.py`) | [NATURAL PLAN](https://github.com/google-deepmind/natural-plan) — NL calendar scheduling · [ToolTalk](https://github.com/microsoft/ToolTalk) — multi-turn calendar/alarm/email tool conversations · [AppWorld](https://github.com/StonyBrookNLP/appworld) — 9 simulated everyday apps, state-based unit tests |

### Confirmed gaps (no established benchmark)

1. Inbox/notification **prioritization** per se (EmailBench/WorkBench nearest).
2. Personal **expense categorization** from natural language (PersonaLedger is
   new; SROIE/CORD cover extraction only).
3. **Proactive/scheduled** behavior — nothing tests cron-like firing itself.
4. **Human approval gates** in agent loops (TheAgentCompany checkpoints are the
   closest proxy).

The agent's own `PIPELINE_METRICS.md` self-measurement (triage precision,
reading done-rate, acceptance rates) is effectively a private benchmark for
gap 1 — PA-Mix leans on that below.

Meta-reference: the Princeton [Holistic Agent Leaderboard](https://hal.cs.princeton.edu/)
aggregates GAIA/τ-bench-class results with standardized harnesses and cost
reporting; useful for sanity-checking our numbers against public runs.

---

## 2. PA-Mix — the mixed benchmark suite (design)

### 2.1 What is being measured, exactly

Three distinct layers get conflated when people "run benchmarks", so PA-Mix
separates them explicitly — every task in the suite belongs to exactly one:

- **M (model probes).** The raw capability of whatever model `LLM_ROLES`
  currently routes to a role — no agent code in the loop. Cheap, runs on any
  routing change. (NutriBench on the `chat` role, SROIE/CORD on the vision
  path, BFCL-style call formatting on `chat`, LitSearch-style ranking on
  `research`.)
- **A (agent-surface tasks).** The agent's own machinery: prompts through
  `handle_turn`, actions through the registry executor, pipeline phases on
  seeded scratch data. This is where the typed-op safety, retrieval-compose,
  repair rounds, and lessons injection actually get exercised — the layer no
  public leaderboard measures for us.
- **S (self-measurement).** The live metrics the agent already records
  (`PIPELINE_METRICS.md`): triage precision proxies, reading done-rate,
  profile-op acceptance. Not a benchmark run at all — PA-Mix just snapshots
  them so regressions in the real deployment sit next to lab scores.

### 2.2 Suite composition

Fixed, seeded subsets — never the full public sets (cost) and never live data
(the repo's scratch-dir test invariant applies to benches too). Sizes chosen
so T1 ≈ a few hundred LLM calls, T2 ≈ low thousands.

**Tier 1 — smoke (run on every model-routing/prompt change; target < ~30 min,
< ~500 calls):**

| Track | Source | Layer | N | Scoring |
|---|---|---|---|---|
| actions-call | BFCL v4 subset re-rendered onto OUR registry schema | M | 100 | AST/exact param match |
| chat-actions | hand-derived τ²-style dialogues over todo/reminder/finance actions | A | 30 | end-state check on scratch stores |
| meal-log | NutriBench subset (zh-translated half) | M | 100 | macro MAE within tolerance |
| receipt | CORD subset → `log_transaction` params | M | 50 | amount/date/merchant exact |
| prefs | PrefEval subset threaded through the LESSONS store | A | 30 | preference applied in action params (the `lessons` claim) |
| zh-quality | AlignBench subset (assistant categories only) | M | 50 | LLM-judge, pinned judge model |

**Tier 2 — weekly (scheduled like the consolidation pass):**

| Track | Source | Layer | N | Notes |
|---|---|---|---|---|
| memory | LongMemEval-S sessions replayed via `handle_turn`+SessionStore, questions asked at the end | A | 50 q | tests OUR session/profile plumbing, not just the model |
| triage | MIND-derived impressions recast as GitHub-notification triage 🔴/🟡/⚪ + S-layer live triage-precision snapshot | A+S | 100 | gap-1 stand-in |
| task-runner | AppWorld subset mapped to `execute_task` with a mock action registry | A | 20 | checkpointed partial credit à la TheAgentCompany |
| research-rank | LitSearch subset on the `research` role + LaMP personalization probe | M | 100 | recall@k / judge |
| web | AssistantBench subset through the `web_search` action | A | 25 | answer match |
| routines | in-house generator (gap 3): synthetic routine specs → assert correct fire/skip/condition behavior on a scratch store with a frozen clock | A | 40 | deterministic |
| approval-gate | in-house generator (gap 4): tasks whose plans contain risky actions → assert the pause, never the side effect | A | 20 | deterministic |

**Tier 3 — occasional (per major model swap; budget-gated):** GAIA validation
split, τ²-bench retail domain as published (for a public-leaderboard-comparable
number), BrowseComp sample, HealthBench sample, LoCoMo one-dialogue deep run.

### 2.3 Adapters (how mixing actually works)

One thin adapter per source keeps the mix honest:

```
bench/
├── adapters/          # one module per source: download, sample(seed), render
│   ├── nutribench.py  #   → [{prompt|turn|phase-input, expected, scorer}]
│   ├── bfcl.py        #   re-renders tool specs onto ACTIONS' prompt_block
│   ├── prefeval.py    #   preference → learn_preference turn → later probe
│   └── …
├── surfaces.py        # the three run targets: role_probe(role, prompt),
│                      # chat_turn(seeded scratch dir), phase_run(phase, seed)
├── scorers.py         # exact / AST / MAE / state-check / pinned-LLM-judge
└── run.py             # `assistant bench --tier 1 [--track meal-log]`
```

Rules that keep results meaningful:

- **Same seeds forever.** A track's subset is sampled once with a recorded
  seed and pinned by item-id manifest committed to the repo; score drift then
  means the SYSTEM changed, not the sample.
- **Judges are pinned.** LLM-judged tracks (AlignBench, LaMP) name an exact
  judge model + prompt hash in the manifest; a judge change resets that
  track's history.
- **A-layer runs are hermetic.** Every run gets a fresh scratch `DATA_DIR`
  seeded by the adapter (profile, stores, sessions) — mirroring
  `test/conftest.py`'s invariant. No network except the LLM endpoint and (web
  track only) the search API.
- **Chinese is not an afterthought.** Every A-layer track runs its prompts in
  the language mix the agent actually sees (zh-majority); M-layer tracks keep
  the source language plus a zh-translated half where the source is
  English-only (translated once, committed, marked as such).
- **Contamination is assumed.** Public-set scores are treated as *relative*
  (this routing vs. that routing), never as absolute capability claims; only
  Tier-3 published-protocol runs are quotable externally.

### 2.4 Scoring and the report card

- Each track yields `score ∈ [0,1]` plus cost (calls, tokens, wall time —
  recorded via the existing `moa`/metrics sinks in `events.db`).
- **No single composite number.** A weighted composite hides exactly the
  regressions this exists to catch. The report is a fixed-order card of
  per-track scores with deltas vs. the previous run and vs. the best-ever run,
  written to `events.db` (`bench` metric rows) and rendered into the digest's
  Health footer when a track drops >5 points — the owner learns about a
  regression the same way they learn about everything else.
- Per-role attribution: every track records which `LLM_ROLES` route served it,
  so "research got worse" is distinguishable from "the research MODEL got
  worse" (M-track same-role comparison isolates it).

### 2.5 What PA-Mix deliberately does not do

- No WebArena/OSWorld-style hosted environments (heavy infra; the agent
  doesn't drive a browser).
- No benchmark of the résumé/website publish path beyond the existing
  deterministic tests — publishing has human gates by design and a benchmark
  that auto-publishes would violate the safety model.
- No health-advice grading beyond the HealthBench sample — the agent's stance
  is wellness-not-diagnosis; a full medical eval would over-claim.
- No leaderboard submissions from Tier 1/2 numbers (see contamination rule).

### 2.6 Build order

1. `bench/` scaffolding + `surfaces.py` + two adapters (NutriBench, CORD) —
   proves the M layer end to end. (small)
2. `chat_turn` surface + prefs and chat-actions tracks — proves the A layer
   and the lessons-store claim. (medium)
3. Gap generators (routines, approval-gate) — pure in-house, deterministic,
   no downloads. (small)
4. Tier-2 memory + triage + task-runner adapters. (the big one)
5. `assistant bench` CLI + report card + digest-footer wiring. (small)

Each step lands with tests (adapters are pure functions over committed
fixtures; surfaces get fake-LLM tests like `test_chat.py`'s Recorder pattern).
