---
name: wechat-outbound-context-token
description: Diagnose proactive WeChat `sendMessage ret=-2 errmsg=prepare failed`: restore a disk-persisted context token when a short-lived CLI process missed it, and when the token is present but aged out, retain the push and retry it once after the owner's next inbound WeChat turn; never misdiagnose either branch as a channel outage
trigger: reminder/routine/announce delivery fails with `sendMessage ret=-2 errmsg=prepare failed` while inbound chat replies still work; structured logs show either a missing context token or `restoreContextTokens: restored` immediately before rejection; failures cluster after a quiet owner window and clear after the next inbound
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
- There are two distinct signatures after the restore-on-miss patch lands:
  `contextToken missing` means the short-lived process still did not restore
  disk state; `restoreContextTokens: restored 1` immediately followed by
  `ret=-2 prepare failed` means the token existed but Weixin no longer accepted
  its age.  Reapplying the persistence patch cannot fix the second branch.

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
   still fails.  Keep the routine output/reminder durable through the normal
   bounded attempts.  On the owner's next authenticated inbound Weixin turn,
   queue only an unacknowledged, still-visible failure with the exact
   `ret=-2 … prepare failed (sent 0/…)` signature.  Preserve its attempt count:
   keep the row terminal/visible and preserve its first-surface receipt while a
   private marker asks the next poll for one delivery-only attempt.  This lets a
   follow-up acknowledgment fence a send that has not crossed the poller's
   atomic queued→in-flight boundary; a crash after that boundary re-offers the
   send (duplicate over loss), and another rejection clears the marker.  Never
   rerun the routine task, queue acknowledged
   or expired rows, retry a partial send, switch to email, or restore unbounded
   polling.

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
- Treating `prepare failed` as an ordinary transient and retrying forever — it
  is deterministic outside the window.  Bounded retries + dead-letter + one
  owner-event-gated delivery-only attempt is the correct shape (see
  `delivery.py` and `notify.py`).
- Trusting a request's `channel=weixin` field as proof the push context was
  refreshed.  It only says the CALLER claims to be a Weixin turn: a loopback
  `/chat` smoke test or health check carrying that flag re-arms the retained
  pushes, they fail seconds later against the unchanged token, and the
  rejection clears the marker a genuine inbound needed (2026-09-08/09: 5 then
  9 retries queued and burned while the token had been untouched since
  2026-09-06).  `_rearm_weixin_after_turn` now gates on
  `notify.weixin_context_fresh`, which reads the mtime of
  `~/.openclaw/openclaw-weixin/accounts/<acct>.context-tokens.json` — the
  plugin rewrites it on every real inbound, so it is the only local evidence
  that distinguishes a token Weixin will accept from one it will not.  The
  account is the request's `account_id` (multi-tenant bridge) or, for the
  single-user bridge that sends none, `ANNOUNCE_ACCOUNT` — the account the
  pushes actually ride, so the gate engages on both deployments.  When
  testing this path, drive a real inbound or fake the mtime; do not just set
  the flag.
