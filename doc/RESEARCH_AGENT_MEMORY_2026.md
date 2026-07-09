# Agent Memory in 2026 → a better profile for the owner

Owner: Taichang Zhou (tzhouam)
Status: research report + profile-v2 design — **implemented 2026-07-09**
(P1–P6 all landed; first real consolidation run applied 15 ops, cron job
`weekly-consolidate` Sun 08:00 HKT). Known limitations: evidence pruning is
conservative by design (a rewrite may never lose cited URLs, so verbose
evidence lists shrink slowly); cross-section contradictions (e.g. RFC #4366
in `experience:` vs #4534 in projects) are flagged in notes but never
auto-resolved against the protected sections.

**Goal.** The daily pipeline builds `profile.yaml` from activity observations,
but the result under-represents the owner: major contributions are buried as
scattered evidence bullets, and correlated work is fragmented into separate
entries. This doc (1) diagnoses why with concrete examples from the live
profile, (2) surveys the outstanding 2026 papers and projects on agent
memory, and (3) proposes concrete mechanisms for the profile pipeline.

---

## 1. Diagnosis — what the live profile gets wrong today

All examples from `~/.personal-agent/profile/profile.yaml` as of 2026-07-09.

**D1 · Evidence-dump, no abstraction (details kept, contributions lost).**
The `Python` skill holds 10 evidence bullets that are actually *major
engineering contributions* ("Developed foundational AR and Diffusion GPU
model runners", "Led rebase to v0.14.0", "Implemented multi-request
streaming") — filed as flat citations under a generic language skill. Nothing
ever promotes accumulated evidence into a contribution statement. Meanwhile
real skills the evidence proves (LLM inference systems, CUDA/GPU serving,
multi-agent orchestration) don't exist as entries; instead GitHub language
buckets do ("Java — evidence: comp3111-lab1-2021f", a 2021 course lab).

**D2 · Fragmentation (one line of work → many entries).**
- The **BDE / DreamZero KV-cache** initiative is spread over `bde-private`
  (private repo), `BDE_doc` (its docs repo, highlight-less), and two
  near-duplicate highlights inside `vLLM-Omni` — with **inconsistent RFC
  numbers** across entries (#4534 vs #4366).
- The **agent-automation** line of work is split across `personal-agent`,
  `vllm-omni-copilot`, and a `vLLM-Omni` highlight about the rebase agent.
- Core vLLM-Omni fixes (PCM streaming issue #4411, rebase follow-up PR
  #4830) are misattributed to `vllm-omni-images` — a Docker images repo —
  because the day's push landed there.

**D3 · Additive-only updates.** The op set (`add_evidence`, `add_skill`,
`add_project`, `update_highlight`, `bump_last_seen`, `mark_dormant`) can only
append or touch timestamps. There is no merge, no rewrite, no promote — so
duplicates accumulate ("Resolved bug… #4411" appears twice in one entry,
"Authored the BDE Phase 1 RFC" twice in another) and misattributions are
permanent.

**D4 · One-day myopia.** `update_profile()` sees the current profile + ~120
of *today's* observations. A contribution arc spanning 15 PRs over 3 months
is never visible in any single prompt, so the model can only add today's
crumb, never see the loaf.

**The counterfactual is already in the file:** the manually-written
`experience:` section reads like a strong resume ("Founding contributor…
built the initial engine core in the repo's first week"). Profile-v2's job
is to make the auto-maintained sections converge on that quality.

---

## 2. Papers — the outstanding 2026 work (verified 2026-07-09)

Weakness key: (a)=no abstraction D1 · (b)=fragmentation D2 · (c)=one-day
window D4 · (d)=additive-only D3.

### Surveys
| Paper | When | Takeaway for us |
|---|---|---|
| **Memory in the Age of AI Agents: A Survey** ([arXiv:2512.13564](https://arxiv.org/abs/2512.13564)) | Dec 2025, rev. Jan 2026 | Canonical field map: forms/functions/dynamics; the "dynamics" chapter covers episodic→semantic consolidation — read before committing to a design. |
| **Memory for Autonomous LLM Agents: Mechanisms, Evaluation, Frontiers** ([arXiv:2603.07670](https://arxiv.org/abs/2603.07670)) | Mar 2026 | Frames memory as a write–manage–read loop; names continual consolidation + contradiction management on the write path as the least-solved parts — exactly (a)/(d). |
| **Graph-based Agent Memory: Taxonomy, Techniques, Applications** ([arXiv:2602.05665](https://arxiv.org/abs/2602.05665)) | Feb 2026 | Best catalog of concrete techniques for (b): how systems link, dedupe, and evolve entity nodes over time. |

### Consolidation / episodic→semantic
- ⭐ **TiMem: Temporal-Hierarchical Memory Consolidation**
  ([arXiv:2601.02845](https://arxiv.org/abs/2601.02845), Jan 2026) — a
  Temporal Memory Tree: raw observations are leaves, progressively
  consolidated upward into abstract persona/summary nodes; semantic-guided
  merging across levels; recall picks the abstraction level per query. SOTA
  LoCoMo 75.3 / LongMemEval-S 76.9 at half the recalled tokens. **The
  closest published blueprint for us**: evidence bullets = leaves,
  "major contributions" = consolidated internal nodes. → (a)(b)(c)
- **Episodic-Semantic Memory for Long-Horizon Scientific Agents**
  ([arXiv:2605.17625](https://arxiv.org/abs/2605.17625), May 2026) —
  dual-store; shows consolidation quality is *the* scalability bottleneck;
  70–85% accuracy over 10k-message horizons at 62% fewer tokens. → (a)(c)
- **SCM: Sleep-Consolidated Memory with Algorithmic Forgetting**
  ([arXiv:2604.20943](https://arxiv.org/abs/2604.20943), Apr 2026) — offline
  multi-stage "sleep" cycle consolidates, re-prioritizes, forgets (~91%
  noise pruned) while the agent idles. The cleanest statement of the
  **nightly/weekly batch consolidation pass** pattern. → (c)(d)

### Rewriting safely (and why naive rewriting fails)
- ⭐ **Useful Memories Become Faulty When Continuously Updated by LLMs**
  ([arXiv:2605.12978](https://arxiv.org/abs/2605.12978), May 2026) — the
  essential cautionary result: continuously LLM-rewritten memory follows an
  inverted-U and ends *below the no-memory baseline*; even consolidating
  from ground truth loses 54% of previously-solved problems. Prescription:
  **raw episodes stay immutable; consolidation is a gated, occasional,
  provenance-tracked layer above them.** → shapes (d)
- **Capability-Preserving Evolution (Do Self-Evolving Agents Forget?)**
  ([arXiv:2605.09315](https://arxiv.org/abs/2605.09315), May 2026) — memory
  entries earn **stability scores** through repeated confirmation;
  high-evidence entries are protected from rewrite, low-evidence ones stay
  mutable. → policy for (d)
- **TOKI: Bitemporal Operator Algebra for Contradiction Resolution**
  ([arXiv:2606.06240](https://arxiv.org/abs/2606.06240), Jun 2026) —
  bitemporal dual-row schema (valid-time + transaction-time), contradicted
  facts kept as audit rows; four write-time resolution policies. The right
  storage discipline for auditable merges. → (d)

### Temporal knowledge graphs / entity resolution
- ⭐ **Zep: Temporal Knowledge Graph for Agent Memory**
  ([arXiv:2501.13956](https://arxiv.org/abs/2501.13956), Jan 2025 — still
  the canonical reference; 2026 graph-memory work benchmarks against it) —
  Graphiti extracts entities/relations per episode, **resolves entities
  against existing nodes (dedup/merge)**, stores bi-temporal edges with
  invalidation on contradiction, and builds **community nodes** summarizing
  entity clusters. Entity resolution + community summaries are the most
  direct fix for (b). → (b)(d)

### Agent-managed memory
- ⭐ **AutoMEM — Cross-Scenario Generality of Agentic Memory Systems**
  ([arXiv:2606.04315](https://arxiv.org/abs/2606.04315), Jun 2026) — gives
  the agent tool-call control over storage/merging/retrieval; beats fixed
  pipelines across scenarios. Evidence that our consolidation should be an
  **LLM curator step with merge tools**, not hard-coded rules. → (b)(d)
- **EvolveMem** ([arXiv:2605.13941](https://arxiv.org/pdf/2605.13941), May
  2026) — the agent revises its own memory *policies* online. Existence
  proof; lower priority.

### User-profiling benchmarks (our exact task)
- ⭐ **StreamProfileBench** ([arXiv:2605.25758](https://arxiv.org/abs/2605.25758),
  May 2026) — user profiling as continuous state maintenance over behavior
  streams; finds a systemic *conservative bias*: models over-retain stale
  interests and miss decay. Additive-only stores institutionalize this. → (c)(d)
- **HorizonBench** ([arXiv:2604.17283](https://arxiv.org/abs/2604.17283),
  Apr 2026) — 6-month histories with evolving preferences; best frontier
  model 52.8%; without cross-day tracking the profile freezes at the first
  observed state. → (c)(d)
- **PERMA** ([arXiv:2603.23231](https://arxiv.org/abs/2603.23231), Mar 2026)
  — systems that **link related interactions** beat pure semantic retrieval
  for persona coherence — direct evidence for consolidating correlated work. → (b)
- **Personalize-then-Store / PerMemBench**
  ([arXiv:2605.25535](https://arxiv.org/abs/2605.25535), May 2026) —
  session-level **storage gating** (skip transient events at write time)
  yields large retention gains — cheap first step toward contribution-shaped
  evidence. → (a)(b)

**Cross-cutting rule from the 2026 literature:** never rewrite the raw
evidence; keep episodes immutable and make consolidation a gated,
provenance-tracked layer above them.

## 3. Projects — what shipping systems do (verified 2026-07-09)

| Project | Memory shape | Consolidation story | Profile story | Alive? |
|---|---|---|---|---|
| **Zep + Graphiti** ([repo](https://github.com/getzep/graphiti), ~28.5k★) | bi-temporal knowledge graph; raw episodes kept as provenance | **best-in-class**: entity resolution/dedup at ingest w/ node-summary rewriting; contradicted edges stamped `invalid_at` (never deleted); `summarize_saga()` rollups (v0.29, Apr 2026); **Observations** offline pass materializing derived pattern nodes (May 2026) | per-user graph + structured context block | very active (pushed 2026-07-09) |
| **Mem0** ([repo](https://github.com/mem0ai/mem0), ~60k★) | flat NL facts + optional graph | write-time arbitration: each new fact retrieves top-k similar and tool-calls **ADD/UPDATE/DELETE/NOOP** | none (`get_profile()` closed as not-planned) | very active; $24M funded |
| **Letta (ex-MemGPT)** (~23.7k★) | core blocks + archival; 2026 pivot to **MemFS** (git-backed markdown) | **sleep-time agents**: paired background agent calls `rethink_memory` to rewrite raw context into learned context ([arXiv:2504.13171](https://arxiv.org/abs/2504.13171)) | freeform `human` block edited over time | active, pivoting |
| **memobase** ([repo](https://github.com/memodb-io/memobase), ~2.8k★) | **profile-first**: `topic → sub_topic` slots + event stream; every event carries a required **"profile delta"** naming the slots it touched | buffered flush → 3-LLM-call extraction of validated deltas; auto-discovers new sub-topics unless strict mode | strongest schema-guided induction surveyed | dormant since Jan 2026 — steal ideas, not dependency |
| **MemOS** (~10.1k★) | MemCube: parametric/activation/plaintext | MemLifecycle state machine (Generated→Activated→Merged→Archived→Expired) + MemScheduler | partial (auto-expiring preference memories) | very active; OpenClaw plugin Mar 2026 |
| **MIRIX** (~3.6k★) | six typed stores (core/episodic/semantic/procedural/resource/vault) | Meta Memory Manager routes to per-type managers; core rewrites at 90% capacity; "auto-dream" landed Jun 2026 (semantics unverified) | implicit `human` block | alive, going hosted |
| **A-MEM** (~915★) | Zettelkasten atomic notes, dynamic links | **memory evolution**: a new note triggers retroactive rewrite of *linked old notes* in light of new evidence | none | dormant (NeurIPS'25 artifact) — steal the idea |
| **supermemory** (~28.3k★) | fact graph | typed edges: **Updates** (supersede w/ `isLatest`), **Extends**, **Derives** (inferred higher-level facts) | dedicated User Profiles API (static + dynamic, auto-induced from Gmail/GitHub) | very active; seed funded |
| **Hindsight** ([repo](https://github.com/vectorize-io/hindsight), ~18.2k★, 2026 breakout) | facts/entities in 3 networks: World / Experiences / **Mental Models** | explicit **Retain / Recall / Reflect** — Reflect forms insights from existing memories | per-user isolation only | very active |
| **basic-memory** (~3.4k★) | local markdown KG | agent-driven `memory-reflect` (scheduled consolidation) + `memory-defrag` (split/merge/prune) skills | none | very active |
| **OpenClaw memory-core** | `MEMORY.md` curated layer + daily notes over SQLite hybrid index | **Dreaming**: scheduled 03:00 three-phase consolidation (ingest → theme extraction → gated promotion into MEMORY.md, promotion gates = recall frequency / score / token cap) | no profile schema | shipping (our own gateway!) |
| **Claude Code / Anthropic** | `/memories` file dir (memory tool GA) + auto-memory `MEMORY.md` index → topic files | server-side compaction (beta 2026-01); consolidation = model reorganizing index↔topic files | none | shipping |

**Convergent 2026 pattern:** a small curated human-readable top layer
(MEMORY.md / MemFS / profile slots) above an immutable searchable evidence
store, with **sleep-inspired background consolidation** (Dreaming,
sleep-time agents, Reflect, Observations) as the promotion machinery
between the layers. Our `profile.yaml` + `events.db` is already the
two-layer skeleton — what's missing is exactly the promotion/consolidation
machinery.

---

## 4. Profile-v2 design — mechanisms mapped to our code

Ordered by leverage. Research invariant respected throughout (per
arXiv:2605.12978): `events.db` evidence stays immutable; consolidation is a
**scheduled, gated, provenance-tracked** layer above it — never a rewrite
after every interaction; every consolidated claim must cite evidence
ids/URLs; the profile repo's git history is the rollback.

### P1 · Initiative resolution — fix fragmentation (D2)
*From: Zep/Graphiti entity resolution, memobase profile-delta, PERMA.*
- Add a `part_of:` field to project entries and an owner-editable alias map
  (`profile/aliases.yaml`) mapping repos/keywords → initiatives, e.g.
  `bde-private, BDE_doc, "DreamZero", "RFC #4534" → BDE (vLLM-Omni)`;
  `vllm-omni-rebase-agent, vllm-omni-copilot, personal-agent → Agent
  automation`. The updater prompt lists the initiatives and **requires every
  op to name which initiative it touches** (the memobase delta trick) — the
  join key that makes correlated events converge instead of fragment.
- New ops: `merge_projects {into, from}` (union evidence, dedupe highlights,
  keep both repo links; `from` entry becomes a stub with `merged_into:`) and
  `move_evidence {from, to, match}` (fixes the #4411-under-vllm-omni-images
  misattribution class).

### P2 · Weekly consolidation pass — fix abstraction + duplicates (D1, D3)
*From: Letta sleep-time / OpenClaw Dreaming / SCM / Zep Observations.*
- New task `tasks/profile_consolidate.py` + `assistant consolidate`,
  scheduled **weekly** (Sunday slot via the gateway cron), not daily —
  deliberate cadence per the inverted-U corruption result.
- Per section, it gets what the daily pass never sees: the entry's full
  accumulated evidence + the related slice of `events.db` + authored-PR
  history (the `enrich-profile` fetcher already exists). It rewrites the
  section: dedupes bullets, merges near-duplicates, and **promotes**
  clusters of evidence into contribution-level highlights written in resume
  voice ("Designed…", "Led…" — the manual `experience:` section is the
  style reference in the prompt).
- Promotion gate (OpenClaw Dreaming style): evidence recurring across ≥3
  days, or a terminal event (PR merged / RFC accepted / release) →
  eligible for a highlight; single-day crumbs stay evidence.
- Output = ops applied through `apply_ops` (validated, cited), one git
  commit, and a **"Profile consolidation this week" diff section in the
  digest email** — the owner audit gate.

### P3 · Stability-scored rewriting — safe mutation (D3)
*From: Capability-Preserving Evolution, TOKI, supermemory `isLatest`.*
- Each highlight/evidence gains `confirmations: n` (bumped when new
  evidence re-supports it). Consolidation may freely rewrite entries with
  low confirmations; entries confirmed ≥3 times require the new text to
  cite strictly more evidence to replace them. `education:`/`experience:`
  stay untouchable. Superseded highlights move to a `history:` list in the
  entry (TOKI audit rows), not deleted.

### P4 · Multi-day context in the daily pass — fix myopia (D4)
*Cheap, immediate.* The daily updater prompt additionally gets: the last 7
days of applied ops + the initiative list from P1. Today's crumb then
attaches to the visible arc instead of spawning a new entry.

### P5 · Write gate — stop storing noise (D1 at the source)
*From: Personalize-then-Store.* Before `add_evidence`, the op must state
which initiative/skill it advances; transient one-offs (a single page
visit, a lone comment) are dropped or logged only to events.db. Kills the
"comp3111-lab1-2021f as Java evidence" class.

### P6 · Skill re-basing (one-time, then maintained by P2)
Re-derive the skills section from initiatives, not GitHub language stats:
"LLM inference systems", "CUDA/GPU serving", "multi-agent orchestration"
with evidence links into initiative entries. One `assistant consolidate
--section skills` run seeds it; P2 maintains it.

### Evaluation
- Mechanical checks in tests: no duplicate highlights (normalized-string
  match), no evidence bullet >? appearing in two entries, RFC/PR number
  consistency within an initiative.
- Behavioral: StreamProfileBench's finding (conservative bias / stale
  retention) → assert dormancy decay still fires after consolidation.
- The real gate stays human: the weekly consolidation diff in the digest.

### Build order
| Step | What | Effort |
|---|---|---|
| 1 | P4 multi-day context + P5 write gate (prompt-only changes) | small |
| 2 | P1 aliases + `merge_projects`/`move_evidence` ops | medium |
| 3 | P2 weekly consolidate task + digest diff section + cron slot | the core |
| 4 | P3 stability scores + history rows | small, rides on 3 |
| 5 | P6 skill re-basing run | one-off |

### Reading order (for the owner)
TiMem → Zep paper → "Useful Memories Become Faulty" → CPE → SCM →
StreamProfileBench. Projects: Graphiti's dedup + Observations code, Letta
sleep-time blog, memobase profile-delta docs.
