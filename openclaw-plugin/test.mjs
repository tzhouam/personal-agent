// Handler unit test, no gateway needed:
//   PERSONAL_AGENT_BIN=/bin/echo /opt/node24/bin/node test.mjs
import plugin from "./index.js";

let handler, opts;
plugin.register({ on: (name, h, o) => { if (name === "before_agent_reply") { handler = h; opts = o; } } });
if (!handler || opts.priority !== 100) throw new Error("hook not registered");

let r = await handler({ cleanedBody: " 我有哪些待办？ " }, { trigger: "user" });
if (!(r?.handled === true && r.reply.text.includes("我有哪些待办？"))) throw new Error("user path failed: " + JSON.stringify(r));

for (const [event, ctx, label] of [
  [{ cleanedBody: "x" }, { trigger: "heartbeat" }, "heartbeat"],
  [{ cleanedBody: "/new" }, { trigger: "user" }, "slash command"],
  [{ cleanedBody: "  " }, { trigger: "user" }, "empty body"],
]) {
  if (await handler(event, ctx) !== undefined) throw new Error(`${label} should fall through`);
}
console.log("ALL PASS");
