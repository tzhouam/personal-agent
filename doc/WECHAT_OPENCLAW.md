# WeChat channel via OpenClaw (Tencent plugin)

Status: **live since 2026-07-03**. The owner chats with the assistant from
regular WeChat; OpenClaw is the transport, the personal-agent stays the brain.

Since 2026-07-03 evening the gateway is also the **single runtime** for the
whole assistant: its command-cron runs the daily pipeline (job `daily-digest`,
07:00 Asia/Hong_Kong → `scripts/daily-run.sh`, one-line result announced to
WeChat) and the bridge plugin supervises the Python daemon as a gateway
service — no separate scheduler/listener daemons.

Since **2026-07-09** the supervised daemon is **`assistant serve`** (HTTP on
127.0.0.1:8377 + email polling in one process) and the bridge talks to it over
HTTP instead of exec-per-message: replies are faster, `.env` edits (key
rotation!) apply on the next message, chats have per-conversation memory, and
`/todo` `/read` `/digest` `/status` are answered with no LLM call at all.
See [DESIGN_SERVICE_LAYER.md](DESIGN_SERVICE_LAYER.md).

## How it flows

```
WeChat (owner's own account)
  │  Tencent iLink API (official, QR login — not a ban-risk bot)
  ▼
OpenClaw Gateway  (~/.openclaw, Node 24 at /opt/node24, foreground under nohup)
  │  channel plugin: @tencent-weixin/openclaw-weixin
  ▼
personal-agent-bridge plugin  (openclaw-plugin/ in this repo, --link installed)
  │  before_agent_reply typed hook → short-circuits BEFORE any model call
  │    /todo /read /digest /status → POST /actions/<name>  (no LLM at all)
  │    anything else               → POST /chat {session, text}
  ▼
assistant serve  (127.0.0.1:8377; session memory; email poll thread)
  │  fallback when the daemon is down: exec `assistant ask "<message>"`
  ▼
the personal-agent — the only brain
```

The same plugin registers a gateway service (`serve-supervisor`) that spawns
and respawns `assistant serve` (exponential backoff 5s→300s, stale-pid
takeover of the old standalone listener's pid file), and the gateway's cron
owns the daily run:

```
openclaw cron (job daily-digest, 0 7 * * * Asia/Hong_Kong, --exact, command payload)
  → scripts/daily-run.sh  (flock; assistant run || assistant run --resume;
                           logs → ~/.personal-agent/daily-run.log)
  → stdout (one line) announced to WeChat; full digest still emailed
```

You message **your own WeChat-connected bot**; the bridge plugin claims every
inbound message with a `before_agent_reply` hook (first `{handled: true}` wins,
runs before the model) and answers it from the serve daemon (exec fallback).
**OpenClaw's own LLM agent never runs for user messages** — it is pure
transport, so there is no persona to drift and no prompt-obedience risk.
The bridge's own slash commands (`/todo`, `/read`, `/digest`, `/status`) are
served from the action registry with no LLM; other slash commands (`/new`, …)
and heartbeats fall through to OpenClaw's normal handling. The workspace bootstrap files (`~/.openclaw/workspace/SOUL.md`
etc.) only matter as a fallback if the plugin is ever disabled.

## Security

- Only the owner's own logged-in WeChat account can reach the agent — the
  plugin binds to the account that scanned the QR, so the sender is inherently
  the owner and **no pairing approval is needed** (the pairing queue stays
  empty; that's expected, not a bug).
- The bridge posts the message as a JSON body to a loopback-only socket
  (127.0.0.1) — optionally bearer-token-guarded via `SERVE_TOKEN` in `.env` —
  and the exec fallback passes it as a single argv element; neither path is
  ever shell-interpreted, and the LLM cannot choose other commands because no
  LLM is in the loop before the personal-agent. Inside the personal-agent the
  usual rule applies: the model's write surface is the typed action registry,
  nothing else.
- The DeepSeek key is **never stored in OpenClaw's config**: `openclaw.json`
  references `${DEEPSEEK_ANTHROPIC_KEY}` and the launcher injects it from
  `/rebase/personal-agent/.env` at start time.

## Components & files

| Piece | Where |
|---|---|
| Node 24 (gateway needs ≥22.19; system node stays v20) | `/opt/node24/bin` |
| OpenClaw + config | `openclaw` 2026.6.11 global npm; `~/.openclaw/openclaw.json` |
| WeChat plugin | `@tencent-weixin/openclaw-weixin` 2.4.6 (installed via `@tencent-weixin/openclaw-weixin-cli`) |
| **Bridge plugin (the brain hookup)** | `openclaw-plugin/` in this repo — `openclaw plugins install --link /rebase/personal-agent/openclaw-plugin` |
| Fallback prompt (only if plugin disabled) | `~/.openclaw/workspace/SOUL.md` + `AGENTS.md` |
| Launcher | `~/.openclaw/start-gateway.sh` (PATH + key injection + `exec openclaw gateway`) |
| Gateway log | `/tmp/openclaw/openclaw-<date>.log` (+ `~/.openclaw/logs/gateway-nohup.log`) |

Required config that is NOT the default (`openclaw config set …` or edit json):

- `gateway.mode: "local"` — gateway refuses to start without it (exit 78).
- `gateway.auth.mode: "token"` + `gateway.auth.token: <random>` — CLI/channel
  websocket refuses to open without credentials.
- `models.providers.deepseek-anthropic`: `baseUrl`, `apiKey: "${DEEPSEEK_ANTHROPIC_KEY}"`,
  `api: "anthropic-messages"`, and the model entry **must set `maxTokens`**
  (e.g. 8192) — the Anthropic transport errors with "requires a positive
  maxTokens value" otherwise. (Only reached by slash commands/heartbeats now —
  user messages never touch the model.)
- `plugins.entries.personal-agent-bridge.enabled: true` **and**
  `plugins.entries.personal-agent-bridge.hooks.allowConversationAccess: true` —
  non-bundled plugins may not register conversation hooks (`before_agent_reply`)
  without the latter; the hook silently stays unregistered.
- Do **not** set `hooks.timeouts.before_agent_reply`: a timed-out hook falls
  through to OpenClaw's own LLM. The plugin bounds the CLI call itself (120 s)
  and returns the error inside the bridge instead.
- `agents.defaults.heartbeat.every: "0m"` — otherwise the gateway wakes the
  DeepSeek model every 30 min for nothing.

## Restart runbook

The gateway is the only daemon (PID 1 is tini — nothing supervises it), and a
guarded block in `~/.bashrc` auto-revives it on the first interactive shell
after a container restart (drill-verified 2026-07-03). Manual start — it
brings cron (daily digest), the chat listener, and WeChat back with it:

```bash
nohup ~/.openclaw/start-gateway.sh >> ~/.openclaw/logs/gateway-nohup.log 2>&1 &
```

To restart (e.g. after config changes — they are only read at startup): the
process is titled **`openclaw`**, *not* "openclaw gateway", so
`pkill -x openclaw` first, then the line above. Cron jobs persist in
`~/.openclaw/state/openclaw.sqlite`; the supervised daemon kills any stale
`chat_listener.pid` holder on startup and takes over.

Re-login (rarely; credentials persist under `~/.openclaw`): run
`PATH=/opt/node24/bin:$PATH openclaw channels login --channel openclaw-weixin`
in a real terminal and scan the QR with WeChat.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Gateway exits code 78, log says "missing gateway.mode" | `openclaw config set gateway.mode local` |
| "channels.start requires credentials before opening a websocket" | set `gateway.auth.mode token` + `gateway.auth.token` |
| WeChat replies with a short error; log shows "requires a positive maxTokens" | add `maxTokens` to the provider's `models[]` entry, restart gateway |
| Replies look invented / generic instead of data-backed | the bridge plugin isn't intercepting — `openclaw plugins inspect personal-agent-bridge --runtime` must show `Status: loaded` + `before_agent_reply (priority 100)`; check `enabled` + `hooks.allowConversationAccess` in openclaw.json, then restart the gateway. (Historical variant of this failure: before the plugin existed, delegation relied on the workspace SOUL.md/AGENTS.md prompt, and OpenClaw's seeded onboarding persona drowned it out — the prompt files are only a fallback now.) |
| Replies show "(assistant bridge error: …)" | the plugin *is* working; both the daemon and the exec fallback failed — run `/rebase/.venv/bin/assistant ask "test"` in a terminal and fix what it reports (.env, ~/.personal-agent) |
| Slash commands answer "(assistant daemon unreachable: …)" | `assistant serve` is down and slash commands have no exec fallback — `curl -s http://127.0.0.1:8377/healthz` should return `{"ok": true}`; check gateway log for `[personal-agent-bridge] assistant serve` respawn lines |
| Replies lost conversation memory ("what about the second one?" draws a blank) | daemon down, bridge degraded to exec fallback — same fix as above; memory lives in `~/.personal-agent/sessions/` |
| Mystery cron jobs with LLM prompts (`gh api …`) appear in `openclaw cron list` | the pre-bridge DeepSeek persona created its own DIY pipeline jobs (2026-07-03: daily-pipeline / website-sync / pr-check-noon — now disabled). Disable, don't imitate: the real pipeline is the `daily-digest` **command** job. The bridge only claims `trigger === "user"`, so agent-turn cron prompts are never piped into `assistant ask` |
| No email replies | email polling lives inside `assistant serve` — `pgrep -af "assistant serve"`; the gateway service respawns it (backoff 5s→300s); check the gateway log for `[personal-agent-bridge] assistant serve` lines |
| Config change has no effect | old process still serving — `pkill -x openclaw` (a kill pattern with "gateway" in it matches nothing) and relaunch |
| "Missing env var DEEPSEEK_ANTHROPIC_KEY" warning from CLI commands | harmless outside the launcher; only the gateway process needs the env var |
| Where did my message go? | `grep -a "inbound message\|outbound: text\|embedded_run_agent_end" /tmp/openclaw/openclaw-<date>.log` |

Logs if anything acts up: `/tmp/openclaw/openclaw-<date>.log` (gateway, incl.
the supervised `assistant serve` stdout), `~/.personal-agent/daily-run.log`
(daily runs). `chat.log`/`scheduler.log` are legacy (standalone listener /
pre-OpenClaw scheduler).
