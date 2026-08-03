---
name: wechat-outbound-context-token
description: Proactive WeChat pushes (reminders/routines/announce) fail with `OutboundDeliveryError: sendMessage ret=-2 errmsg=prepare failed` once the owner hasn't messaged for ~24h, then "self-heal" when they message again — the CLI direct-send path drops the Weixin context token; patch getContextToken to restore the disk-persisted token, and never diagnose this as "channel down, needs reboot"
trigger: reminder/routine/announce delivery fails with `sendMessage ret=-2 errmsg=prepare failed` while inbound chat replies still work; structured log shows `sendWeixinOutbound: contextToken missing … sending without context`; push failures cluster ~24h after the owner's last inbound message and clear on their next message
modules: [ops, notify]
status: active
created_at: 2026-08-03
---

## Diagnose
- Split symptom: **inbound chat replies work, proactive pushes fail** — the
  bridge reply rides the gateway's live session; `notify.send_wechat` shells
  `openclaw message send`, a separate short-lived process.
- Signature in the serve log: `failed: rc=1 OutboundDeliveryError: sendMessage
  ret=-2 errmsg=prepare failed`. In `/tmp/openclaw/openclaw-<date>.log` the
  same send shows `sendWeixinOutbound: contextToken missing for to=…, sending
  without context`.
- Confirm the window pattern before touching anything: list every push outcome
  (`grep -E "reminder m[0-9]+ |routine rt|announce" gateway-nohup.log`) against
  the owner's last inbound (session file mtimes). 2026-07-31→08-02: every send
  ≤24h after an inbound succeeded, every send past ~24h failed, and the channel
  "recovered" the moment the owner messaged — that is not an outage, it is the
  Weixin iLink push window for context-less sends.
- Mechanism (plugin `@tencent-weixin/openclaw-weixin`, dist/src): every inbound
  message yields a per-conversation `context_token`, cached in-process and
  persisted to `~/.openclaw/openclaw-weixin/accounts/<accountId>.context-tokens.json`.
  `restoreContextTokens` (disk→memory) runs **only** in `gateway.startAccount`.
  The channel declares `outbound.deliveryMode: "direct"`, so `openclaw message
  send` loads the channel in its own fresh process, never runs `startAccount`,
  finds an empty store, and sends without the token — accepted only inside the
  ~24h window, `prepare failed` outside it.

## Fix
1. Patch `getContextToken` in the **loaded** plugin copy (find it with
   `openclaw plugins list` — the npm-project path under `~/.openclaw/npm/…`,
   NOT the `/opt/node*/lib/node_modules` copy) at
   `dist/src/messaging/inbound.js`: on an in-memory miss, call
   `restoreContextTokens(accountId)` once per account, then re-read. Safe in
   the gateway too: disk is written on every `setContextToken`, so it is never
   older than memory. No gateway restart needed — every CLI send loads the
   patched file fresh.
2. The patch is inside a third-party npm package: re-check it after every
   plugin update (`node --check` the file; grep for `restoredOnMiss`), and keep
   the upstream report alive — the durable fix belongs in the plugin (restore
   persisted tokens on direct-mode sends) or in openclaw (route direct sends
   through the running gateway).
3. Residual gap: a token also ages; if Weixin rejects a days-old token the push
   still fails. That path is already handled — bounded retries dead-letter the
   reminder and the D5 failure surface resends it in-chat on the owner's next
   turn. Do not add unbounded retries back.

## Verification
- `openclaw message send --channel weixin --account <acct> --target <peer> -m
  <test>` → structured log shows **no** `contextToken missing` warning and
  `✅ Sent`; the message arrives on the phone.
- The real proof is a push >24h after the owner's last inbound (next silent
  day): reminder/routine line logs `delivered`/`sent`, not `prepare failed`.

## Anti-patterns
- Believing the chat agent's own diagnosis ("通道故障，需要管理员重启") — a
  reboot reloads nothing relevant; the failure is per-send and state lives on
  disk. Verify claims against the send log before restarting anything.
- Patching the `/opt/node*/lib/node_modules` plugin copy — the gateway loads
  the `~/.openclaw/npm/projects/…` copy; patch what `openclaw plugins list`
  reports (sync the other copy only as belt-and-braces).
- Testing right after the owner messaged and declaring it fixed — inside the
  24h window context-less sends succeed anyway; the log's missing-token
  warning, not send success, is what the patch removes.
- Treating `prepare failed` as transient and retrying forever — it is
  deterministic outside the window; bounded retries + dead-letter + in-chat
  surfacing is the correct shape (see notify.py).
