---
name: wechat-openclaw-bridge
description: Connect WeChat to the personal-agent via Tencent's official OpenClaw channel plugin, with a before_agent_reply hook plugin routing every message to `assistant ask` (no gateway LLM in the loop); covers the setup traps (gateway.mode, auth token, maxTokens, allowConversationAccess, hook timeouts, process title)
trigger: WeChat/微信 integration is requested, the OpenClaw gateway exits code 78 or "requires credentials before opening a websocket", WeChat replies are error notices or invented/generic prose, or a gateway config change doesn't take effect
modules: [chat, ops]
status: active
created_at: 2026-07-03
last_used_at: 2026-07-03
run_count: 0
---

## Diagnose
- Owner wants two-way WeChat. Unofficial bots (wechaty/itchat) are ban-risk;
  WeCom needs a public callback URL this container doesn't have. The sanctioned
  path is `@tencent-weixin/openclaw-weixin` (Tencent-published, iLink API, QR
  login like the desktop client) running inside an OpenClaw Gateway.
- Vet before running third-party installers: `npm view <pkg> --json`
  (maintainer emails, age, description) + cross-check the OpenClaw docs clone
  at `/rebase/reference-agents/openclaw/docs/channels/wechat.md`.
- Failure signatures: exit 78 + "missing gateway.mode"; "channels.start
  requires credentials before opening a websocket"; WeChat gets a short error
  reply + log shows "Anthropic Messages transport requires a positive
  maxTokens value"; config edits ignored (stale process still serving).

## Fix
1. Node ≥22.19 required: install to `/opt/node24`, leave system node alone;
   prefix commands with `PATH=/opt/node24/bin:$PATH`.
2. `npm i -g openclaw` then the plugin (the Tencent `-cli` installer or
   `openclaw plugins install "@tencent-weixin/openclaw-weixin"`). The installer
   may hang after success — verify with `openclaw plugins list`.
3. Config (all read only at startup):
   - `gateway.mode: "local"`, `gateway.auth.mode: "token"`,
     `gateway.auth.token: <random>`.
   - Custom provider: `models.providers.<id> = {baseUrl, apiKey:
     "${DEEPSEEK_ANTHROPIC_KEY}", api: "anthropic-messages", models: [{id,
     name, contextWindow, maxTokens: 8192}]}` — `maxTokens` is mandatory in
     practice despite docs claiming a default.
   - Never inline the real key; a launcher script
     (`~/.openclaw/start-gateway.sh`) exports it from the owner's .env and
     `exec openclaw gateway` (foreground — no systemd in the container).
4. Bridge to the personal-agent — use a **hook plugin, not a prompt**. A tiny
   plugin (`/rebase/personal-agent/openclaw-plugin/`: package.json with
   `openclaw.extensions: ["./index.js"]`, `openclaw.plugin.json`, plain-object
   default export) registers a `before_agent_reply` typed hook that execs
   `/rebase/.venv/bin/assistant ask <message>` and returns
   `{handled: true, reply: {text}}` — first claim wins, **before any model
   call**, so OpenClaw's LLM never runs for user messages. Install with
   `openclaw plugins install --link /rebase/personal-agent/openclaw-plugin`,
   then set `plugins.entries.personal-agent-bridge.hooks.allowConversationAccess:
   true` (non-bundled plugins can't register conversation hooks without it) and
   restart the gateway. In the handler: skip heartbeats (`ctx.trigger ===
   "heartbeat"`) and bodies starting `/` so commands keep working; bound the
   child process yourself (120 s) — do NOT set a config
   `hooks.timeouts.before_agent_reply`, a timed-out hook falls through to the
   gateway LLM.
   Why not a prompt bridge: instructing the gateway agent via workspace
   AGENTS.md was tried first and failed — the gateway seeds its own SOUL.md
   onboarding persona that drowns out a short AGENTS.md, and the agent chatted
   from its own head (`grep -c "assistant ask" <log>` → 0). Prompt files are
   now only the fallback if the plugin is disabled.
5. Login: `openclaw channels login --channel openclaw-weixin` (owner scans QR).
   No pairing approval needed — the plugin binds to the owner's own account,
   so the sender is inherently the owner.
6. Restarts: the gateway process is titled **`openclaw`** — `pkill -x
   openclaw`, then relaunch the nohup'd start script. `pkill -f "openclaw
   gateway"` matches nothing and leaves a stale process serving old config.
7. Permission classifiers block the agent from starting/killing the gateway
   (autonomous exec loop) — hand the owner exact one-liners instead.

## Verification
`openclaw plugins inspect personal-agent-bridge --runtime` → `Status: loaded`,
`Typed hooks: before_agent_reply (priority 100)`,
`allowConversationAccess: true`. `openclaw channels status --probe` →
"Gateway reachable" + `openclaw-weixin … running`. Owner sends a WeChat
message; `/tmp/openclaw/openclaw-<date>.log` shows `inbound message` →
`outbound: text sent OK` with **no model call in between**, and the reply
contains real todo/digest data. Handler logic is unit-testable without the
gateway: import index.js, call `register({on})`, invoke the captured handler
with `PERSONAL_AGENT_BIN=/bin/echo`.

## Anti-patterns
- Bridging via prompt instructions to the gateway's own LLM agent when a typed
  hook can bypass the LLM entirely — prompts drift, hooks don't.
- Setting `hooks.timeouts.before_agent_reply` in openclaw.json — on timeout
  the hook is skipped and the gateway LLM answers instead; bound the exec in
  the handler and return the error as the reply.
- Reaching for wechaty/itchat/web-protocol bots — account-ban territory when
  an official plugin path exists.
- Inlining live API keys into `openclaw.json` (persistent, inspectable) —
  env-ref + launcher injection instead.
- Editing config and expecting the running gateway to pick it up — it never
  does; restart it.
- Approving extra senders into pairing "to test" — every approved sender can
  drive the agent's exec tool.
- Diagnosing from the nohup file only — the structured log at
  `/tmp/openclaw/openclaw-<date>.log` has the per-run error previews.
