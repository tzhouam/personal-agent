# personal-agent — User Guide

Everything you need to set the agent up and use it day to day. If you just want
to get running, do the [Install](#1-install) and [Setup wizard](#2-setup) and
skip ahead — every integration below is optional and can be added later.

**Contents**
1. [Install](#1-install)
2. [Setup](#2-setup)
3. [Configure each integration](#3-integrations)
   · [LLM](#31-llm) · [GitHub](#32-github) · [Email](#33-email)
   · [Website](#34-website) · [Résumé](#35-résumé) · [WeChat](#36-wechat)
   · [Web search](#37-web-search)
4. [Your first run](#4-first-run)
5. [The daily digest](#5-the-daily-digest)
6. [Chatting with the agent](#6-chat)
7. [Your website](#7-website)
8. [Managing your profile](#8-profile)
9. [Scheduling](#9-scheduling)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. Install

```bash
git clone <this-repo> personal-agent && cd personal-agent
python -m venv .venv && source .venv/bin/activate    # or your preferred env
pip install -e .
assistant --help
```

Requires **Python 3.11+**. That's the whole dependency footprint — a handful of
pure-Python packages (anthropic, httpx, langgraph, pydantic, pyyaml, tenacity).

---

## 2. Setup

Run the wizard:

```bash
assistant init
```

It walks each configuration group, explains what each value is for and where to
get it, and **validates as you go** — a wrong LLM key or GitHub token fails
immediately, not at 7 a.m. tomorrow. Press Enter to keep a shown value, type a
new value to change it, or `-` to clear one. Secrets are masked in the prompt.

At the end it offers to seed your profile from GitHub and prints your remaining
steps.

**Doing it by hand instead:** `cp .env.template .env`, edit (every setting is
documented inline), then verify:

```bash
assistant init --check      # ✅/⚠️/❌ report across every integration
```

Run `--check` any time — after rotating a key, when a run misbehaves, or just to
confirm health. ✅ = working, ⚠️ = works but worth improving (e.g. a fallback in
use, an over-scoped token), ❌ = blocking, ◌ = optional and disabled.

Only three things are truly required: an **LLM key**, a **GitHub token**, and an
**email delivery path**. Everything else is optional.

---

## 3. Integrations

### 3.1 LLM

The agent's brain. It uses the Anthropic SDK, so any Anthropic-compatible
endpoint works.

| Setting | Notes |
|---|---|
| `ANTHROPIC_API_KEY` | Your key. Required. |
| `ANTHROPIC_BASE_URL` | Leave empty for real Anthropic. For a compatible provider (e.g. DeepSeek) set its base URL, e.g. `https://api.deepseek.com/anthropic`. |
| `ANTHROPIC_MODEL` | Main model for reasoning-heavy work (profile, digests, planning). |
| `LLM_SUPPORTS_IMAGES` | Set `true` when the model is natively multimodal (Claude, `qwen3.6-plus` via DashScope's Anthropic proxy `https://dashscope.aliyuncs.com/apps/anthropic`, …) — chat then attaches photos directly to the model. Text-only models fall back to the `VISION_*` describe chain (see `.env.template`). |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL` | Optional cheaper model for bulk relevance scoring (hundreds of items/day). Falls back to the main model if unset. |

**Cost:** the cheap model dominates volume (scoring papers/notifications); the
main model handles the ~dozen reasoning calls per run. Expect low single-digit
dollars per day on mid-tier models, less with a cheap-tier scorer set.

### 3.2 GitHub

Powers the activity collector (your commits, PRs, reviews, notifications) and,
if you enable it, website/marks pushes.

1. GitHub → Settings → Developer settings → **Fine-grained personal access
   tokens** → Generate.
2. Read access to your repositories; add **Contents: read/write** only if your
   website repo is private or you use marks sync.
3. `GITHUB_TOKEN=<token>`, `GITHUB_USER=<your-username>`.

Read-only is enough for the core digest. The `--check` doctor confirms the token
authenticates as the right user.

### 3.3 Email

How the daily digest reaches you, and (via SMTP) how the email chat channel and
Gmail collector work.

**Option A — Resend (easiest):** free API key at [resend.com](https://resend.com).

```
RESEND_API_KEY=re_...
RESEND_FROM=onboarding@resend.dev   # or your verified sender
DIGEST_TO=you@example.com
```

**Option B — SMTP (also unlocks Gmail collector + email chat):** for Gmail,
create an [app password](https://myaccount.google.com/apppasswords) (needs 2FA).

```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@gmail.com
SMTP_PASSWORD=<app-password>
DIGEST_TO=you@gmail.com
```

Setting both is ideal: Resend delivers, SMTP powers the Gmail collector (reads
your last-24h mail headers to enrich the profile/digest) and the email chat
channel. Verify with `assistant send-test-email`.

### 3.4 Website

Renders your profile to a GitHub Pages site.

1. Create a Pages repo — typically `<username>.github.io`.
2. Ensure your `GITHUB_TOKEN` can push to it (Contents: read/write if private).
3. `WEBSITE_REPO=<username>/<username>.github.io`

**Private pages password.** The todos / reading / routines pages contain
personal data, so they're **encrypted client-side** — the published HTML holds
only ciphertext, decrypted in your browser with a password (AES-GCM, PBKDF2).

```
WEBSITE_PASSWORD=<a strong password>
```

Without it, those pages render in the clear (fine if the repo is private and
Pages is disabled for them, risky on a public site — `--check` warns you). To
view a private page, open it and enter the password once; it's remembered in
that browser.

**Marks sync (optional but recommended).** The Done / Unrelated buttons on the
site act instantly in your browser, and — if configured — also reach the agent
so they feed the metrics and tuning. Because a static page can't call your
machine, the browser pushes marks to a small private repo the agent reads each
run:

1. Create a **private** repo, e.g. `<username>/agent-marks`.
2. Create a **fine-grained PAT scoped to only that repo**, Contents:
   read/write, nothing else. (This token ships to the browser inside the
   encrypted page, so scope it minimally — `--check` warns if it can see other
   repos.)
3. `MARKS_REPO=<username>/agent-marks` and `MARKS_PUSH_TOKEN=<that token>`.
   Requires `WEBSITE_PASSWORD` (the token only ships encrypted).

Without marks sync, the buttons still work locally — marks just stay in your
browser and don't reach the agent.

### 3.5 Résumé

Keeps your LaTeX résumé in sync with the profile. **Requires an Overleaf premium
account** (the git bridge is a paid feature) or any git remote holding your
`.tex`.

1. Overleaf → your project → Menu → Git → generate a token.
2. `RESUME_REMOTE_URL=https://git:<token>@git.overleaf.com/<project-id>`
3. `assistant resume-init` — clones the project locally.

Thereafter the daily run proposes edits grounded in your profile, gates them on
a LaTeX compile (if a toolchain is installed), and commits **locally only**. The
diff appears in your digest; nothing reaches Overleaf until you run
`assistant approve-resume` (which pulls and rebases any web edits first, and
never force-pushes). `assistant resume-status` shows a pending update.

> If your Overleaf account isn't premium, the bridge returns "no git access."
> Keep the `.tex` in a private GitHub repo as the git remote instead, or skip
> résumé sync entirely.

### 3.6 WeChat

Chat with the agent from your phone. Two paths:

- **OpenClaw gateway (recommended):** runs Tencent's official WeChat plugin plus
  this repo's bridge; the agent answers from your own account. Full setup,
  restart runbook, and troubleshooting in
  [WECHAT_OPENCLAW.md](WECHAT_OPENCLAW.md). This is also the runtime that
  schedules the daily run and delivers proactive messages (reminders, routines).
- **WeCom (企业微信, no extra infra for sending):** register a free WeCom org +
  self-built app, enable its WeChat plugin. Set `WECOM_CORP_ID/SECRET/AGENT_ID/
  OWNER_USERID` to push messages; receiving your replies additionally needs the
  app's callback URL publicly routed to your machine (`WECOM_TOKEN`,
  `WECOM_AES_KEY`, port `WECOM_CALLBACK_PORT`).

The email channel (below) needs none of this and works out of the box.

### 3.7 Web search

Backs the `/search` chat command and enriches task planning with real results.
**Works with no key** (DuckDuckGo Lite), but a keyed backend is more reliable.
Preferred order, best first:

| Backend | Setting | Why |
|---|---|---|
| **Gemini grounding** (recommended) | `GEMINI_API_KEY` | One key, searches + answers in one call, free ~1500/day. [AI Studio](https://aistudio.google.com) → Get API key. |
| Tavily | `TAVILY_API_KEY` | LLM-oriented, free 1,000/mo, no card. |
| Brave Search | `BRAVE_API_KEY` | Free ~1–2k/mo, no card. |
| Google Programmable Search | `GOOGLE_API_KEY` + `GOOGLE_CSE_ID` | Legacy; only useful with a pre-existing whole-web engine. |
| DuckDuckGo Lite | *(none)* | Keyless fallback, rate-limited. |

---

## 4. First run

```bash
assistant bootstrap        # seed profile.yaml from your GitHub account (once)
assistant run --dry-run    # full pipeline; digest written to disk, no email sent
```

The dry run tells you where the digest HTML landed — open it to see what your
real digest will look like. When happy:

```bash
assistant run              # the real thing
```

**Backfill (recommended).** Bootstrap only seeds from your current repos. To
build a rich profile from your history:

```bash
assistant enrich-profile --since 2025-01     # sweep authored + reviewed PRs,
                                             # commits, and repo context since a date
```

This runs the profile updater over your history chronologically, then does an
editorial consolidation pass. Re-runnable and safe (deduplicated), so you can
widen the window later.

---

## 5. The daily digest

One HTML email, top to bottom:

- **✅ Todos** — your open action items, most urgent first, with a calendar of
  the important ones. Auto-derived from red notifications and pending approvals,
  plus anything you added.
- **🔴 Action needed / 🟡 Worth knowing / ⚪ FYI** — GitHub notifications triaged
  against your profile (review requests, mentions, CI on your PRs rank highest).
- **📚 Papers & industry** — new arXiv papers and blog/news items scored for
  relevance to *you*, each with a one-line “why this matters.” The 中文媒体
  section covers Chinese AI media.
- **📄 Résumé pending approval** — if the run proposed a résumé edit, its diff.
- **📋 Profile changes today** — the git diff of what the agent learned about
  you.
- **📈 Health (7 days)** — how the agent itself is doing: run success, profile
  ops acceptance, notification action rate, reading done-rate, publish/delivery.
  The numbers tell you where it's weak.

**Feedback is implicit.** You don't rate anything — the agent learns from what
you *do*: marking a todo done, marking a paper read (relevant) or unrelated
(negative signal), acting on a red notification. These drive the metrics and
auto-tune how much it surfaces (e.g. if you never read the papers, it surfaces
fewer until you start).

---

## 6. Chat

Message the agent and it answers, executes typed actions, or plans work. Only
you can reach it — every channel authenticates the sender.

### Channels

- **Email (works out of the box):** mail your digest address from one of your
  own addresses with a subject starting `agent`, e.g.
  *"agent: add a todo to review the API PR, due Friday"*. The reply comes back
  by email. Non-owner senders and other subjects are ignored.
- **WeChat / WeCom:** see [§3.6](#36-wechat). Same capabilities, on your phone.
- **Local:** `assistant ask "what's due this week?"` for a one-off from the
  terminal.

### What you can ask

Just talk to it — "what should I focus on today?", "add a reminder to follow up
with Sam in 2 hours", "book a dinner for six on Friday", "run the research
digest again". It figures out the action. **Photos work too**: send a
screenshot or a payment receipt (WeChat or email attachment) and it responds
to what the image shows — and offers to log receipts to the ledger. On WeChat you can also use explicit
slash commands (no LLM call, instant):

| Command | Does |
|---|---|
| `/todo`, `/todo add <title> [due:YYYY-MM-DD]`, `/todo done <id>` | Manage todos |
| `/read`, `/read done <id>`, `/read unrelated <id>` | Manage reading list |
| `/digest` | Trigger a full daily run |
| `/run <phase>` | Run one phase (`research`/`website`/`todos`/`resume`/`curate`/`consolidate`) |
| `/plan <task>` | Plan a novel multi-step task, tracked as a todo |
| `/search <query>` | Web search with a synthesized answer |
| `/remind <+2h\|HH:MM> <message>` · `/remind list\|cancel <id>` | One-shot reminders |
| `/routine list\|cancel <id>` | Recurring routines (create by just describing one) |
| `/fin` · `/fin sum [YYYY-MM]` · `/fin list [YYYY-MM]` | Finance summary / records |
| `/fin <income\|expense> <amount> [category] [note]` · `/fin cat <id> <category>` · `/fin void <id>` | Log / recategorize / void a transaction |
| `/health` · `/health sum [days]` | Health summary (log by just describing, or send a food photo) |
| `/status` | Last run + open counts |

### Finance

Tell it money things in plain language — "午饭花了45", "工资到账32000", or send
a payment-receipt screenshot with "记一下" — and it logs them to
`finance.yaml` in your profile repo (git-versioned, local-only, wrong entries
voided rather than deleted). Each record carries a full `YYYY-MM-DD HH:MM`
identity — the transaction time read off the receipt or your message ("下午3
点打车" → 15:00), else the logging clock time — plus amount, income/expense,
context note, and currency. Stated times are what distinguish two same-priced
purchases; sending the same receipt twice — or a receipt for a payment the
agent already logged, however the note is worded — is rejected with a
pointer to the existing record, and a same-day same-amount near-miss gets
a ⚠ warning so you can void one if it's the same bill. Wrong
category? "把f37改成housing" or `/fin cat f37 housing`; wrong entry?
`/fin void f37` (voided, never deleted). Then ask things like *"这个月收支健康吗？怎么改善？"* — the monthly
totals, savings rate, and category breakdown are computed by code and handed
to the model, so the analysis cites your real numbers.

### Health

Tell it what you ate ("午饭吃了牛肉面"), your workout ("跑了30分钟"), or your
weight ("体重70.5") — or send a **photo of a meal, a nutrition label, or a
body scale** — and it logs to a health subprofile (`health.yaml` in the
profile repo, local-only, never rendered to the website). Food photos get
calorie/protein estimates (marked as estimates); labels are read verbatim,
ingredients checked against the nutrients you've asked it to track
("帮我记着要补维生素D"). Set body facts once ("我身高178，1999年的") and then
ask *"我最近健康状况怎么样？怎么改善？"* — BMI, weight trend, exercise
minutes, and daily calorie/protein averages are computed by code, so the
advice cites your real numbers. Wellness guidance only — it will point you
to a doctor for anything medical.

### Planning novel tasks

Hand it something it has no built-in action for — *"find a good Sichuan place
near the office for 6 on Friday"* — and it produces a concrete plan, splitting
what it can do (track it, remind you, draft messages, search) from what needs
you (send the message, book the table), names the next action, and tracks the
whole thing as a todo. It's honest about not having a calendar or browser — it
makes *your* steps as easy as possible rather than pretending to do them.

### Reminders and routines

- **Reminder** — one-shot: *"remind me in 2h to follow up with Gaohan"* →
  arrives as a proactive WeChat message.
- **Routine** — recurring, optionally conditional: *"every workday at 8:30 tell
  me if there's a weather alert in Shenzhen"* or *"every Monday summarize my
  open PRs"*. Conditions are evaluated at fire time (web search + LLM judge,
  conservatively). Managed with `/routine`.

  Schedules go beyond weekdays: *"每月1号提醒我交房租"* (`monthly:1`) or
  *"every year on March 15 remind me to renew the domain"* (`yearly:03-15`).
  Day-of-month clamps to short months (`monthly:31` fires Jun 30 / Feb 28),
  and `yearly:02-29` falls back to Feb 28 in non-leap years.

Both require WeChat configured (they push proactively).

---

## 7. Website

Five public pages (About, Experience, Education, Projects) render deterministically
from your profile — no LLM output ever reaches a public page, so nothing can be
fabricated. Three private pages (Todos, Reading, Routines) are encrypted.

- **Todos page** — a calendar of your important items plus a scrollable,
  date-grouped list. Each item has owner-only **Pin** / **Done** buttons.
  Items open longer than ~3 weeks show a "going stale" badge and expire at 30
  days (committed items — with a due date or someone waiting on you — never
  expire, they age *up*).
- **Reading page** — your unread papers, same layout, with **Done** and
  **Unrelated** buttons. "Unrelated" is negative feedback that steers future
  paper selection away from that topic.
- **Routines page** — your recurring routines and pending reminders.

**Owner mode.** The Pin/Done/Unrelated buttons are yours — enable them by
opening any page with `#owner` once (`#guest` turns them off); the flag persists
in that browser. Guests never see the buttons and their local marks are ignored,
so everyone else always sees your canonical list. Marks act instantly locally
and, with [marks sync](#34-website) configured, reach the agent so they count.

The site republishes every daily run and on `assistant run-phase website`.

---

## 8. Profile

Your profile (`~/.personal-agent/profile/profile.yaml`) is the hub — everything
downstream reads it. It's a **git repo**: every daily change is a commit, so
`git -C ~/.personal-agent/profile log -p` is a complete audit trail and
`git … revert HEAD` undoes any change.

### How it's maintained

- **Daily** — a constrained LLM pass emits *typed patch operations*
  (add evidence, bump last-seen, add skill/project, merge, move) that code
  applies. It can only append or adjust, never freely rewrite — so it can't
  silently corrupt your profile.
- **Weekly** — a consolidation pass (`assistant consolidate`, or the cron job)
  sees each section in full and does the editorial work: merges fragmented
  entries, moves misfiled evidence, and promotes clusters of evidence into
  résumé-grade contribution highlights. It also runs a quality audit
  (contradictions, stale claims, unsupported highlights) and emails the
  findings.

### Initiatives — grouping related work

Activity naturally fragments: the same project shows up under several repos.
`aliases.yaml` in your profile repo maps repos/keywords to **initiatives** so
correlated work converges onto one entry instead of scattering:

```yaml
initiatives:
  - name: My inference engine
    entry: my-project          # the profile project that owns this work
    patterns: [my-project, my-project-docs, "RFC #123", some-keyword]
```

Edit it to match your projects. The daily updater and weekly consolidation use
it as the join key.

### What you own

The agent **never edits** `identity`, `education`, or `experience` — fill those
in by hand in `profile.yaml`. Your hand-written `experience` section also serves
as the *style reference* the consolidation pass matches when writing highlights,
so make it good.

### Safety rails

- Every claim cites an observation; nothing is invented.
- Superseded highlights move to a `history` list — never deleted.
- Entries confirmed repeatedly resist rewrites that would cite fewer sources.
- Stale entries decay to `dormant` (skills/interests) rather than vanishing.

Details and the research behind it: [RESEARCH_AGENT_MEMORY_2026.md](RESEARCH_AGENT_MEMORY_2026.md).

---

## 9. Scheduling

Run once a day. Point any scheduler at `assistant run || assistant run --resume`
(the `--resume` re-enters a crashed run rather than restarting).

**cron:**

```cron
0 7 * * *  cd /path/to/personal-agent && /path/to/.venv/bin/assistant run || /path/to/.venv/bin/assistant run --resume
```

**systemd:** copy the templates in [`systemd/`](../systemd/), edit paths, then
`systemctl enable --now personal-agent.timer`.

**OpenClaw gateway:** required for the WeChat channel and proactive messages; it
runs the daily job from its own persistent cron and supervises the chat daemon.
See [WECHAT_OPENCLAW.md](WECHAT_OPENCLAW.md).

> ⚠️ **Timezone.** The run stamps dates and schedules from the machine's clock.
> On minimal container images that set `TZ` but ship no zoneinfo files,
> everything silently falls back to UTC — install `tzdata`. Set
> `PERSONAL_AGENT_TZ` to pin the zone for the Python side.

**Weekly consolidation** is a second, lighter job — schedule
`assistant consolidate` weekly (e.g. Sunday morning). Under OpenClaw it's the
`weekly-consolidate` cron job.

---

## 10. Troubleshooting

Run **`assistant init --check`** first — it diagnoses most configuration issues
directly.

| Symptom | Likely cause / fix |
|---|---|
| No digest email arrived | Check delivery: `assistant send-test-email`. If that works, check the run: `assistant run --dry-run` and read the console. |
| LLM errors ("authentication", "no JSON found") | Bad/expired key → `assistant init --check`. "No JSON / truncated" on a reasoning model means the token budget was too low — already tuned, but see the `llm-json-truncation-reasoning-models` skill if you hit it in new code. |
| Run crashed midway | `assistant run --resume` re-enters the exact phase it stopped at, reusing saved artifacts. |
| Profile learned something wrong | It's git: `git -C ~/.personal-agent/profile revert HEAD`. Then fix `aliases.yaml` or the protected sections if needed. |
| A collector returns nothing | Chrome: the History file is locked while Chrome runs / may not exist on this machine (the agent skips it gracefully). Gmail: needs SMTP creds. GitHub: check the token. `--check` shows each collector's status. |
| Chinese media / a feed is empty | Feeds are flaky by nature; the digest footer lists any source dead 3 days running. Point `config/sources.yaml` at a working mirror. |
| Website didn't update | `assistant run-phase website` runs it standalone and prints the result; check `WEBSITE_REPO` push access with `--check`. |
| WeChat replies stopped | The gateway is down — see the restart runbook in [WECHAT_OPENCLAW.md](WECHAT_OPENCLAW.md). |
| Résumé sync fails with "no git access" | Overleaf git bridge needs premium, and the token must own the project — see [§3.5](#35-résumé). |
| Reading list grows forever | You're surfacing faster than reading. Mark items done/unrelated — the adaptive quota throttles surfacing to match your pace (and the digest tells you it's doing so). |

**Logs.** The daily run logs to the console (capture it in your scheduler);
under OpenClaw, to `~/.personal-agent/daily-run.log`. The `runs/<run_id>/`
directory holds every artifact from a run for inspection.

**Skills.** When the agent (or you) resolves a recurring operational failure,
the fix is distilled into `skills/<name>/SKILL.md` — a growing library of
runbooks for this exact system. Worth skimming if you're debugging something
gnarly.

---

Questions the guide doesn't answer are usually answered by the
[Design doc](DESIGN.md) (how it's built) or by `assistant <command> --help`.
