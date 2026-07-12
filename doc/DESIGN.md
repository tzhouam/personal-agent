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

| Phase | What it does | Key module(s) |
|---|---|---|
| **collect** | Run every registered collector (GitHub, Chrome, Gmail) → normalized `Observation`s; fetch GitHub notifications; pull website marks | `collectors/`, `marks.py` |
| **profile** | LLM emits typed patch ops against the profile; code applies them; git commit | `tasks/profile_update.py`, `profile_store.py` |
| **resume** | If profile changes are résumé-worthy, edit the LaTeX (approval-gated) | `tasks/resume.py` |
| **digest** | Triage GitHub notifications 🔴/🟡/⚪ against the profile; dedupe via seen-store | `tasks/github_digest.py` |
| **todos** | Age out stale todos; auto-close finished ones; derive new from red notifications | `tasks/todos.py`, `todo_store.py`, `urgency.py` |
| **research** | Gather arXiv + feeds, score for relevance, select, summarize; feed the reading list | `research/`, `tasks/` |
| **website** | Render the profile + todos + reading + routines to HTML; push to Pages | `website.py` |
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
  (following the [writing rules](../src/assistant/writing.py) and the owner's
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

Actions: todo/reading management, `trigger_run`, `run_phase`, `plan_task`,
`web_search`, reminders, routines, status/profile queries.

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
- **Todos** use the [urgency metric](../src/assistant/urgency.py) (a
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

- **A new collector** — add a module under `collectors/`, implement
  `collect(since)`, decorate with `@register("name")`. Done.
- **A new chat action** — add an `Action` to the registry in `actions.py`
  (name, params, handler, LLM-exposed?, slash alias). It becomes available over
  chat, slash commands, and HTTP automatically.
- **A new pipeline phase** — add a node in `orchestrator.py`, insert it into the
  `_PHASES` list, give it an artifact and a `metrics.EXTRACTORS` entry.
- **A new research source** — add it to `config/sources.yaml` (RSS/Atom URL,
  language). Per-source health tracking and the score floor handle the rest.
- **A new metric** — record it from the phase node; add it to `build_health`.

The codebase favors small, testable, pure functions and a large test suite
(`test/`, run with `pytest`). When you resolve a recurring operational failure,
distill it into `skills/<name>/SKILL.md` — the growing runbook library.

---

## 12. Project layout

```
src/assistant/
├── orchestrator.py     the 9-phase graph, run loop, resume, metrics wrapper
├── state.py            AssistantState + state.json persistence
├── config.py           Pydantic Settings (all .env knobs)
├── init_wizard.py      `init` wizard + `--check` doctor
├── cli/                argparse entry points
├── llm.py              Anthropic client wrapper (retry, JSON, image blocks)
├── vision.py           image → description fallback (remote API only)
├── profile_store.py    the profile: apply_ops, git, aliases, render
├── finance_store.py    income/expense ledger (finance.yaml, dedup, summaries)
├── events_store.py     evidence log + seen-store + metrics (SQLite/FTS5)
├── todo_store.py       todos + reading list (YAML in the profile repo)
├── urgency.py          the todo urgency metric
├── metrics.py          per-phase extractors + the Health footer
├── marks.py            website marks collection
├── notify.py           proactive WeChat + reminders
├── routines.py         recurring conditional routines (weekly/monthly/yearly)
├── search.py           web search backends
├── writing.py          résumé-voice rules (shared prompt block)
├── website/            site render + publish
├── actions/            the typed action registry + handlers
├── serve.py            the loopback HTTP daemon
├── collectors/         github, chrome, gmail
├── deliver/            email render/send, wechat announce
├── research/           arxiv, feeds, ranking pipeline
├── chat/               agent, email/wecom channels, session service
└── tasks/              profile_update, profile_consolidate, github_digest,
                        todos, research, resume, curate
openclaw-plugin/        the WeChat bridge (Node)
config/sources.yaml     research follow-list
doc/                    this doc + the sub-docs
skills/                 operational runbooks
test/                   pytest suite
```
