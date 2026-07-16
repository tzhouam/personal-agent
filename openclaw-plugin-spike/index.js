/**
 * A.8 spike probe — read-only. Logs what OpenClaw actually exposes at
 * `before_dispatch` (and `message_received`) for each inbound message, so you
 * can confirm the weixin channel supplies a STABLE, DISTINCT `accountId` before
 * enabling multi_tenant (doc/DESIGN_MULTI_USER.md §A.8).
 *
 * It NEVER returns {handled:true} — every hook returns undefined, so it only
 * observes and the message flows on to your real bridge / OpenClaw untouched.
 * Enable it temporarily alongside the personal-agent bridge, send one WeChat
 * from each of two accounts, then read the lines tagged [A8-SPIKE].
 */

function dump(tag, event, ctx) {
  const pick = (o) => {
    if (!o || typeof o !== "object") return o;
    const out = {};
    // the fields the design cares about, plus a full key list to catch anything
    // we don't know the name of yet
    for (const k of ["channel", "accountId", "account_id", "sessionKey",
                     "conversationId", "senderId", "trigger", "body", "content",
                     "cleanedBody", "text"]) {
      if (o[k] !== undefined) out[k] = o[k];
    }
    out._keys = Object.keys(o);
    return out;
  };
  try {
    console.log(`[A8-SPIKE] ${tag} ctx=${JSON.stringify(pick(ctx))}`);
    console.log(`[A8-SPIKE] ${tag} event=${JSON.stringify(pick(event))}`);
  } catch (e) {
    console.log(`[A8-SPIKE] ${tag} <unserializable: ${e?.message}>`);
  }
}

export default {
  id: "a8-spike-probe",
  name: "A.8 accountId spike probe",
  description: "Read-only: logs channel/accountId/sessionKey at before_dispatch.",
  configSchema: { type: "object", additionalProperties: false },
  register(api) {
    api.on("message_received", (event, ctx) => { dump("message_received", event, ctx); });
    // Low priority + always-undefined return = pure observer, claims nothing.
    api.on("before_dispatch", (event, ctx) => { dump("before_dispatch", event, ctx); },
           { priority: 1 });
  },
};
