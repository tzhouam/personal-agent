# personal-agent

Daily self-assistant agent. See [DESIGN.md](DESIGN.md) for the full design;
this is **Milestone 1**: profile store + GitHub collector + notification digest + email + timer.

## Install

```bash
source /rebase/.venv/bin/activate
pip install -e /rebase/personal-agent
cp .env.template .env   # fill in tokens
```

## Usage

```bash
assistant bootstrap        # first time only: seed profile.yaml from GitHub
assistant show-profile
assistant run --dry-run    # full pipeline, digest written to disk, no email
assistant run              # daily: collect → profile → resume → digest → research → email → curate
assistant run --resume     # re-enter the phase where a crashed run stopped
assistant send-test-email
assistant resume-init      # clone the Overleaf project (set RESUME_REMOTE_URL first)
assistant resume-status    # show a resume update awaiting approval
assistant approve-resume   # push the approved update to Overleaf
```

### Resume sync (Overleaf)

Requires the Overleaf **git bridge** (premium): set `RESUME_REMOTE_URL` in `.env` to the
project's git URL and run `assistant resume-init`. The agent edits the LaTeX locally with
exact search/replace edits grounded in the profile, gates on a LaTeX compile when a
toolchain is installed, and commits **locally only** — the daily email shows the diff and
nothing reaches Overleaf until you run `assistant approve-resume` (which pulls/rebases
remote edits first and never force-pushes).

Data lives in `~/.personal-agent/`:
- `profile/` — git repo holding `profile.yaml` (source of truth) + `PROFILE.md` (render).
  Every daily update is a commit; `git -C ~/.personal-agent/profile log -p` is the audit trail.
  **Fill in `education:` / `experience:` manually — the agent never edits those sections.**
- `events.db` — SQLite+FTS5 raw observation log + surfaced-item dedup store.
- `runs/<run_id>/` — per-run artifacts (observations, digest JSON/HTML) used by `--resume`.
- `state.json` — phase marker (named phase = phase to re-enter).

## Chat with the agent

A background listener answers messages from the owner and can execute typed
actions (add/close todos, mark reading done, trigger a digest run):

```bash
assistant ask "what's due this week?"     # one-off, local
assistant chat-listen                     # foreground loop (normally you don't run
                                          # this yourself — the OpenClaw gateway
                                          # supervises it as a plugin service)
```

- **Email (works out of the box)**: mail the digest mailbox from one of your
  own addresses with a subject starting `agent` — e.g. "agent: add a todo to
  review X, due Friday". The reply comes back by email. Non-owner senders and
  other subjects are ignored; a UID watermark prevents replays.
- **WeChat via OpenClaw (live)**: Tencent's official
  `@tencent-weixin/openclaw-weixin` plugin runs in an OpenClaw Gateway on this
  machine, and our [`openclaw-plugin/`](openclaw-plugin/) bridge (a
  `before_agent_reply` hook) routes every inbound message straight to
  `assistant ask` — the gateway's own LLM never runs, OpenClaw is transport
  only. Setup, restart runbook, and troubleshooting:
  [doc/WECHAT_OPENCLAW.md](doc/WECHAT_OPENCLAW.md).
- **WeChat via WeCom (企业微信, alternative)**: register a free WeCom org +
  self-built app, enable the WeChat plugin (我→设置→插件→企业微信, scan QR) —
  the agent then messages you *inside WeChat*. Set
  `WECOM_CORP_ID/SECRET/AGENT_ID/OWNER_USERID` for push; receiving your replies
  additionally needs the app's callback URL publicly routed to this machine
  (`WECOM_TOKEN`/`WECOM_AES_KEY`, port 8329 — a tunnel or small VPS). See
  `.env.template`.

## Schedule (daily 07:00 HKT)

The OpenClaw gateway is the single runtime: its SQLite-persisted **command
cron** runs [`scripts/daily-run.sh`](scripts/daily-run.sh) (`run || run
--resume`, full logs → `~/.personal-agent/daily-run.log`, stdout = the one-line
WeChat announce) at 07:00 Asia/Hong_Kong, and the bridge plugin supervises the
chat listener as a gateway service. Job management:

```bash
export PATH=/opt/node24/bin:$PATH
openclaw cron list                 # the job is named daily-digest
openclaw cron run <jobId> --wait   # force a run now (sends the real digest!)
openclaw cron runs --id <jobId>    # run history + captured summary lines
```

Fallbacks when not using OpenClaw — systemd host:

```bash
sudo cp systemd/personal-agent.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now personal-agent.timer
```

or the loop scheduler (`nohup ./scheduler.sh &`, logs `~/.personal-agent/scheduler.log`).

### Restart runbook (after a container restart)

Usually **zero lines**: a guarded block in `~/.bashrc` revives the gateway the
first time any interactive shell opens after a restart (verified by drill).
Manual equivalent — the gateway brings cron, the chat listener, and WeChat
back with it:

```bash
nohup ~/.openclaw/start-gateway.sh >> ~/.openclaw/logs/gateway-nohup.log 2>&1 &
```

(Restart-only variant: `pkill -x openclaw` first — the process is titled `openclaw`.)
Dead-man signal: if the 07:00 digest email/WeChat ping doesn't arrive, the
gateway is down — run the line above.

⚠️ After a container **rebuild**, reinstall tzdata (`apt-get install -y
tzdata`): the base image sets `TZ=Asia/Shanghai` but ships **no zoneinfo
files**, so everything silently falls back to UTC — digest dates shift a day
and wall-clock schedules drift 8 h. OpenClaw's cron is immune (Node bundles
ICU), and `daily-run.sh` + the bridge pin `TZ` for the Python side, but the
pin only works when tzdata exists. Logs: `/tmp/openclaw/openclaw-<date>.log`
(gateway incl. chat listener), `~/.personal-agent/daily-run.log` (daily runs),
`~/.personal-agent/chat.log` (legacy standalone listener only).

## Architecture (M1 slice)

```
collect (GitHub events + notifications, pluggable registry)
  → profile update (LLM emits typed patch ops; code applies; git commit)
  → digest (deterministic reason-buckets + LLM triage vs profile; seen-dedup)
  → deliver (HTML email via Resend, SMTP fallback)
```

Later milestones (see DESIGN.md §9): Chrome + Gmail collectors (M2), arXiv/blog/中文媒体
research digest (M3), Overleaf resume sync with approval gate (M4), curator + skills (M5).
