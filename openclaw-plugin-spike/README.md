# A.8 accountId spike — how to check

Goal: confirm `@tencent-weixin/openclaw-weixin` gives the bridge a **stable,
distinct `accountId`** (plus `channel` and a usable `sessionKey`) at
`before_dispatch`. This is the gate before enabling `DEPLOYMENT_MODE=multi_tenant`
(see `../doc/DESIGN_MULTI_USER.md` §A.8).

The probe is **read-only** — it logs and returns `undefined`, so it never claims
a message and your normal bridge keeps working. Use two WeChat accounts logged
into the same OpenClaw gateway.

## Run it

1. **Register the probe** in your OpenClaw config the same way the personal-agent
   bridge is registered (add this plugin dir to the gateway's plugin list /
   `openclaw.json` `plugins.entries`, `enabled: true`, and — like the bridge —
   allow conversation hooks: `hooks.allowConversationAccess: true`). Keep the
   personal-agent bridge enabled too; the probe only observes.
2. **Restart the gateway** and watch its console/stdout (where you already see
   `[personal-agent-bridge]` lines).
3. From **account A**, send two WeChat messages (e.g. `hi` then `/status`).
4. From **account B**, send two messages.
5. Read the lines tagged `[A8-SPIKE] before_dispatch …`:
   ```
   tail -f <gateway.log> | grep A8-SPIKE
   ```

## Pass / fail checklist

Read the `before_dispatch` lines (the `ctx=…` and `event=…` pairs):

- [ ] **`ctx.channel === "weixin"`** is present on weixin turns.
- [ ] **`ctx.accountId` is present and non-empty.**
- [ ] **Stable:** account A's two messages show the **same** `accountId`; so do B's.
- [ ] **Distinct:** A's `accountId` ≠ B's `accountId`.
- [ ] **Body is readable** in `event.body` or `event.content` (note
      `event.cleanedBody` is expected to be **empty** here — that's why the bridge
      moved off `before_agent_reply`).
- [ ] **`ctx.sessionKey`** is present and stable within one conversation.

If **all** boxes tick, the gate is passed: the design's assumptions hold, and you
can wire real `accountId`s with `assistant admin bind-channel <uid> weixin
<accountId>` and flip `DEPLOYMENT_MODE=multi_tenant`.

If `accountId` is **missing** at `before_dispatch`, look at the `_keys` list the
probe dumps (the full field list on `ctx`/`event`) — it will show what the weixin
channel *does* provide (it might live under a different name, or on `event`), and
we adjust `openclaw-plugin/index.js` (`ctx?.accountId`) accordingly before enabling.

## Remove it

Delete the probe from the gateway config and restart. It leaves nothing behind
(no state, no files).
