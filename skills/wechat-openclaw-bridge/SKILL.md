---
name: wechat-openclaw-bridge
description: Connect WeChat to the personal-agent via Tencent's official OpenClaw channel plugin, with a dual-hook bridge plugin (single_user = before_agent_reply → daemon HTTP with exec fallback; multi_tenant = before_dispatch → accountId routing, fail-closed, no CLI fallback) so no gateway LLM is in the loop; covers the setup traps (gateway.mode, auth token, maxTokens, allowConversationAccess, hook timeouts, process title) and the multi-tenant enablement gates
trigger: WeChat/微信 integration is requested, the OpenClaw gateway exits code 78 or "requires credentials before opening a websocket", WeChat replies are error notices or invented/generic prose, a gateway config change doesn't take effect, or multi-tenant WeChat routing misbehaves (wrong tenant, SAFE-reply loops, missing accountId)
modules: [chat, ops]
status: active
created_at: 2026-07-03
last_used_at: 2026-07-16
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
- Multi-tenant signatures: every weixin reply is the SAFE notice
  ("系统暂时不可用…") → the fail-closed path fired — check `ctx.accountId`
  presence (A.8 probe), the bridge token (daemon 401), and that the accountId
  is bound (`assistant admin list`); replies landing under the wrong user →
  binding or media-cache keying, never fall back to "*" keys.

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
4. Bridge to the personal-agent — use a **hook plugin, not a prompt**. The
   plugin (`/rebase/personal-agent/openclaw-plugin/`: package.json with
   `openclaw.extensions: ["./index.js"]`, `openclaw.plugin.json`, plain-object
   default export) is **dual-hook**; each hook is inert in the other mode, and
   the mode is re-read from the personal-agent `.env` (`DEPLOYMENT_MODE`) on
   **every call** — a mode/token change needs no gateway restart:
   - **single_user (default)**: `before_agent_reply` claims real user turns
     (`ctx.trigger === "user"` or missing), POSTs to the `assistant serve`
     daemon (`/chat` with a session id; slash commands → `/actions/*`), and
     only **falls back to exec** `/rebase/.venv/bin/assistant ask` when the
     daemon is down. Returns `{handled: true, reply: {text}}` — first claim
     wins, **before any model call**, so OpenClaw's LLM never runs for user
     messages.
   - **multi_tenant**: routing moves to **`before_dispatch`** — the ONLY hook
     that exposes `ctx.accountId` (`before_agent_reply` has `cleanedBody` but
     no accountId; at `before_dispatch` it's the reverse: read the text from
     `event.body ?? event.content`, `cleanedBody` is EMPTY there). The bridge
     sends `{channel:"weixin", account_id, session:"oc:<acct>:<sessionKey>"}`
     with the mandatory bridge token; the daemon maps accountId→uid via the
     registry. **Fail closed**: for a weixin turn every path returns
     `{handled:true, text:SAFE}` — missing accountId, daemon down, auth
     failure all yield the safe reply, never a bare `return` (which would let
     OpenClaw's own model answer with no tenant context — a leak). **No exec
     CLI fallback in this mode** — a default-user CLI run would answer as the
     wrong tenant. Images go as capped base64 **bytes** (`encodeBase64Capped`:
     MIME allowlist, 8 MB, ≤3), cache keyed `accountId::sessionKey`, no `"*"`
     fallback (cross-tenant leak).
   Install with
   `openclaw plugins install --link /rebase/personal-agent/openclaw-plugin`,
   then set `plugins.entries.personal-agent-bridge.hooks.allowConversationAccess:
   true` (non-bundled plugins can't register conversation hooks without it) and
   restart the gateway. Bound calls in the handler (120 s text / 300 s image) —
   do NOT set a config `hooks.timeouts.before_agent_reply`, a timed-out hook
   falls through to the gateway LLM.
   Why not a prompt bridge: instructing the gateway agent via workspace
   AGENTS.md was tried first and failed — the gateway seeds its own SOUL.md
   onboarding persona that drowns out a short AGENTS.md, and the agent chatted
   from its own head (`grep -c "assistant ask" <log>` → 0). Prompt files are
   now only the fallback if the plugin is disabled.
   **Claim only `ctx.trigger === "user"`** (or missing) at `before_agent_reply`:
   cron agent-turn jobs also fire it (with `trigger: "cron"`) — claiming them
   pipes cron prompts into the chat agent and misfires chat actions.
   Beware: an unbridged persona can CREATE cron jobs — the 2026-07-03 persona
   scheduled its own DIY `gh api` pipeline (daily-pipeline/website-sync/
   pr-check-noon); audit `openclaw cron list` after any persona incident and
   `openclaw cron disable <id>` the strays.
5. Same plugin can supervise residual pollers as a gateway service:
   `api.registerService({id, start(ctx), stop(ctx)})` (ctx has `logger`,
   `config`, `workspaceDir`); spawn the poller in `start`, respawn on exit
   with exponential backoff (reset after 5 min uptime), SIGTERM in `stop`.
   Kill any stale pid-file holder before the first spawn (a SIGKILL'd gateway
   orphans the child, which still holds the pid lock). Never spawn at module
   top level — plugin discovery evaluates the entry file; only `start()` runs
   on real gateway startup.
5. Login: `openclaw channels login --channel openclaw-weixin` (owner scans QR).
   **single_user**: no pairing approval needed — the plugin binds to the
   owner's own account, so the sender is inherently the owner.
   **multi_tenant**: one WeChat account per user, each logged into the same
   gateway. `accountId` routing is tenant *selection*, NOT sender
   *authentication* — each account additionally needs OpenClaw's per-account
   sender allowlist/pairing configured (DESIGN_MULTI_USER.md §11.3) so only
   that user's own WeChat can command their tenant. Prerequisites before
   flipping `DEPLOYMENT_MODE=multi_tenant`:
   (a) the **A.8 spike passed** — the probe at
   `/rebase/personal-agent/openclaw-plugin-spike/` (guide: `README.md` /
   `验证指南.md`) confirmed stable, distinct `ctx.accountId` per account at
   `before_dispatch`;
   (b) per-account sender allowlist verified (a non-allowlisted sender must be
   refused, not answered);
   (c) bridge token registered: `assistant admin set-bridge-token <token>`
   (daemon stores the hash) with the SAME plaintext as `SERVE_TOKEN` in `.env`
   (the bridge presents it) — in this mode the token is **mandatory on every
   endpoint**, never open-when-unset;
   (d) users registered/bound: `assistant admin add-user <uid>` +
   `assistant admin bind-channel <uid> weixin <accountId>`.
6. Restarts: the gateway process is titled **`openclaw`** — `pkill -x
   openclaw`, then relaunch the nohup'd start script. `pkill -f "openclaw
   gateway"` matches nothing and leaves a stale process serving old config.
7. Permission classifiers block the agent from starting/killing the gateway
   (autonomous exec loop) — hand the owner exact one-liners instead.

## Verification
`openclaw plugins inspect personal-agent-bridge --runtime` → `Status: loaded`,
`allowConversationAccess: true`, and **both** typed hooks registered:
`before_agent_reply (priority 100)` (single_user path) and `before_dispatch`
(multi_tenant path — which one actually claims a turn depends on
`DEPLOYMENT_MODE` in the .env). `openclaw channels status --probe` →
"Gateway reachable" + `openclaw-weixin … running`. Owner sends a WeChat
message; `/tmp/openclaw/openclaw-<date>.log` shows `inbound message` →
`outbound: text sent OK` with **no model call in between**, and the reply
contains real todo/digest data. In multi_tenant additionally: messages from
account A and account B land in separate `DATA_DIR/users/<uid>/` session
files, and `/chat` without the bridge token gets a 401.
Handler logic is unit-testable without the gateway: the full hermetic suite is
`/opt/node24/bin/node /rebase/personal-agent/openclaw-plugin/test.mjs`
(covers both hooks, fail-closed paths, media caps, supervisor); or import
index.js, call `register({on})`, and invoke the captured handlers directly.
For live accountId verification use the read-only probe
`openclaw-plugin-spike/` (A.8).

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
- Reading `ctx.accountId` at `before_agent_reply` — it's not there; only
  `before_dispatch` exposes it (and there the text is `event.body ??
  event.content`, NOT `cleanedBody`, which is empty).
- A bare `return` (fall-through) on a weixin turn in multi_tenant — OpenClaw's
  own model then answers with no tenant context; always return
  `{handled:true, text:SAFE}` on any failure.
- Exec'ing `assistant ask` as a multi_tenant fallback — it runs as the default
  user and answers as the wrong tenant; report the outage instead.
- Treating accountId routing as sender authentication — it only selects the
  tenant; the per-account sender allowlist/pairing is the actual auth.
- Enabling `DEPLOYMENT_MODE=multi_tenant` before the A.8 spike has proven
  stable per-account accountIds on real hardware.
