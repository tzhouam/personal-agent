# personal-agent

A daily self-assistant that keeps a living picture of *you* and turns your own
activity into something useful every morning. It reads what you did (GitHub,
email, browser history), maintains an evidence-backed profile of your skills
and projects, publishes a personal website and résumé from it, and emails you
a triaged digest — action-needed notifications, new papers worth reading, and
industry news. Chat with it over WeChat or email: ask questions, send photos
and receipts, track your money and health, or hand it multi-step tasks it
executes on its own and reports back.

Everything runs locally and stores your data on your own machine; the only
things that leave are LLM API calls, the digest email, and the sites/repos you
explicitly point it at.

## 🎬 90-second introduction

https://github.com/user-attachments/assets/6228e3fc-8e1c-4edc-a2d2-68330df2823e

- **New here?** → [`assistant init`](#quickstart) walks you through setup.
- **Using it day to day?** → [**User Guide**](doc/USER_GUIDE.md)
- **Understanding or extending it?** → [**Design & Architecture**](doc/DESIGN.md)

---

## What it does

| | |
|---|---|
| 🧠 **Living profile** | Builds and maintains `profile.yaml` — skills, projects, interests, every claim backed by a cited observation — from your daily activity. A weekly editorial pass merges fragmented work into résumé-grade contribution highlights. |
| 📥 **Activity collectors** | Pluggable: GitHub (authored + reviewed PRs/issues, commits, notifications), Chrome history (privacy-tiered), Gmail (headers only). Adding a source is one module. |
| 📰 **Daily digest email** | GitHub notifications triaged 🔴/🟡/⚪ against your profile, new arXiv papers + industry/中文 news ranked by relevance, your open todos and reading list, and a 7-day health footer. |
| 🌐 **Personal website** | Renders your profile to a GitHub Pages site (about, experience, projects) plus private, password-encrypted todos / reading / routines pages. Deterministic — no LLM can fabricate a public page. |
| 📄 **Résumé sync** | Edits your LaTeX résumé from the profile (Overleaf git-bridge or any git remote), gated on a compile and your explicit approval — never auto-pushed. |
| 💬 **Chat + tasks** | Message it from email or WeChat: ask questions, manage todos/reading, run pipeline phases on demand, set reminders and recurring routines, search the web, or hand it a novel multi-step task — it executes doable ones itself in the background (search → act → adapt → report back), and plans/tracks the ones that need you. |
| 💰 **Finance ledger** | Tell it what you spent or earned (or send a payment-receipt screenshot) — records land in a git-versioned `finance.yaml` in your profile repo, never leaving the machine. Ask "这个月收支健康吗" and it analyzes real computed totals: savings rate, category breakdown, month-over-month. |
| 🖼️ **Image understanding** | Send a photo or screenshot (WeChat, email attachment, `assistant ask --image`). With a multimodal main model it sees the image directly; text-only models get a describe-then-reason fallback via a vision model API. |
| 🩺 **Health tracking** | Tell it what you ate, your workout, or your weight — or send a photo of a meal / nutrition label / body scale — and it maintains a health subprofile (`health.yaml`, local-only): body facts, meals with estimated macros, exercise, and nutrients you want covered. Ask "我最近健康状况怎么样" and it analyzes computed BMI, weight trend, exercise minutes, and calorie/protein averages. |
| 🧬 **Self-evolving** | Give it durable feedback ("以后记账默认用港币", "别再推荐X类文章") and it stores the rule — git-versioned, retire-able — and follows it from the very next message. A weekly pass also studies its own chat/task history for friction (failed actions, corrections) and distills new behavior rules itself. |
| 📊 **Self-measuring** | Per-step metrics (success, latency, acceptance rates, triage precision, reading done-rate) in a local SQLite table, surfaced in the digest and used to auto-tune how much it surfaces. |

## How it works, in one breath

One daily run is a 9-phase [LangGraph](https://langchain-ai.github.io/langgraph/)
pipeline with crash-resume:

```
collect → profile → resume → digest → todos → research → website → deliver → curate
```

Each phase reads and writes a shared state, persists per-run artifacts, and can
be re-entered if a run crashes. The profile is a two-layer memory: an immutable
evidence log (`events.db`) beneath a small curated, git-versioned `profile.yaml`
— the same pattern the 2026 agent-memory literature converged on
([research notes](doc/RESEARCH_AGENT_MEMORY_2026.md)). Full architecture in the
[Design doc](doc/DESIGN.md).

---

## Requirements

- **Python 3.11+**
- An **Anthropic-compatible LLM API key** (real Anthropic, or any compatible
  endpoint such as DeepSeek or Alibaba Model Studio — set a base URL). A
  multimodal model (Claude, Qwen-VL class) also unlocks image chat
- A **GitHub token** (fine-grained, read-only is enough for the collector)
- An **email delivery path**: a [Resend](https://resend.com) API key (easiest)
  or SMTP credentials (a Gmail app password also unlocks the Gmail collector
  and the email chat channel)
- *Optional*: a GitHub Pages repo (website), an Overleaf premium account
  (résumé git bridge), an OpenClaw gateway (WeChat), web-search API keys.

## Quickstart

```bash
pip install -e .            # from the repo root
assistant init             # guided setup — see below
assistant run --dry-run    # full pipeline, digest written to disk, no email
assistant run              # the real thing: collect → … → email → curate
```

`assistant init` is an interactive wizard that walks every configuration group
(LLM, GitHub, email, website, résumé, web search), writes your `.env` as you
go, and **validates each section live** — it pings the LLM, checks the GitHub
token identity, confirms repo push access, and warns if a token is
over-scoped. It finishes by seeding your profile from GitHub and printing the
remaining steps.

Prefer editing by hand? `cp .env.template .env`, fill it in (every knob is
documented inline), then run **`assistant init --check`** — the no-prompt
config doctor — to verify. Run `--check` any time something feels off; it
reports ✅/⚠️/❌ across every integration.

```
$ assistant init --check
personal-agent config check
──────────────────────────────────────────────
✅ LLM                  model claude-sonnet-4-6 answers
✅ GitHub               authenticated as your-username
✅ Email                Resend configured → you@example.com
✅ Website              push access ok
◌  Résumé sync          RESUME_REMOTE_URL unset — disabled
⚠️  Web search           no search key — falls back to DuckDuckGo Lite
──────────────────────────────────────────────
all required config healthy 🎉
```

## Scheduling

The agent is meant to run once a day (e.g. early morning). Point any scheduler
at `assistant run || assistant run --resume`:

- **cron**: `0 7 * * *  cd /path/to/personal-agent && assistant run || assistant run --resume`
- **systemd timer**: templates in [`systemd/`](systemd/)
- **OpenClaw gateway** (required for the WeChat channel — see
  [doc/WECHAT_OPENCLAW.md](doc/WECHAT_OPENCLAW.md))

See the [User Guide → Scheduling](doc/USER_GUIDE.md#scheduling) for the exact
setup and the timezone caveat.

## Command reference

| Command | What it does |
|---|---|
| `assistant init [--check]` | Guided setup wizard, or config doctor |
| `assistant run [--dry-run] [--resume]` | Execute a daily run |
| `assistant run-phase <phase>` | Run one phase standalone (`research`/`website`/`todos`/`resume`/`curate`/`consolidate`) |
| `assistant bootstrap` | Seed `profile.yaml` from GitHub (first run) |
| `assistant enrich-profile --since YYYY-MM` | Backfill the profile from GitHub history |
| `assistant consolidate [--dry-run] [--section …]` | Weekly editorial profile pass |
| `assistant show-profile` | Print a profile summary |
| `assistant todo list\|add\|done` · `assistant reading list\|done\|unrelated` | Manage todos / reading list |
| `assistant ask "…" [--image photo.png]` | Ask the chat agent one question locally (images welcome) |
| `assistant task "…" [--no-notify]` | Agentically execute a multi-step task now (step trace under `~/.personal-agent/tasks/`) |
| `assistant serve` | Local HTTP daemon (chat/actions API for the WeChat bridge) |
| `assistant send-test-email` | Verify email delivery |
| `assistant resume-init\|resume-status\|approve-resume` | Résumé sync + approval gate |

## Acknowledgements

The always-on runtime is built on **[OpenClaw](https://github.com/openclaw/openclaw)**:
its gateway hosts the WeChat channel (via Tencent's official
`@tencent-weixin/openclaw-weixin` plugin), schedules the daily run through
command-cron, and supervises the chat daemon — this repo's
[`openclaw-plugin/`](openclaw-plugin/) bridge routes every owner message from
OpenClaw into the agent. The pipeline itself runs on
[LangGraph](https://langchain-ai.github.io/langgraph/), and several
architectural ideas (typed hooks, plugin services) were informed by studying
OpenClaw's source. Thanks to both projects.

## Where your data lives

Everything is under `~/.personal-agent/` (override with `DATA_DIR`):

```
~/.personal-agent/
├── profile/          git repo: profile.yaml (source of truth) + PROFILE.md render
│   ├── aliases.yaml    your initiative groupings (owner-editable)
│   ├── todos.yaml  reading_list.yaml  finance.yaml  health.yaml
│   └── …
├── events.db         SQLite: raw observation log, seen-store, metrics
├── runs/<run_id>/    per-run artifacts (for --resume and audit)
├── state.json        phase marker (which phase to re-enter)
└── sessions/         chat session memory
```

The profile is a git repo, so every daily change is a reviewable, revertible
commit: `git -C ~/.personal-agent/profile log -p`.

## Safety model (the short version)

- **You are never fabricated.** Every profile claim cites an observation; the
  website render is deterministic (no LLM output reaches a public page); résumé
  edits only surface facts already in the profile.
- **Protected sections.** The agent never edits `education`/`experience`/
  `identity` in your profile — you own those.
- **Human gates on outward actions.** Résumé pushes need explicit approval;
  private website pages are client-side encrypted with your password.
- **Everything reversible.** The profile is git-versioned; nothing is deleted
  (stale entries go dormant/outdated), and every run is one commit.

Full detail in [Design → Safety & privacy](doc/DESIGN.md#safety--privacy).

## Documentation

- [**User Guide**](doc/USER_GUIDE.md) — setup and everyday use, per integration
- [**Design & Architecture**](doc/DESIGN.md) — how the system is built, extension points
- [WeChat via OpenClaw](doc/WECHAT_OPENCLAW.md) — the chat gateway, setup & runbook
- [Pipeline metrics](doc/PIPELINE_METRICS.md) — what's measured, per step
- [Agent-memory research](doc/RESEARCH_AGENT_MEMORY_2026.md) — the thinking behind the profile design
- [`skills/`](skills/) — operational runbooks the agent has accumulated for recurring failures

## License

[MIT](LICENSE) — use, modify, and distribute freely; no warranty.
