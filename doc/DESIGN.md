# personal-agent — Design & Architecture

This document is the map for anyone who wants to understand, operate, or extend
the system. For *using* it, see the [User Guide](USER_GUIDE.md); for setup, see
the [README](../README.md).

Deep-dives on specific subsystems live in their own docs and are linked inline:
[service layer](DESIGN_SERVICE_LAYER.md), [pipeline metrics](PIPELINE_METRICS.md),
[agent-memory research](RESEARCH_AGENT_MEMORY_2026.md),
[WeChat gateway](WECHAT_OPENCLAW.md).

---

## 1. Philosophy

Three ideas shape every decision in the codebase:

1. **You are not fabricated.** The agent maintains a picture of a real person.
   Every claim it stores about you must cite an observation; anything shown
   publicly (website, résumé) is either deterministic or gated on your approval.
   The constrained-write-surface pattern below is how this is enforced
   mechanically, not just by prompt.

2. **Two-layer memory.** An immutable evidence log beneath a small, curated,
   human-readable, git-versioned profile. Summarize *over* evidence, never
   *instead of* it. This is the pattern the 2026 agent-memory literature
   converged on ([research notes](RESEARCH_AGENT_MEMORY_2026.md)), and it makes
   the profile auditable and every change revertible.

3. **Degrade, never crash.** A daily run touches a dozen flaky external services.
   Each phase catches its own failures into an error list and the run continues;
   the digest renders whatever it has. A crashed run resumes at the phase it
   stopped, not from scratch.

---

## 2. The pipeline

One daily run is a [LangGraph](https://langchain-ai.github.io/langgraph/)
`StateGraph(AssistantState)` invocation — nine phases, each a node:

```
                observations[]              profile
   ┌─────────┐  ┌─────────┐  ┌────────┐  ┌────────┐  ┌───────┐
   │ collect │→ │ profile │→ │ resume │→ │ digest │→ │ todos │→ ┐
   └─────────┘  └─────────┘  └────────┘  └────────┘  └───────┘  │
   ┌──────────┐  ┌─────────┐  ┌─────────┐  ┌────────┐           │
 ┌←│ research │← │ website │← │ deliver │← │ curate │←──────────┘
 │ └──────────┘  └─────────┘  └─────────┘  └────────┘
 └→ (reading list, digest email, published site, dormancy decay)
```

`agent/orchestrator.py` builds and runs it. Phase responsibilities:

All under `agent/` (paths below are relative to `src/assistant/agent/`):

| Phase | What it does | Key module(s) |
|---|---|---|
| **collect** | Run every registered collector (GitHub, Chrome, Gmail) → normalized `Observation`s; fetch GitHub notifications; pull website marks | `collectors/`, `marks.py` |
| **profile** | LLM emits typed patch ops against the profile; code applies them; git commit | `tasks/profile_update.py`, `profile_store.py` |
| **resume** | If profile changes are résumé-worthy, edit the LaTeX (approval-gated) | `tasks/resume.py` |
| **digest** | Triage GitHub notifications 🔴/🟡/⚪ against the profile; dedupe via seen-store | `tasks/github_digest.py` |
| **todos** | Age out stale todos; auto-close finished ones; derive new from red notifications | `tasks/todos.py`, `todo_store.py`, `urgency.py` |
| **research** | Gather arXiv + feeds, score for relevance, select, summarize; feed the reading list | `research/`, `tasks/` |
| **website** | Render the profile + todos + reading + routines to HTML; push to Pages | `website/` |
| **deliver** | Render and send the digest email; announce success to WeChat | `deliver/` |
| **curate** | Decay dormant entries; prune old chat sessions + staged chat images | `tasks/curate.py` |

### State & resume

`AssistantState` (a `TypedDict`, `agent/state.py`) is the shared bag threaded
through the graph. Resilience follows one discipline, borrowed from the parent
rebase-agent project:

- A `state.json` on disk holds the **phase to re-enter**, advanced only when a
  phase completes successfully.
- Each phase writes its output as a JSON artifact under `runs/<run_id>/`.
- `assistant run --resume` rehydrates artifacts from the interrupted run and
  restarts at the saved phase.
- A single `flock` around the whole run prevents cron, a chat-triggered run, and
  a manual run from interleaving.

### Error handling

Phases 3–9 each wrap their body in try/except and accumulate failures into
`state["errors"]` (a reducer-merged list). A failure degrades the output — a
broken collector means fewer observations, a failed research phase means no
papers — but never blocks the run. The seen-store is only updated *after* the
email actually sends, so a delivery failure doesn't silently swallow items.

---

## 3. Memory: the profile

The profile is the hub — every downstream phase reads it, only the profile phase
writes it.

### Two layers

- **Evidence (`events.db`)** — a SQLite + FTS5 append-only log of every
  observation, plus a seen-store for dedup and a metrics table. Immutable.
- **Curated (`profile/profile.yaml`)** — a small, structured, human-readable
  document in its own **git repo**. Skills, projects, interests; each entry
  carries cited evidence, timestamps, a `confirmations` count, and a `status`
  (active/dormant/merged). `PROFILE.md` is a regenerated render.

Because the curated layer is git, every daily update is a reviewable, revertible
commit and silent drift is impossible.

### Constrained writes (the safety core)

The profile is never freely rewritten by an LLM. `profile_store.apply_ops`
accepts only a fixed set of **typed patch operations** and applies them in code:

```
bump_last_seen · add_evidence · add_skill · add_interest · add_project
update_highlight · mark_dormant · merge_projects · move_evidence   (daily)
+ rewrite_entry                                                     (weekly only)
```

Invariants enforced by code, not prompt:

- **Protected sections** (`identity`, `education`, `experience`, `preferences`)
  reject every op — the owner owns those.
- **Never delete** — entries go `dormant`/`merged`/`outdated`; superseded
  highlights move to a `history` list (audit rows).
- **Stability gate** — an entry confirmed ≥3 times rejects a `rewrite_entry`
  that would cite fewer source URLs than it currently has.
- **Initiative-owning entries** can't be merged away (a fragment merges *into*
  the canonical entry, never the reverse).

### Daily vs weekly

- **Daily** (`tasks/profile_update.py`) is deliberately additive: a single
  constrained LLM call, given the profile + initiatives + last-7-days of ops +
  today's observations, emits ops. New skills start `emerging`; a write gate
  drops transient noise.
- **Weekly** (`tasks/profile_consolidate.py`) is the editorial pass: it sees a
  whole section at once and may `merge_projects`, `move_evidence`, and
  `rewrite_entry` to promote clustered evidence into résumé-voice highlights
  (following the [writing rules](../src/assistant/agent/writing.py) and the owner's
  hand-written `experience` section as the style reference). It also runs an
  **LLM judge audit** — contradictions, stale claims, unsupported highlights —
  recorded as metrics and emailed, never auto-fixed.

### Initiatives

`aliases.yaml` maps repos/keywords → initiative umbrellas. This is the join key
that stops correlated work from fragmenting into separate entries: every daily
op names the initiative it advances, and consolidation merges fragments into the
initiative's canonical entry. Owner-editable.

Background and citations: [RESEARCH_AGENT_MEMORY_2026.md](RESEARCH_AGENT_MEMORY_2026.md).

---

## 4. Collectors

A pluggable registry (`collectors/__init__.py`): each collector implements
`collect(since) -> list[Observation]` and registers itself. Adding a source
(calendar, Slack, …) is one self-contained module; the orchestrator never
changes.

```python
class Observation(TypedDict):
    source: str        # github | chrome | gmail | …
    ts: str
    kind: str          # commit | pr | review | visit | email | …
    title: str
    url: str | None
    entities: list[str]
    raw: dict
```

- **GitHub** — the richest source: authored + reviewed PRs/issues (search API,
  paginated), commit summaries, and `GET /notifications` (fetched once, used by
  both collect and digest). `enrich-profile` uses the same code for history
  backfill, adding per-repo README/description context.
- **Chrome** — reads the History SQLite (copied first, since Chrome locks it).
  **Privacy-tiered**: a denylist is dropped at read time; only an allowlist of
  domains keeps full titles/URLs; everything else is domain-level counts. Raw
  URLs outside the allowlist never enter a prompt.
- **Gmail** — IMAP, headers/snippets only, reusing the SMTP credentials. Also
  the email chat channel's inbound side.

---

## 5. Service layer & chat

The chat surface is a **typed action registry** (`actions.py`) — one table that
is the single source of truth for what the agent can *do*. It drives three
consumers: the chat LLM's prompt (which actions it may emit), the executor, and
the CLI/HTTP entry points. Handlers return one human-readable line describing
what the code actually did — replies are built from those, never from LLM claims.

Action outcomes are **reviewed, not just appended**: when an outcome reports a
failure (bad parameters, wrong id, unknown action), the model is shown exactly
what it emitted and what came back, and gets up to two repair rounds to
correct and re-execute — retried outcomes appear as "(retry) …" in the reply.
Duplicate rejections never retry: that's dedup working as intended.

Actions: todo/reading management, `trigger_run`, `run_phase`, `plan_task`,
`execute_task`, `web_search`, reminders, routines, the finance ledger
(log/void/recategorize/summary), the health log (meals/exercise/weight/
profile/needs/summary), and status/profile queries.

### Images

Attached images (a WeChat photo, an email attachment, `assistant ask
--image`) reach the agent one of two ways:

- **Natively multimodal main LLM** (`LLM_SUPPORTS_IMAGES=true`, e.g.
  qwen3.6-plus or Claude): chat attaches the image blocks directly to the
  model call (`llm.py`) — the model sees the pixels, no separate vision pass.
- **Text-only main LLM**: a describe-then-reason fallback (`vision.py`)
  writes one detailed description per image — scene plus verbatim text
  transcription — via a configured multimodal API
  (`VISION_API_KEY`/`VISION_MODEL`, Anthropic- or OpenAI-style wire format
  via `VISION_PROVIDER`), and the chat prompt carries it as an
  "## Attached images" context block. Models never run locally — image
  understanding is API-only by design.

WeChat delivery rides the gateway's `message_received` hook (which carries
the staged media path) into a short TTL cache the reply hook drains; the
daemon's `/chat` accepts both `image_paths` (local, loopback-trusted) and
base64 `images` staged into `DATA_DIR/media/` (pruned with chat history by
the curate phase).

### Finance ledger

`finance_store.py` keeps income/expense records in `finance.yaml` inside the
profile git repo — versioned like todos, local-only like everything else, and
never-delete (wrong entries are *voided*, miscategorized ones moved with
`recategorize_transaction`). Every record carries a full `YYYY-MM-DD HH:MM`
identity: the stated transaction time read off a receipt or the owner's
phrasing, else the logging clock time (`time_source: stated|auto`). Dedup
runs on two identities (stated times only — auto-filled clock times are
excluded so a forgotten-and-resent entry is still caught): the **bill
identity** kind + amount + currency + date + stated time, which rejects a
receipt image of an already-recorded payment even when the note is worded
differently; and the full signature including the note for entries without
a stated time. A same-day same-amount near-miss is logged but flagged with
a ⚠ warning naming the lookalike records.

Records enter through the typed `log_transaction` action: spoken amounts
("午饭花了45") or amounts the model reads off a payment-receipt screenshot.
All analysis numbers — monthly income/spend/net, savings rate, category
breakdown, previous-month comparison — are **computed in code**
(`summary()`), injected into the chat context as a "## Finance ledger"
block, and the LLM is instructed to cite those figures rather than invent
any. `/fin` slash commands cover quick logging, summaries, and
recategorizing.

### Health subprofile

`health_store.py` mirrors the finance pattern in `health.yaml`: a small
static body profile (sex, birth year, height), append-only `meal` /
`exercise` / `weight` records with the same stated-or-auto time identity and
dedup (one meal at 12:30 is one meal, however described), and a list of
nutrients the owner wants covered. Meals arrive as text or as photos the
multimodal LLM reads (estimating macros, transcribing label ingredients).
`summary()` computes BMI, weight trend, exercise totals, and daily
calorie/protein averages in code; the chat context carries it as a
"## Health" block so status questions are answered from real numbers.
Health data is never rendered to the website or digest.

### Cross-links

The profile, finance ledger, and health log describe one person, and
analyses are told to treat them that way. `insights.py` computes the
deterministic joins — meal↔expense pairs matched on the shared
date + stated-time identity, monthly food spend vs meals actually logged
(flagging spend-days with no meal), health-category spending vs the open
nutrient needs — and the chat context carries them as a "## Cross-links"
block; the system prompt instructs the model to weave every section (work
profile, routines, finance, health) into any single-domain analysis.

### Agentic task execution

`task_runner.py` handles requests with no built-in pipeline (the copilot
pattern): a bounded ReAct loop — one registry action per turn, real outcome
fed back with the same `looks_failed` review as chat, adapt on failure,
finish with a report. Runs detached (`assistant task` via Popen, like
trigger_run), persists every turn atomically to `DATA_DIR/tasks/<id>.json`
(collision-safe ids), writes its own trace (`<id>-trace.jsonl`) and a numeric
`task` metrics row, and delivers the report over WeChat.
`execute_task`/`plan_task`/`trigger_run`/`approve_task` are excluded from its
action set (no recursion, no surprise pipeline runs, no self-approval).

**Execution depth adapts to difficulty.** Each task is first assessed (one
cheap single-model call + deterministic keyword clamps that only raise the
tier): *simple* → no plan, a 3-turn budget, every call single-model (no MoA);
*medium* → a short persisted plan (drafted single-model), 12 turns, still no
MoA; *complex* → the plan is drafted on the configured `task` role (the one
MoA-worthy spot) and carries per-milestone status the model ticks each turn
plus a verify check the finish report must address.

**Approval is gated at action dispatch, at every tier.** The registry's
`risky` metadata (`run_phase website` publishes; `reboot`) is the boundary —
an unapproved task that reaches a risky action pauses as `awaiting_approval`
with the pending action persisted and the owner notified ("批准请回复:
批准任务 <id>"); a complex task whose assessment shows publishing intent
pauses before its first step. The owner's `approve_task` action releases it
(locked `awaiting_approval → queued → running` transitions, idempotent
double-approval, resume from persisted steps — executed steps never replay,
and terminal tasks refuse re-runs).

### Self-evolution

`lessons_store.py` is the agent's behavior-change surface: durable rules
with provenance (`owner` — stated directly in chat via the
`learn_preference` action — or `evolve` — distilled weekly by
`tasks/evolve.py` from chat sessions and task traces, friction-annotated).
Active lessons are appended to the system prompt of every chat and task
turn (`system_prompt()`), so learning changes behavior immediately;
everything is git-audited, retire-only (never deleted), capped at 25 active
(evolve-sourced rotate out first, owner rules never auto-evict), and
near-duplicates are rejected.

### Workflows

`workflow_store.py` saves owner-authored procedures (`workflows.yaml` in the
profile repo — versioned, never-delete, retire-only, best-effort git audit
with the YAML as source of truth). A workflow v1 **is** a saved text plan
(`name, description, steps ≤6, verify`) — exactly the format the task runner
executes: `run_workflow` mints a pre-planned task record (status `queued`)
and dispatches it through the resume path, so a queue retry *resumes* from
persisted steps rather than restarting, milestones/tier budgets apply
(clamped ≥ medium), and **every outward step still pauses for per-action
owner approval** — approval is one-shot (`pre_approved` + `pending_action`
are consumed at load; a second risky step pauses again). Run accounting is
exactly-once (`mark_ran` is task-id-idempotent and ordered before the
terminal persist); a retired workflow's pending tasks cancel at start.
Routines can bind a workflow first-class (`workflow: wf3`) for deterministic
scheduled dispatch — no chat-model text interpretation — and retiring the
workflow cancels its bound routines. The five workflow actions are excluded
from the task loop: workflow authoring/invocation is an owner surface. The
store fails closed on a corrupt file (preserved for recovery, mutations
refused). Explicit non-goals v1: branching/conditional logic, parameters,
cross-tenant sharing.

### Write concurrency

One user's YAML stores share one git repo and one daemon serves chat, the
pipeline, routines, and background tasks concurrently — so every mutating
store method holds the per-user write lock for its whole load→mutate→save+
commit transaction (`locks.locked_transaction`; the same reentrant
`data_dir/write.lock` the chat executor's batch lock uses, so nesting is
safe). Saves are atomic (`tmp` + `os.replace` — no torn reads), the daily
profile update re-loads and applies its ops *inside* the lock (LLM call
outside), and reminders/routines claim-before-send so a poll race can't
double-fire and a concurrent cancel can't be lost. Locks are held for
milliseconds — never across LLM or network calls.

### Proactive messaging

`notify.py` (`send_wechat`) pushes messages to the owner without an inbound
command. It backs the deliver-phase success announce, one-shot **reminders**
(`ReminderStore`), and conditional **routines** (`routines.py` — WHEN [time +
daily/workday/weekday-list, `monthly:<dom>`, or `yearly:<MM-DD>` schedules] +
optional LLM-judged CONDITION + a TASK run through the chat agent). The serve daemon's
poll loop fires due reminders and routines each cycle.

---

## 6. Digests

### GitHub triage

Deterministic pre-classification by notification `reason`
(review_requested/mention/…), then an LLM pass ranks and summarizes **relative
to the profile** ("you own this PR", "this touches the scheduler you rebased"),
bucketing into 🔴 action / 🟡 worth-knowing / ⚪ FYI. A seen-store suppresses
unchanged, already-shown threads.

### Research

Query set generated from the profile → arXiv fetch + RSS/Atom feeds → dedupe vs
seen-store → cheap-model relevance scoring → select → one full-model call writes
all summaries with a per-item "why this matters to you." Papers feed a
persistent reading list. The 中文 section has a score floor so it's never empty
when a source works, and per-source health tracking surfaces any feed dead 3
days running in the digest footer (rather than silently vanishing). An
**adaptive quota** throttles how many papers surface to ~1.5× the rate you
actually act on them.

---

## 7. Website

`website.py` renders the profile + todos + reading + routines to a static site
and pushes to a GitHub Pages repo. **Deterministic — no LLM in the loop**, so
nothing fabricated can reach a public page.

- Public pages (About/Experience/Education/Projects) render from the profile.
- Private pages (Todos/Reading/Routines) are **AES-GCM encrypted at render
  time**; the published HTML holds ciphertext, decrypted in-browser with the
  owner's password (WebCrypto, PBKDF2). Owner-only action buttons are gated by a
  localStorage flag.
- **Todos** use the [urgency metric](../src/assistant/agent/urgency.py) (a
  Taskwarrior-style polynomial over priority/due/blocking/age × staleness) for
  calendar eligibility, ordering, and expiry.
- **Marks sync** (`marks.py`): Done/Unrelated clicks act locally and push to a
  private marks repo via a repo-scoped token embedded *only inside the encrypted
  payload*; the agent collects them each run (idempotent via the seen-store).

---

## 8. Metrics

Every phase is wrapped to record duration, error count, and its headline numbers
into an `events.db` metrics table. `metrics.py` derives a 7-day health view —
step success, profile-ops acceptance rate, notification action rate (SRE
alerting-precision proxy), reading done-rate, todo flow, publish/delivery,
weekly profile-audit findings — rendered as a **Health footer** in the digest.
Everything is computable from artifacts already written plus the owner's implicit
actions; no explicit ratings. Full catalog and the research behind each metric:
[PIPELINE_METRICS.md](PIPELINE_METRICS.md).

---

## 9. Configuration

`config.py` is a Pydantic `Settings` reading `.env` (repo root, then CWD).
`config/sources.yaml` holds the research follow-list. Secrets never live in
code. `assistant init` writes `.env` interactively with live validation;
`assistant init --check` (`init_wizard.py`) is the config doctor — the same
probes run non-interactively with a ✅/⚠️/❌ report.

**Multi-model routing.** The `ANTHROPIC_*` settings are the default provider/model; `LLM_ROLES` (a JSON role→{model, base_url?, api_key?} map) routes task roles (chat, pipeline, research, task, evolve) to different models — and, since a model often lives on a different endpoint, different base URLs + keys — so e.g. chat runs on mimo-v2.5 while research runs on qwen3.6-plus at once. `LLM._resolve` maps role→(client, model) and caches one client per provider; an unset role falls back to the cheap or default model. When `LLM_MIXTURE` gives >=2 members, the listed roles run **Mixture-of-Agents** (Wang et al. 2024): every member proposes in parallel and an aggregator synthesizes one best answer (optionally over several refine layers) — trading ~2x cost/latency for quality on the offline reasoning roles. Each member and the aggregator is `{model, base_url?, api_key?}` (same shape as an `LLM_ROLES` entry), so proposers can live on different providers — e.g. MiMo + Qwen proposing into a DeepSeek aggregator — and a member reusing the default endpoint just omits `base_url`/`api_key`. The offline generation calls (research query-gen + summaries, résumé edit) carry a role so they participate; interactive paths (chat, `plan_task`, `web_search`) and pure judges stay single-model, to keep replies fast.

**Global vs personal config (multi_tenant).** The shared `.env` carries *global*
infra only — LLM keys/routing, RESEND transport, `SERVE_TOKEN`,
`DEPLOYMENT_MODE`, search keys, and the operator's own `ANNOUNCE_*` channel (the
global self-improve ping reads these via root `Settings()`). Every user's
identity/credentials (`GITHUB_*`, `SMTP_*`, `DIGEST_TO`, `WEBSITE_*`, `MARKS_*`,
`RESUME_REMOTE_URL`, …) live in `users/<uid>/config.env` and **never** inherit
from the shared `.env` (`PERSONAL_ENV_FIELDS`). A new user is provisioned by the
**invite/onboarding** flow (`onboarding.py`, §5) with an empty `config.env`
skeleton, so no credential is ever copied between users.

`LLM_REVIEW` is a third, single-spec knob — the "strongest available reasoning" slot (`{model, base_url?, api_key?}`) resolvable as the `review` role and used by the local plan reviewer (`scripts/review_plan.py`, a development-process tool the runtime never invokes); it never joins the MoA role set.

All three are **degrade-safe by construction**: `LLM_ROLES`/`LLM_MIXTURE`/`LLM_REVIEW` are parsed by a tolerant validator (`config.py`, via `NoDecode`) that falls back to `{}` on malformed JSON rather than raising — a bad optional routing config must never crash startup, since every command builds `Settings()`. A common malform is a *multi-line* value in `.env`: `dotenv` reads only its first physical line unless the whole JSON is wrapped in `'single quotes'`, so keep each on one line or quote it. In the MoA path itself (`llm._mixture`), a proposer that errors is dropped and one that errors on a transient blip is retried (retry lives on `_call`, so members retry independently); if the aggregator raises *or returns empty* (e.g. a reasoning model that spends its whole budget on hidden thinking), it falls back to a surviving proposal rather than yielding nothing. Every mixture call is observable: each member/aggregator/fallback LLM span is stage-tagged, a parent `mixture` span summarizes the call, and a numeric `moa` metrics row (members, proposals_ok, aggregator_ok, fallback_used, abandoned, duration) lands in events.db even for chat/task turns that have no tracer — so MoA overhead is measurable against the cost metrics. `complete(..., mixture=False)` is the per-call escape hatch the task runner uses to keep simple/medium work single-model.

Data lives under `DATA_DIR` (default `~/.personal-agent/`): `profile/` (git),
`events.db`, `runs/<id>/`, `state.json`, `sessions/`, plus `todos.yaml`,
`reading_list.yaml`, `reminders.yaml`, `routines.yaml`, `aliases.yaml` in the
profile repo.

---

## 10. Safety & privacy

- **Local-first.** Everything runs and stores on your machine. The only egress
  is (a) LLM API calls, (b) the digest email, (c) explicitly-configured
  sites/repos, (d) approved résumé pushes.
- **Filtered before prompting.** Chrome/Gmail data is denylist-filtered at read
  time; raw payloads live only in local SQLite.
- **Constrained writes.** The profile's only write surface is the typed op set;
  the website render is deterministic; résumé edits only surface profile facts.
- **Protected sections & approval gates.** `identity`/`education`/`experience`
  are never auto-edited; résumé pushes need `approve-resume`; private pages are
  client-side encrypted.
- **Everything reversible.** The profile is git; nothing is deleted; every run
  is a commit.
- **Least-privilege tokens.** The GitHub collector token can be read-only; the
  marks token is scoped to one repo (and `--check` warns if it isn't).

---

## 11. Extending it

All application code lives under two layers (see §12): `agent/` (this owner's
personal agent) and `platform/` (the runtime that hosts it). **`agent/` may
import `platform/`; `platform/` must never import `agent/`** — enforced by
`test/test_boundary.py`. Extension points are almost all in `agent/`:

- **A new collector** — add a module under `agent/collectors/`, implement
  `collect(since)`, decorate with `@register("name")`. Done.
- **A new chat action** — add an `Action` to the registry in
  `agent/actions/registry.py` (name, params, handler, LLM-exposed?, slash
  alias). It becomes available over chat, slash commands, and HTTP automatically.
- **A new pipeline phase** — add a node in `agent/orchestrator.py`, insert it
  into the `_PHASES` list, give it an artifact and a `metrics.EXTRACTORS` entry.
- **A new research source** — add it to `config/sources.yaml` (RSS/Atom URL,
  language). Per-source health tracking and the score floor handle the rest.
- **A new metric** — record it from the phase node; add it to `build_health`.
- **A new personal sub-store** — follow `agent/finance_store.py`/
  `agent/health_store.py`: a YAML file in the profile repo, never-delete records
  with the stated-or-auto time identity, code-computed summaries, typed chat
  actions, and a context block; wire its joins into `agent/insights.py`.
- **A new platform capability the agent needs** — declare a hook/contract in the
  relevant `platform/` module and register the agent-side implementation in
  `agent/wiring.py` (the pattern `serve`, `llm`, `admin`, `onboarding` all use),
  so the platform stays agent-free.

The codebase favors small, testable, pure functions and a large test suite
(`test/`, run with `pytest`). When you resolve a recurring operational failure,
distill it into `skills/<name>/SKILL.md` — the growing runbook library.

---

## 12. Project layout

The package is split into two layers with a one-way import rule — **`agent/`
may import `platform/`; `platform/` must never import `agent/`** — enforced by
`test/test_boundary.py`. Where the runtime needs agent behavior it declares a
contract and the agent registers an implementation, wired at a composition root
(`cli`, `agent/app.py`/`wiring.py`). `agent/` is one owner's personal agent
(the daily pipeline is per-owner, not a system concern); `platform/` hosts it.

```
src/assistant/
├── cli/                argparse entry points  (composition root — imports both)
├── init_wizard.py      `init` wizard + `--check` doctor  (composition root)
│
├── platform/           SYSTEM — runtime, hosting, tenancy (never imports agent/)
│   ├── config.py           Pydantic Settings (all .env knobs)
│   ├── llm.py              Anthropic client (retry, JSON, roles, MoA; injected metrics sink)
│   ├── serve.py            loopback HTTP daemon (injected ServeServices)
│   ├── jobs.py scheduler.py worker.py   durable job queue + in-process pool
│   ├── dispatch.py         the job-kind contract (agent supplies handlers)
│   ├── identity.py registry.py onboarding.py admin.py   multi-tenant tenancy
│   ├── notify.py search.py vision.py    shared services (WeChat, web search, vision)
│   └── locks.py timeutil.py tracing.py uidsafe.py       infra leaves
│
└── agent/              USER — one owner's personal agent (imports platform/)
    ├── orchestrator.py state.py        the 9-phase daily pipeline + resume
    ├── dispatch.py app.py observability.py wiring.py   the platform-contract impls
    ├── profile_store.py finance_store.py health_store.py insights.py
    ├── events_store.py todo_store.py lessons_store.py workflow_store.py
    ├── task_runner.py routines.py marks.py metrics.py urgency.py writing.py utils.py
    ├── actions/            the typed action registry + handlers
    ├── chat/               agent, email/wecom channels, session service
    ├── collectors/         github, chrome, gmail
    ├── deliver/            email render/send, wechat announce
    ├── research/           arxiv, feeds, ranking pipeline
    ├── website/            site render + publish
    └── tasks/              profile_update, profile_consolidate, github_digest,
                            todos, research, resume, curate, evolve, global_evolve
openclaw-plugin/        the WeChat bridge (Node)
config/sources.yaml     research follow-list
doc/                    this doc + the sub-docs
skills/                 operational runbooks
test/                   pytest suite
```
