---
name: wechat-openclaw-bridge
description: Connect WeChat to the personal-agent via Tencent's official OpenClaw channel plugin, with OpenClaw's agent delegating to `assistant ask`; covers the four setup traps (gateway.mode, auth token, maxTokens, process title)
trigger: WeChat/微信 integration is requested, the OpenClaw gateway exits code 78 or "requires credentials before opening a websocket", WeChat replies are error notices, or a gateway config change doesn't take effect
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
4. Bridge to the personal-agent: `agents.defaults.workspace` +
   `~/.openclaw/workspace/AGENTS.md` telling the agent to run
   `/rebase/.venv/bin/assistant ask "<message>"` for any data question and
   relay the output.
5. Login: `openclaw channels login --channel openclaw-weixin` (owner scans QR).
   No pairing approval needed — the plugin binds to the owner's own account,
   so the sender is inherently the owner.
6. Restarts: the gateway process is titled **`openclaw`** — `pkill -x
   openclaw`, then relaunch the nohup'd start script. `pkill -f "openclaw
   gateway"` matches nothing and leaves a stale process serving old config.
7. Permission classifiers block the agent from starting/killing the gateway
   (autonomous exec loop) — hand the owner exact one-liners instead.

## Verification
`openclaw channels status --probe` → "Gateway reachable" + `openclaw-weixin …
running`. Owner sends a WeChat message; `/tmp/openclaw/openclaw-<date>.log`
shows `inbound message` → (no `embedded_run_agent_end` errors) → `outbound:
text sent OK`, and the reply contains real todo/digest data (delegation to
`assistant ask` worked), not generic prose.

## Anti-patterns
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
