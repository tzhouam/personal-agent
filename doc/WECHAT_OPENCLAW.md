# WeChat channel via OpenClaw

This is the optional path that lets you chat with the agent from regular WeChat
on your phone. It uses [OpenClaw](https://openclaw.ai) as the transport and
Tencent's official WeChat plugin — **the personal-agent stays the brain**;
OpenClaw's own LLM never answers your messages.

If you only want email chat, skip this entirely — the email channel works with
just your SMTP credentials (see the [User Guide](USER_GUIDE.md#6-chat)). Set
this up when you want WeChat, proactive messages (reminders/routines), or want
the gateway to also run your daily schedule.

> **Note on specifics.** OpenClaw runs in your own environment, so exact paths
> (where Node lives, where OpenClaw's config sits) depend on your install.
> Commands below use placeholders like `<node-bin>` and `<repo>`; a concrete
> reference deployment is shown in the boxed "Example" callouts. Substitute your
> own values.

---

## How it flows

```
WeChat (your own account)
  │  Tencent iLink API (official, QR login — a real client, not a ban-risk bot)
  ▼
OpenClaw Gateway  (your machine; runs a Node process)
  │  channel plugin: @tencent-weixin/openclaw-weixin
  ▼
personal-agent-bridge plugin  (openclaw-plugin/ in this repo, --link installed)
  │  before_agent_reply hook → short-circuits BEFORE any model call
  │    /todo /read /digest /status /run /plan /search → POST /actions/<name>
  │    anything else                                  → POST /chat {session, text}
  ▼
assistant serve  (127.0.0.1:SERVE_PORT; session memory; email poll thread)
  │  fallback when the daemon is down: exec `assistant ask "<message>"`
  ▼
the personal-agent — the only brain
```

The bridge plugin does two jobs:

1. **Routes your messages.** A `before_agent_reply` hook claims every inbound
   user message (first `{handled: true}` wins, before any model call) and answers
   it from the `assistant serve` daemon over loopback HTTP — slash commands hit
   `/actions`, everything else hits `/chat`. OpenClaw's own LLM never runs for
   your messages, so there's no persona to drift and no prompt-injection surface.
2. **Supervises the daemon.** It registers a gateway service (`serve-supervisor`)
   that spawns and respawns `assistant serve` with exponential backoff, so the
   gateway is the single process you need alive.

Optionally, OpenClaw's persistent cron also runs your **daily pipeline** and
**weekly consolidation** as command jobs, making the gateway your one runtime
(no separate scheduler). See [Scheduling](#scheduling) below.

---

## Setup

### Prerequisites

- **Node ≥ 22.19** for the gateway (a separate install is fine if your system
  Node is older).
- **OpenClaw** installed globally (`npm i -g openclaw`).
- The **WeChat channel plugin**: `@tencent-weixin/openclaw-weixin`.
- This repo, with `assistant` installed and a working `.env` (verify with
  `assistant init --check`).

### 1. Install the bridge plugin

From an environment where the OpenClaw CLI is on `PATH`:

```bash
openclaw plugins install --link <repo>/openclaw-plugin
```

`--link` keeps it pointed at the repo so plugin updates are just a `git pull`.

### 2. Configure the gateway

These are the non-default settings the bridge needs (`openclaw config set …` or
edit `openclaw.json`):

| Setting | Value | Why |
|---|---|---|
| `gateway.mode` | `local` | Gateway refuses to start otherwise (exit 78). |
| `gateway.auth.mode` + `gateway.auth.token` | `token` + a random string | The channel websocket won't open without credentials. |
| `plugins.entries.personal-agent-bridge.enabled` | `true` | Enable the bridge. |
| `plugins.entries.personal-agent-bridge.hooks.allowConversationAccess` | `true` | **Required** — non-bundled plugins can't register the `before_agent_reply` hook without it; it silently stays unregistered otherwise. |
| `agents.defaults.heartbeat.every` | `0m` | Otherwise the gateway wakes its model every 30 min for nothing. |
| a model provider entry | your LLM (see below) | Only reached by OpenClaw's own `/`-commands and heartbeats — never your messages — but it must be valid or the gateway complains. |

**LLM provider entry.** Point OpenClaw at the same LLM you gave the
personal-agent. Reference it from an env var rather than pasting the key into
`openclaw.json`, and inject that var when you launch the gateway. For an
Anthropic-compatible provider set `api: "anthropic-messages"`, the `baseUrl`,
and — importantly — a positive `maxTokens` on the model entry (the Anthropic
transport errors with "requires a positive maxTokens value" otherwise).

**Do not** set `hooks.timeouts.before_agent_reply`: a timed-out hook falls
through to OpenClaw's own LLM. The plugin bounds its own calls and returns any
error inside the bridge instead.

### 3. Connect your bridge to the daemon (optional token)

The bridge reads `SERVE_PORT` and `SERVE_TOKEN` from the personal-agent `.env`.
If you set `SERVE_TOKEN`, the loopback API requires it and the bridge sends it
automatically. The socket is loopback-only (`127.0.0.1`) regardless.

### 4. Launch the gateway and log in

Start the gateway (inject your LLM key first). A tiny launcher script that sets
`PATH`, injects the key, and `exec`s `openclaw gateway` is the tidy way:

```bash
nohup <launcher> >> <log> 2>&1 &
```

Then log the WeChat channel in once (persists afterward):

```bash
openclaw channels login --channel openclaw-weixin
```

Scan the QR with WeChat. Because the plugin binds to the account that scanned,
**the sender is inherently you** — no pairing approval is needed (an empty
pairing queue is expected, not a bug).

> **Example (reference deployment):** Node 24 at `/opt/node24/bin`, OpenClaw
> config at `~/.openclaw/openclaw.json`, launcher `~/.openclaw/start-gateway.sh`
> (injects `DEEPSEEK_ANTHROPIC_KEY` from the repo `.env`, then
> `exec openclaw gateway`), gateway log `/tmp/openclaw/openclaw-<date>.log`.
> Login: `PATH=/opt/node24/bin:$PATH openclaw channels login --channel openclaw-weixin`.

That's it — message your WeChat bot and the agent answers.

---

## Scheduling

Instead of cron/systemd, you can let the gateway's SQLite-persisted cron run the
pipeline as **command jobs** (so a WeChat-only deployment needs no other
scheduler):

```
daily-digest        0 7 * * *  →  scripts/daily-run.sh   (assistant run || run --resume)
weekly-consolidate  0 8 * * 0  →  assistant consolidate
```

`daily-run.sh` holds a flock, logs the full run to
`~/.personal-agent/daily-run.log`, and emits a one-line status. The deliver
phase announces successes to WeChat itself (`WECHAT_ANNOUNCE=true`), so the cron
job only needs to carry failures.

```bash
openclaw cron list                 # your jobs
openclaw cron run <jobId> --wait   # force one now (sends the real digest!)
openclaw cron runs --id <jobId>    # run history
```

---

## Security

- **Only you can reach the agent** — the plugin binds to your logged-in WeChat
  account.
- **No shell interpretation, no LLM in front of the brain.** The bridge posts
  your message as a JSON body to the loopback socket (or passes it as a single
  argv element in the exec fallback). Nothing is shell-parsed, and no model runs
  before the personal-agent, so the only write surface is the agent's typed
  action registry.
- **Keys stay out of OpenClaw's config.** Reference your LLM key as
  `${ENV_VAR}` in `openclaw.json` and inject it at launch — it never lands in a
  config file on disk.

---

## Restart runbook

The gateway is the one daemon; it brings cron, the chat daemon, and WeChat back
with it. To (re)start:

```bash
pkill -x openclaw            # stop (process is titled "openclaw", not "openclaw gateway")
nohup <launcher> >> <log> 2>&1 &
```

Config changes are read only at startup, so restart after editing
`openclaw.json`. Cron jobs and channel credentials persist under OpenClaw's
state dir, so a restart doesn't lose them; the supervised daemon reclaims a
stale pid-lock on startup.

**Dead-man signal:** if your daily digest doesn't arrive, the gateway is
probably down — run the restart line.

> **Example (reference deployment):** a guarded block in `~/.bashrc` auto-revives
> the gateway on the first interactive shell after a container restart, so the
> practical restart cost is usually zero. On minimal container images, also
> ensure `tzdata` is installed — some set `TZ` but ship no zoneinfo files, and
> everything silently falls back to UTC (see the User Guide's timezone note).

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Gateway exits 78, "missing gateway.mode" | `openclaw config set gateway.mode local` |
| "channels.start requires credentials before opening a websocket" | set `gateway.auth.mode token` + `gateway.auth.token` |
| WeChat replies with a short error; log shows "requires a positive maxTokens" | add `maxTokens` to the provider's model entry, restart the gateway |
| Replies look invented / generic instead of data-backed | the bridge isn't intercepting — `openclaw plugins inspect personal-agent-bridge --runtime` must show `Status: loaded` + `before_agent_reply (priority 100)`; check `enabled` + `hooks.allowConversationAccess`, then restart |
| Replies show "(assistant bridge error: …)" | the plugin works; both the daemon and the exec fallback failed — run `assistant ask "test"` in a terminal and fix what it reports (.env, data dir) |
| Slash commands answer "(assistant daemon unreachable: …)" | `assistant serve` is down (slash commands have no exec fallback) — `curl -s http://127.0.0.1:<SERVE_PORT>/healthz` should return `{"ok": true}`; check the gateway log for supervisor respawn lines |
| Replies lost conversation memory | daemon down, bridge degraded to exec fallback — same fix; memory lives in `~/.personal-agent/sessions/` |
| No email replies | email polling lives inside `assistant serve` — `pgrep -af "assistant serve"`; the gateway service respawns it |
| Config change has no effect | old process still serving — `pkill -x openclaw` and relaunch |
| Where did my message go? | grep the gateway log for `inbound message` / `outbound: text` |

Logs: the gateway log (includes the supervised `assistant serve` stdout) and
`~/.personal-agent/daily-run.log` (daily runs). The service-layer design behind
the HTTP bridge is in [DESIGN_SERVICE_LAYER.md](DESIGN_SERVICE_LAYER.md).
