# WeChat channel via OpenClaw (Tencent plugin)

Status: **live since 2026-07-03**. The owner chats with the assistant from
regular WeChat; OpenClaw is the transport, the personal-agent stays the brain.

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
  ▼
/rebase/.venv/bin/assistant ask "<message>"   ← the personal-agent, the only brain
```

You message **your own WeChat-connected bot**; the bridge plugin claims every
inbound message with a `before_agent_reply` hook (first `{handled: true}` wins,
runs before the model), execs `assistant ask`, and returns its stdout as the
reply. **OpenClaw's own LLM agent never runs for user messages** — it is pure
transport, so there is no persona to drift and no prompt-obedience risk.
Slash commands (`/new`, `/status`, …) and heartbeats fall through to OpenClaw's
normal handling. The workspace bootstrap files (`~/.openclaw/workspace/SOUL.md`
etc.) only matter as a fallback if the plugin is ever disabled.

## Security

- Only the owner's own logged-in WeChat account can reach the agent — the
  plugin binds to the account that scanned the QR, so the sender is inherently
  the owner and **no pairing approval is needed** (the pairing queue stays
  empty; that's expected, not a bug).
- The bridge hook runs a fixed binary (`assistant ask <message>`) — the message
  is a single argv element, never shell-interpreted, and the LLM cannot choose
  other commands because no LLM is in the loop before the personal-agent.
  Inside the personal-agent the usual rule applies: the model's write surface
  is the typed chat actions, nothing else.
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

## Restart runbook

All three assistant daemons die with the container (PID 1 is tini — nothing
supervises them). Bring everything back with:

```bash
nohup /rebase/personal-agent/scheduler.sh >/dev/null 2>&1 &                          # daily digest, 07:00 HKT
nohup /rebase/.venv/bin/assistant chat-listen >> ~/.personal-agent/chat.log 2>&1 &   # email (+Slack/WeCom) chat listener
nohup ~/.openclaw/start-gateway.sh >> ~/.openclaw/logs/gateway-nohup.log 2>&1 &      # WeChat gateway
```

Restart just the gateway (e.g. after config changes — they are only read at
startup): the process is titled **`openclaw`**, *not* "openclaw gateway", so:

```bash
pkill -x openclaw
nohup ~/.openclaw/start-gateway.sh >> ~/.openclaw/logs/gateway-nohup.log 2>&1 &
```

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
| Replies show "(assistant bridge error: …)" | the plugin *is* working; the personal-agent CLI failed — run `/rebase/.venv/bin/assistant ask "test"` in a terminal and fix what it reports (.env, ~/.personal-agent) |
| Config change has no effect | old process still serving — `pkill -x openclaw` (a kill pattern with "gateway" in it matches nothing) and relaunch |
| "Missing env var DEEPSEEK_ANTHROPIC_KEY" warning from CLI commands | harmless outside the launcher; only the gateway process needs the env var |
| Where did my message go? | `grep -a "inbound message\|outbound: text\|embedded_run_agent_end" /tmp/openclaw/openclaw-<date>.log` |

Logs if anything acts up: `/tmp/openclaw/openclaw-<date>.log` (gateway),
`~/.personal-agent/chat.log` (email listener), `~/.personal-agent/scheduler.log`
(daily runs).
