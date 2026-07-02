# Personal Self-Assistant Agent — Design

Owner: Taichang Zhou (tzhouam)
Status: draft v0.1 (2026-07-02)

A daily-scheduled personal agent that (1) maintains a living profile of the owner,
(2) refreshes that profile from activity traces (GitHub, Chrome, email, …),
(3) keeps the Overleaf resume in sync with the profile, (4) digests GitHub
notifications, and (5) delivers a daily research/industry digest (arXiv +
company/people blogs + Chinese AI media 新智元 / 机器之心 / 量子位) by email.

It deliberately reuses the architecture that already works in
`../vllm-omni-rebase-agent/` (LangGraph StateGraph + Anthropic SDK agent loop +
SQLite/FTS5 memory + markdown skills + curator) and the strongest ideas mined
from `../reference-agents/` (Hermes prompt caching & archive-only curator,
SWE-agent deterministic history pruning, Cline tool-call loop detection,
OpenClaw pluggable collector registry).

---

## 1. Top-level architecture

One daily run = one LangGraph `StateGraph(AssistantState)` invocation, driven by
cron/systemd-timer on the local machine (must be local: Chrome history and the
logged-in browser profile live here).

```
            ┌─────────────────────────────────────────────┐
            │ Phase 1 · COLLECT  (parallel, asyncio.gather)│
            │  github · chrome · gmail · [plugins…]        │
            └──────────────────┬──────────────────────────┘
                               ▼  observations[]
            ┌─────────────────────────────────────────────┐
            │ Phase 2 · PROFILE UPDATE (LLM merge)         │
            │  observations → profile.yaml diff (git)      │
            └──────────────────┬──────────────────────────┘
                               ▼  profile
       ┌───────────────────────┼───────────────────────────┐
       ▼                       ▼                           ▼
┌──────────────┐    ┌──────────────────┐        ┌────────────────────┐
│ Phase 3a     │    │ Phase 3b         │        │ Phase 3c           │
│ RESUME SYNC  │    │ GITHUB DIGEST    │        │ RESEARCH DIGEST    │
│ (Overleaf)   │    │ (notifications)  │        │ (arXiv/blogs/中文)  │
└──────┬───────┘    └────────┬─────────┘        └─────────┬──────────┘
       └───────────────────────┼───────────────────────────┘
                               ▼
            ┌─────────────────────────────────────────────┐
            │ Phase 4 · DELIVER  (one HTML email)          │
            └──────────────────┬──────────────────────────┘
                               ▼
            ┌─────────────────────────────────────────────┐
            │ Phase 5 · CURATE  (dedup, decay, learn)      │
            └─────────────────────────────────────────────┘
```

Same resume discipline as the rebase agent: `state.json` mirror on disk, the
`phase` marker names the phase to *re-enter*, advanced only at phase
completion, so a crashed run resumes instead of restarting. Phases 3a/3b/3c
are independent — a failure in one degrades the email, never blocks the others
(errors accumulate into `errors[]`, Phase 4 renders what it has).

### AssistantState (TypedDict, mirrors RebaseState conventions)

```python
class AssistantState(TypedDict, total=False):
    run_id: str
    phase: str                      # collect|profile|tasks|deliver|done
    observations: list[Observation] # normalized activity events from collectors
    profile_diff: str               # unified diff applied to profile.yaml this run
    resume: ResumeTaskState         # status, diff, pushed|pending_approval
    github_digest: DigestSection
    research_digest: DigestSection
    errors: Annotated[list, add]
```

---

## 2. Profile store (requirement 1)

The profile is the hub — every downstream task reads it; only Phase 2 writes it.

**Storage: a git repo at `~/.personal-agent/profile/`** (private, local; optionally
pushed to a private GitHub repo for backup).

- `profile.yaml` — the structured source of truth:

```yaml
identity:   {name, emails, github: yourname, affiliations: [ExampleU], links: []}
skills:     # every entry carries evidence — no unsourced claims
  - name: "vLLM internals / LLM inference"
    level: expert           # emerging|working|expert (LLM-assessed, evidence-based)
    evidence: ["vllm-omni rebase automation", "PR vllm-omni#4709"]
    first_seen: 2025-11-02
    last_seen: 2026-07-01
    status: active           # active|dormant — decayed by curator, never deleted
interests:
  - {topic: "multi-agent orchestration", weight: 0.9, last_seen: 2026-07-01, status: active}
projects:
  - name: vllm-omni-rebase-agent
    role: owner/author
    period: {start: 2026-03, end: null}
    highlights: ["LangGraph 5-phase orchestrator", "autonomous CI debugging"]
    evidence: [commit ranges, PR links]
publications: []             # arXiv/DBLP entries, auto-discovered + confirmed
education: []                # seeded manually from current resume, rarely touched
experience: []
preferences: {digest_language: zh+en, email_time: "08:00 HKT"}
```

- `PROFILE.md` — human-readable render, regenerated each run.
- `events.db` — SQLite+FTS5 raw observation log (same pattern as
  `debug_memory_store.py`): every observation is appended with source, timestamp,
  and extracted entities. The profile is the *curated* layer; events.db is the
  *evidence* layer. Profile entries link back to event ids.

**Why git:** every daily update is a reviewable, revertible diff. The daily email
includes the profile diff, so silent drift is impossible.

**Bootstrap:** first run ingests the current resume PDF/LaTeX + GitHub profile
(repos, languages, pinned) + a short interview (the agent emails questions, owner
replies once). Education/experience are seeded manually — collectors can't infer
those reliably.

### Profile updater (Phase 2)

A single LLM call (not an open-ended agent loop) with a strict contract:

- Input: current `profile.yaml` + today's normalized observations (capped,
  SWE-agent-style deterministic truncation — no LLM summarization needed at this
  volume).
- Output: a **list of typed patch operations** (`add_evidence`, `bump_last_seen`,
  `add_skill{status: emerging}`, `add_project`, `update_highlight`,
  `mark_dormant_candidate`) — applied by code, not free-form YAML rewriting.
  This is the same insight as the rebase agent's plan-review gate: constrain the
  LLM's write surface.
- Invariants (Hermes curator, copied verbatim): **never delete** (only
  `status: dormant`), manual/pinned entries (education, experience) are never
  auto-modified, every new claim must cite ≥1 observation id.

---

## 3. Collectors (requirement 2)

Pluggable registry (OpenClaw context-engine pattern): each collector implements
`collect(since: datetime) -> list[Observation]` and registers itself; adding
Slack/WeChat/calendar later touches no orchestrator code.

```python
class Observation(TypedDict):
    source: str        # github|chrome|gmail|...
    ts: str
    kind: str          # commit|pr|review|visit|email|star|...
    title: str
    url: str | None
    entities: list[str]  # repos, people, topics (extracted)
    raw_ref: str         # pointer back into events.db, raw payload never in prompts
```

### 3.1 GitHub collector
- Auth: fine-grained PAT (read-only: repo metadata, notifications, events).
- Pulls since last run: authored commits/PRs/issues/reviews across all repos,
  starred repos, `GET /notifications` (feeds Phase 3b too — fetched once, used twice).
- Cheap and reliable; this is the MVP collector.

### 3.2 Chrome collector
- Reads `~/.config/google-chrome/Default/History` (SQLite). The file is locked
  while Chrome runs → **copy to scratchpad, query the copy** (`urls`,
  `visits` tables; Chrome epoch = µs since 1601-01-01).
- Extracts last-24h visits, aggregates by domain, keeps titles for an
  **allowlisted domain set** (arxiv.org, github.com, docs sites, HF, scholar…)
  and only domain-level counts for everything else.
- **Privacy rule: denylist > allowlist > domain-count-only.** Raw URLs outside
  the allowlist never enter an LLM prompt. Denylist (banking, health, personal)
  is dropped at read time, never written to events.db.

### 3.3 Gmail collector
- Gmail API, OAuth with `gmail.readonly` scope (token cached locally; one-time
  browser consent).
- Reads last-24h headers + snippets; a cheap-model classifier tags
  {academic, github-notice, industry-newsletter, personal, other}. Full bodies
  are fetched **only** for classes that feed the profile/digest (academic,
  newsletters). Personal mail contributes at most counts.
- Also the **feedback channel**: replies to the daily digest addressed to the
  agent (e.g. "more RL papers, less agents") are parsed into preference updates.

### 3.4 Future plugins (same interface, not in MVP)
Shell/Claude-Code history, Zotero/Scholar library, calendar, WeChat readouts,
Slack. Each is a self-contained module under `collectors/`.

---

## 4. Resume sync — Overleaf (requirement 3)

**Hard constraint: Overleaf has no public API.** "With my Google account" means
the Overleaf login is Google OAuth — that only matters for browser automation.
Three viable integration paths:

| Path | Needs | Reliability |
|---|---|---|
| **A. Overleaf Git bridge** (recommended) | Overleaf premium; per-project git URL + Overleaf auth token | High — plain git |
| B. Overleaf ↔ GitHub Sync | Premium; resume repo on GitHub | High, but sync is manual-click or needs A anyway |
| C. Playwright browser automation with the logged-in Chrome profile | Nothing paid | Brittle; breaks on UI changes; last resort |

**Recommended design (path A):**

1. Canonical resume lives in a local git repo `~/.personal-agent/resume/`
   (LaTeX), with the Overleaf project's git URL as a remote.
2. When Phase 2 produced a profile diff that *matters for the resume* (new
   project milestone, publication, skill promotion — a small LLM relevance
   check), a resume-editor agent (the shared `_run_agent_loop` engine, tools:
   `read_file`/`edit_file`/`run_shell` for `latexmk`) edits the LaTeX.
   Constraints in prompt: never fabricate, only surface facts present in
   `profile.yaml` with evidence, preserve document style, **must compile**
   (`latexmk -pdf` is the verification gate — a resume edit that doesn't
   compile is a failed task).
3. **Approval gate — the resume is outward-facing, so it is never auto-pushed.**
   The daily email includes the LaTeX diff + rendered PDF attachment; pushing
   to Overleaf happens on explicit approval: `assistant approve resume` (CLI),
   or an approval reply parsed by the Gmail collector next run. Pre-push, the
   agent pulls Overleaf's remote first (owner may have edited in the web UI)
   and rebases; conflicts → surfaced in email, never force-pushed.
   (Same philosophy as the rebase agent's "never push to main" rule.)

Decision needed from owner: is the Overleaf account premium? If not: path B via
a free GitHub-synced template workflow, or path C.

---

## 5. GitHub notification digest (requirement 4)

- Input: `GET /notifications` (all since last run) + the day's activity
  observations for context.
- Deterministic pre-classification (no LLM): reason field →
  {review_requested, mention, assign, ci_activity, author, subscribed}.
- LLM pass ranks and summarizes **relative to the profile** ("you own this PR",
  "this touches vllm-omni scheduler which you rebased last week"), producing:
  - 🔴 **Action needed** — review requests, mentions, CI red on own PRs
  - 🟡 **Worth knowing** — activity on subscribed threads, releases of tracked deps
  - ⚪ **FYI counts** — everything else, one line per repo
- Each item: one-sentence summary + suggested action + link. Threads already
  seen and unchanged are suppressed (seen-store, §7).
- Ships inside the daily email. Optional later: a separate immediate email when
  a `review_requested` arrives (would need a second, lighter cron).

---

## 6. Research & industry digest (requirement 5)

### 6.1 arXiv
- Query set is **generated from the profile** each run: interests + project
  topics → arXiv categories (cs.CL, cs.LG, cs.DC, cs.MA…) + keyword queries
  via the arXiv API (last 1–2 days window).
- Pipeline: fetch (~100–300 abstracts) → dedupe vs seen-store → cheap-model
  relevance scoring against a rendered profile summary (batch, haiku-class)
  → top ~10 get a full-model read: 3-sentence summary + explicit
  "why this matters to *you*" line tied to a profile interest/project.

### 6.2 Company / people blogs & social media (English)
- **RSS-first**: Anthropic, OpenAI, DeepMind, Meta AI, HF blog, vLLM blog,
  lmsys, key personal blogs (Karpathy, Lilian Weng, …) — plain RSS/Atom fetch.
- Hacker News front page + `lobste.rs`, filtered by profile relevance.
- X/Twitter has no viable free API — **out of MVP scope**; revisit via a paid
  aggregator or Nitter instance if the owner wants it. (Silent-cap rule: the
  digest footer states which sources were configured vs actually fetched.)
- The follow list (`sources.yaml`: feeds, people, companies) is owner-editable
  and also grows by suggestion: the curator proposes additions when an entity
  keeps appearing in high-ranked items.

### 6.3 Chinese AI media — 新智元, 机器之心, 量子位
(assuming 新智源/机械之心 meant 新智元 and 机器之心)
- These publish primarily on WeChat 公众号 — no official API. Strategy, in order:
  1. **机器之心**: jiqizhixin.com is a real website — direct scrape/RSS.
  2. **新智元 / 量子位**: self-hosted **RSSHub** instance with its WeChat routes;
     flaky by nature, so wrapped in per-source health tracking — a source that
     fails 3 consecutive days is flagged in the digest footer instead of
     silently vanishing.
  3. Fallback: aggregator sites that mirror 公众号 content.
- Same relevance pipeline as 6.2; summaries written in Chinese (per
  `preferences.digest_language`).

### 6.4 Digest email (Phase 4)
One daily HTML email, sections: **Action needed (GitHub)** → **Papers** →
**Industry** → **中文媒体** → **Profile changes today** (the yaml diff) →
**Resume pending approval** (if any) → footer (source health, items scanned/
selected counts). Delivery via Gmail API send (same OAuth app) — reusing the
SMTP config pattern from the rebase agent as fallback.

---

## 7. Memory, dedup, and learning (Phase 5)

- **seen.db** (SQLite): every surfaced item's normalized id (arXiv id, notif id,
  URL hash) + shown date — the dedup backbone for all digests.
- **Feedback loop**: digest replies parsed by the Gmail collector become
  preference observations ("less X, more Y" → interest weight nudges;
  clicked/starred papers later, if we add tracking links).
- **Curator** (post-run, Hermes invariants): decays interests/skills not
  evidenced in N=30 days to `dormant`, merges near-duplicate interests,
  proposes new sources for `sources.yaml`, archives — never deletes.
- **Skills dir** (`skills/*/SKILL.md`, same format as the rebase agent):
  operational runbooks the agent writes for itself after resolving failures
  (e.g. "gmail token refresh dance", "RSSHub WeChat route workaround",
  "overleaf git 409 conflict recovery").

---

## 8. Engine & implementation notes

- **Language/stack**: Python 3.12, LangGraph, Anthropic SDK — direct code reuse
  from `vllm-omni-rebase-agent` (`_run_agent_loop`, dispatcher pattern,
  `persist_state_fields`, FTS5 store, skills store, curator skeleton).
- **Improvements over the rebase agent** (the deferred Hermes items, worth doing
  here from day 1 since this runs daily forever):
  - **Prompt caching** (`system_and_3`): cache breakpoints on system prompt +
    last 3 messages in the agent-loop calls.
  - **Loop detection** (Cline): hash tool-call signatures, abort after 3
    identical consecutive calls.
  - Deterministic observation truncation (SWE-agent `LastNObservations`) instead
    of max_turns hard-stops.
- **Model tiering** (mirrors the rebase agent's L1–L4): haiku-class for
  classification/relevance scoring (hundreds of items/day), sonnet-class for
  digest writing and profile patching, opus-class only for resume editing.
  Estimated cost: the cheap tier dominates volume; expect low single-digit
  $/day.
- **Scheduling**: systemd timer (better logging/retry than cron), 07:00 HKT
  daily; `--resume` on failure retry at 07:30. Manual: `assistant run`,
  `assistant run --only research`, `assistant approve resume`.
- **Config**: Pydantic `Settings` + `.env` (ANTHROPIC_API_KEY, GITHUB_TOKEN,
  Gmail OAuth client, Overleaf git token, SMTP fallback) + `sources.yaml`.

### Security & privacy
- Everything runs and stays local; the only egress is (a) LLM API calls,
  (b) the digest email, (c) approved resume pushes.
- Chrome/Gmail data is filtered *before* prompt assembly (denylist at read
  time); raw payloads live only in local SQLite.
- Tokens: fine-grained read-only PAT, `gmail.readonly` + `gmail.send` scopes
  only, Overleaf token scoped to the resume project.
- The resume approval gate is the only human gate — everything else is
  read-only or reversible (git-versioned profile).

---

## 9. Build order

| Milestone | Delivers | Requirements covered |
|---|---|---|
| **M1** | profile store + GitHub collector + notification digest + email delivery + systemd timer | 1, 4, 2(partial) |
| **M2** | Chrome + Gmail collectors, LLM profile updater with patch ops, profile diff in email | 2 |
| **M3** | arXiv + RSS blogs + 机器之心/新智元 (RSSHub) digest, seen-store, feedback parsing | 5 |
| **M4** | Overleaf resume sync with approval gate | 3 |
| **M5** | curator, skills, prompt caching polish, source suggestions | quality |

M1 is deliberately the GitHub slice end-to-end (collector → profile → digest →
email → schedule): it exercises every architectural seam with the most reliable
data source before the flaky ones (WeChat scraping, browser history locks,
OAuth dances) enter the picture.

## 10. Open decisions for the owner

1. **Overleaf**: premium (→ git bridge, path A) or not (→ path B/C)?
2. **Resume pushes**: keep the approval gate (recommended) or fully automatic?
3. **Digest language**: 中文, English, or mixed (default: mixed — Chinese for
   中文媒体 section, English elsewhere)?
4. **Where it runs**: this H200 dev box, or a personal always-on machine?
   (Chrome history must be read on the machine where you browse.)
5. X/Twitter monitoring: skip (default), Nitter, or paid API?
