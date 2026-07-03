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
OpenClaw agent  (provider deepseek-anthropic → api.deepseek.com/anthropic)
  │  ~/.openclaw/workspace/AGENTS.md instructs it to delegate
  ▼
/rebase/.venv/bin/assistant ask "<message>"   ← the personal-agent, real data
```

You message **your own WeChat-connected bot**; OpenClaw's agent answers, and for
anything about todos / digest / profile / reading list it is instructed to run
`assistant ask` (exec tool) and relay the real data instead of answering from
its own head. If a reply ever looks invented rather than data-backed, harden the
bridge prompt in `~/.openclaw/workspace/AGENTS.md`.

## Security

- Only the owner's own logged-in WeChat account can reach the agent — the
  plugin binds to the account that scanned the QR, so the sender is inherently
  the owner and **no pairing approval is needed** (the pairing queue stays
  empty; that's expected, not a bug).
- The OpenClaw agent has an exec tool (that's how the bridge works), so anyone
  who could message it could drive shell commands. With account-bound identity
  that's only the owner; still, don't approve other senders into pairing.
- The DeepSeek key is **never stored in OpenClaw's config**: `openclaw.json`
  references `${DEEPSEEK_ANTHROPIC_KEY}` and the launcher injects it from
  `/rebase/personal-agent/.env` at start time.

## Components & files

| Piece | Where |
|---|---|
| Node 24 (gateway needs ≥22.19; system node stays v20) | `/opt/node24/bin` |
| OpenClaw + config | `openclaw` 2026.6.11 global npm; `~/.openclaw/openclaw.json` |
| WeChat plugin | `@tencent-weixin/openclaw-weixin` 2.4.6 (installed via `@tencent-weixin/openclaw-weixin-cli`) |
| Bridge prompt | `~/.openclaw/workspace/AGENTS.md` |
| Launcher | `~/.openclaw/start-gateway.sh` (PATH + key injection + `exec openclaw gateway`) |
| Gateway log | `/tmp/openclaw/openclaw-<date>.log` (+ `~/.openclaw/logs/gateway-nohup.log`) |

Required config that is NOT the default (`openclaw config set …` or edit json):

- `gateway.mode: "local"` — gateway refuses to start without it (exit 78).
- `gateway.auth.mode: "token"` + `gateway.auth.token: <random>` — CLI/channel
  websocket refuses to open without credentials.
- `models.providers.deepseek-anthropic`: `baseUrl`, `apiKey: "${DEEPSEEK_ANTHROPIC_KEY}"`,
  `api: "anthropic-messages"`, and the model entry **must set `maxTokens`**
  (e.g. 8192) — the Anthropic transport errors with "requires a positive
  maxTokens value" otherwise, and the WeChat reply is just an error notice.

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
| Config change has no effect | old process still serving — `pkill -x openclaw` (a kill pattern with "gateway" in it matches nothing) and relaunch |
| "Missing env var DEEPSEEK_ANTHROPIC_KEY" warning from CLI commands | harmless outside the launcher; only the gateway process needs the env var |
| Where did my message go? | `grep -a "inbound message\|outbound: text\|embedded_run_agent_end" /tmp/openclaw/openclaw-<date>.log` |

Logs if anything acts up: `/tmp/openclaw/openclaw-<date>.log` (gateway),
`~/.personal-agent/chat.log` (email listener), `~/.personal-agent/scheduler.log`
(daily runs).
