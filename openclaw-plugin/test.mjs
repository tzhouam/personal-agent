// Hermetic handler + supervisor unit tests, no gateway needed:
//   /opt/node24/bin/node test.mjs
// Builds a stub assistant binary, an isolated HOME, and a stub serve daemon,
// then imports index.js (which reads PERSONAL_AGENT_* and HOME at module load).
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawn } from "node:child_process";

const work = mkdtempSync(join(tmpdir(), "pab-test-"));
const stub = join(work, "stub-assistant");
writeFileSync(
  stub,
  '#!/bin/sh\nif [ "$1" = "ask" ]; then echo "exec:$@"; exit 0; fi\n' +
    'echo "$1" > "$WORK/spawn-arg"\n' +
    'if [ "$STUB_MODE" = "exit" ]; then exit 1; fi\nexec sleep 30\n',
  { mode: 0o755 },
);
process.env.PERSONAL_AGENT_BIN = stub;
process.env.PERSONAL_AGENT_ENV = join(work, "no-such.env"); // defaults, no token
process.env.PERSONAL_AGENT_PORT = "1"; // daemon down until the stub server starts
process.env.HOME = work;
process.env.WORK = work;
mkdirSync(join(work, ".personal-agent"), { recursive: true });

const { default: plugin, serveService, parseSlash, SAFE, encodeBase64Capped, chanBody } =
  await import("./index.js");

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const alive = (pid) => { try { process.kill(pid, 0); return true; } catch { return false; } };
const log = { info() {}, warn() {}, error() {}, debug() {} };

// ---- registration ----
let handler, opts, dispatchHandler;
const services = [];
plugin.register({
  on: (name, h, o) => {
    if (name === "before_agent_reply") { handler = h; opts = o; }
    if (name === "before_dispatch") { dispatchHandler = h; }
  },
  registerService: (s) => services.push(s),
});
if (!handler || opts.priority !== 100) throw new Error("hook not registered");
if (!dispatchHandler) throw new Error("before_dispatch hook not registered");
if (services.length !== 1 || services[0].id !== "serve-supervisor") throw new Error("service not registered");

// before_dispatch is inert in single_user (no env file → single_user default),
// so the live single-user path stays on before_agent_reply.
if (await dispatchHandler({ body: "hi" }, { channel: "weixin", accountId: "A" }) !== undefined)
  throw new Error("before_dispatch must be inert in single_user");

// ---- parseSlash ----
const cases = [
  ["/status", { action: "run_status", params: {} }],
  ["/digest", { action: "trigger_run", params: {} }],
  ["/reboot", { action: "reboot", params: {}, timeoutMs: 15_000 }],
  ["/todo", { action: "list_todos", params: {} }],
  ["/todo list", { action: "list_todos", params: {} }],
  ["/todo done t3", { action: "done_todo", params: { id: "t3" } }],
  ["/todo add buy a GPU due:2026-07-15",
   { action: "add_todo", params: { title: "buy a GPU", source: "wechat", due: "2026-07-15" } }],
  ["/todo add 复查 PR", { action: "add_todo", params: { title: "复查 PR", source: "wechat" } }],
  ["/read", { action: "list_reading", params: {} }],
  ["/read done r2", { action: "done_reading", params: { id: "r2" } }],
  ["/read unrelated r5", { action: "unrelated_reading", params: { id: "r5" } }],
  ["/run research", { action: "run_phase", params: { phase: "research" }, timeoutMs: 90_000 }],
  ["/plan book a dinner for 6 on Friday",
   { action: "plan_task", params: { request: "book a dinner for 6 on Friday" }, timeoutMs: 120_000 }],
  ["/search vllm omni releases",
   { action: "web_search", params: { query: "vllm omni releases" }, timeoutMs: 120_000 }],
  ["/remind", { action: "list_reminders", params: {} }],
  ["/remind cancel m2", { action: "cancel_reminder", params: { id: "m2" } }],
  ["/remind +2h follow up with Gaohan",
   { action: "set_reminder", params: { when: "+2h", message: "follow up with Gaohan" } }],
  ["/routine", { action: "list_routines", params: {} }],
  ["/routine cancel rt2", { action: "cancel_routine", params: { id: "rt2" } }],
  ["/new", null],
  ["/todos", null],
];
for (const [input, want] of cases) {
  const got = parseSlash(input);
  if (JSON.stringify(got) !== JSON.stringify(want))
    throw new Error(`parseSlash(${input}): ${JSON.stringify(got)} != ${JSON.stringify(want)}`);
}
if (!parseSlash("/todo frobnicate")?.usage) throw new Error("bad /todo subcommand should give usage");
if (!parseSlash("/run")?.usage || !parseSlash("/plan")?.usage) throw new Error("bare /run and /plan should give usage");
console.log("parseSlash: PASS");

// ---- daemon down: chat falls back to exec, slash reports unreachable ----
let r = await handler({ cleanedBody: " 我有哪些待办？ " }, { trigger: "user" });
if (!(r?.handled === true && r.reply.text.startsWith("exec:ask") && r.reply.text.includes("我有哪些待办？")))
  throw new Error("exec fallback failed: " + JSON.stringify(r));
r = await handler({ cleanedBody: "/todo" }, { trigger: "user" });
if (!(r?.handled === true && r.reply.text.includes("daemon unreachable")))
  throw new Error("slash daemon-down message missing: " + JSON.stringify(r));
console.log("daemon-down fallbacks: PASS");

// ---- non-user triggers, foreign slashes, empty body fall through ----
for (const [event, ctx, label] of [
  [{ cleanedBody: "x" }, { trigger: "heartbeat" }, "heartbeat"],
  [{ cleanedBody: "run the pipeline" }, { trigger: "cron" }, "cron agent-turn"],
  [{ cleanedBody: "/new" }, { trigger: "user" }, "foreign slash command"],
  [{ cleanedBody: "  " }, { trigger: "user" }, "empty body"],
]) {
  if (await handler(event, ctx) !== undefined) throw new Error(`${label} should fall through`);
}
console.log("fall-through paths: PASS");

// ---- daemon up: HTTP chat with session id, slash → /actions ----
const requests = [];
const daemon = createServer((req, res) => {
  let raw = "";
  req.on("data", (c) => (raw += c));
  req.on("end", () => {
    const body = raw ? JSON.parse(raw) : {};
    requests.push({ path: req.url, body });
    res.setHeader("Content-Type", "application/json");
    if (req.url === "/chat") return res.end(JSON.stringify({ reply: `daemon:${body.text}@${body.session}` }));
    if (req.url.startsWith("/actions/")) return res.end(JSON.stringify({ result: `did ${req.url.slice(9)}` }));
    res.statusCode = 404;
    res.end(JSON.stringify({ error: "no route" }));
  });
});
await new Promise((resolve) => daemon.listen(0, "127.0.0.1", resolve));
process.env.PERSONAL_AGENT_PORT = String(daemon.address().port);

r = await handler({ cleanedBody: "hello there" }, { trigger: "user", conversationId: "conv7" });
if (r?.reply?.text !== "daemon:hello there@oc:conv7") throw new Error("HTTP chat failed: " + JSON.stringify(r));

r = await handler({ cleanedBody: "/todo add review the PR due:2026-07-20" }, { trigger: "user" });
if (r?.reply?.text !== "did add_todo") throw new Error("slash HTTP failed: " + JSON.stringify(r));
const addReq = requests.find((q) => q.path === "/actions/add_todo");
if (!addReq || addReq.body.title !== "review the PR" || addReq.body.due !== "2026-07-20" || addReq.body.source !== "wechat")
  throw new Error("add_todo params wrong: " + JSON.stringify(addReq));

r = await handler({ cleanedBody: "/status" }, { trigger: "user" });
if (r?.reply?.text !== "did run_status") throw new Error("slash /status failed");
daemon.close();
process.env.PERSONAL_AGENT_PORT = "1";
console.log("daemon HTTP path: PASS");

// ---- supervisor ----
const pidFile = join(work, ".personal-agent", "chat_listener.pid");

// 1. start() kills a stale pid-lock holder, then spawns `assistant serve`
const stale = spawn("/bin/sleep", ["30"]);
writeFileSync(pidFile, String(stale.pid));
process.env.STUB_MODE = "run";
const svc = serveService();
svc.start({ logger: log });
await sleep(2500);
if (alive(stale.pid)) throw new Error("stale listener not killed");
let st = svc._state();
if (!st.child || !alive(st.child.pid)) throw new Error("serve not spawned");
const { readFileSync } = await import("node:fs");
if (readFileSync(join(work, "spawn-arg"), "utf8").trim() !== "serve")
  throw new Error("supervisor did not spawn `assistant serve`");
console.log("supervisor start + stale takeover: PASS");

// 2. stop() terminates the child and blocks respawn
const childPid = st.child.pid;
svc.stop({ logger: log });
await sleep(800);
if (alive(childPid)) throw new Error("serve not stopped");
if (svc._state().child) throw new Error("child handle not cleared");
console.log("supervisor stop: PASS");

// 3. quick exits double the backoff (crash-loop damping)
process.env.STUB_MODE = "exit";
const svc2 = serveService();
svc2.start({ logger: log });
await sleep(2500); // 1.5s spawn delay + immediate exit
st = svc2._state();
if (st.backoffMs !== 10_000) throw new Error(`backoff not doubled: ${st.backoffMs}`);
svc2.stop({ logger: log });
console.log("supervisor backoff: PASS");

// ── inbound media cache: message_received metadata → before_agent_reply ──
{
  const { cacheInboundMedia, takeInboundMedia } = await import("./index.js");
  // The real weixin shape (openclaw 2026.6.11): message_received carries
  // sessionKey + metadata.mediaPath, NO senderId; before_agent_reply carries
  // ONLY sessionKey (no conversationId/senderId).
  cacheInboundMedia(
    { sessionKey: "agent:main:weixin-dm", metadata: { mediaPath: "/tmp/pic1.jpg", mediaType: "image/*" } },
    {},
  );
  cacheInboundMedia( // non-image media must be ignored
    { sessionKey: "agent:main:weixin-dm", metadata: { mediaPath: "/tmp/voice.wav", mediaType: "audio/wav" } },
    {},
  );
  let got = takeInboundMedia(["agent:main:weixin-dm", undefined, undefined, undefined, "*"]);
  if (got.length !== 1 || got[0] !== "/tmp/pic1.jpg") throw new Error(`media cache: ${got}`);
  if (takeInboundMedia(["*"]).length !== 0) throw new Error("media not consumed everywhere on take");
  // worst case: NO shared key at all — the "*" single-owner fallback binds it
  cacheInboundMedia(
    { metadata: { mediaPaths: ["/tmp/a.png", "/tmp/b.png"], mediaTypes: ["image/png", "image/png"] } },
    {},
  );
  got = takeInboundMedia([undefined, undefined, undefined, undefined, "*"]);
  if (got.length !== 2) throw new Error(`wildcard fallback failed: ${got}`);
  console.log("inbound media cache: PASS");
}

// ── chanBody + encodeBase64Capped (media caps §A.4) ──
{
  if (JSON.stringify(chanBody("s")) !== JSON.stringify({ session: "s" }))
    throw new Error("chanBody(string) wrong");
  const cb = chanBody({ session: "x", channel: "weixin", account_id: "wxA" });
  if (cb.session !== "x" || cb.channel !== "weixin" || cb.account_id !== "wxA")
    throw new Error("chanBody(object) wrong");
  // a non-image extension is dropped by the MIME allowlist; missing files skipped
  const img = join(work, "shot.png");
  writeFileSync(img, Buffer.from([0x89, 0x50, 0x4e, 0x47]));
  const enc = encodeBase64Capped([img, "/tmp/does-not-exist.png", join(work, "note.txt")]);
  if (enc.length !== 1 || enc[0].media_type !== "image/png" || !enc[0].data)
    throw new Error("encodeBase64Capped wrong: " + JSON.stringify(enc));
  console.log("chanBody + media caps: PASS");
}

// ── multi_tenant: before_dispatch routes weixin by accountId (§A.3) ──
// Shapes below mirror the A.8 spike capture (2026-07-16): the channel is
// EVENT.channel = "openclaw-weixin" (full plugin id, no ctx.channel), ctx has
// accountId + account-global sessionKey + per-peer conversationId.
{
  const envPath = join(work, "no-such.env");
  writeFileSync(envPath, "DEPLOYMENT_MODE=multi_tenant\nSERVE_TOKEN=bridgetok\n");
  process.env.PERSONAL_AGENT_PORT = "1"; // daemon down for the fail-closed checks

  // non-weixin channels pass through untouched
  if (await dispatchHandler({ body: "hi", channel: "telegram" }, { accountId: "A" }) !== undefined)
    throw new Error("non-weixin should pass through");

  // FAIL CLOSED: weixin turn with no accountId → safe reply, never a fall-through
  let d = await dispatchHandler({ body: "hi", channel: "openclaw-weixin" },
                                { channelId: "openclaw-weixin" });
  if (!(d?.handled === true && d.text === SAFE)) throw new Error("no-accountId must fail closed: " + JSON.stringify(d));

  // FAIL CLOSED: daemon down → safe reply, NO exec CLI fallback (no default-user run)
  d = await dispatchHandler({ body: "你好", channel: "openclaw-weixin" },
                            { accountId: "wxA", sessionKey: "agent:main:main" });
  if (!(d?.handled === true && d.text === SAFE)) throw new Error("daemon-down must fail closed: " + JSON.stringify(d));

  // legacy/short shape (ctx.channel === "weixin") still claimed — fail closed here too
  d = await dispatchHandler({ body: "hi" }, { channel: "weixin" });
  if (!(d?.handled === true && d.text === SAFE)) throw new Error("short-name channel must be claimed: " + JSON.stringify(d));

  // daemon up: route with account_id + channel + uid-scoped session
  const reqs = [];
  const dae = createServer((req, res) => {
    let raw = ""; req.on("data", (c) => (raw += c));
    req.on("end", () => {
      const body = raw ? JSON.parse(raw) : {};
      reqs.push({ path: req.url, body });
      res.setHeader("Content-Type", "application/json");
      if (req.url === "/chat") return res.end(JSON.stringify({ reply: `mt:${body.account_id}:${body.text}` }));
      if (req.url.startsWith("/actions/")) return res.end(JSON.stringify({ result: `ok ${body.account_id}` }));
      res.statusCode = 404; res.end(JSON.stringify({ error: "no route" }));
    });
  });
  await new Promise((resolve) => dae.listen(0, "127.0.0.1", resolve));
  process.env.PERSONAL_AGENT_PORT = String(dae.address().port);

  d = await dispatchHandler(
    { body: "你好", content: "你好", channel: "openclaw-weixin", sessionKey: "agent:main:main" },
    { channelId: "openclaw-weixin", accountId: "wxA", sessionKey: "agent:main:main",
      conversationId: "peer-77@im.wechat" });
  if (d?.text !== "mt:wxA:你好") throw new Error("mt chat route failed: " + JSON.stringify(d));
  const chatReq = reqs.find((q) => q.path === "/chat");
  // per-peer memory: session keys on conversationId, not the account-global sessionKey
  if (chatReq.body.account_id !== "wxA" || chatReq.body.channel !== "weixin"
      || chatReq.body.session !== "oc:wxA:peer-77@im.wechat")
    throw new Error("mt chat body wrong: " + JSON.stringify(chatReq.body));

  // a slash command carries the SAME tenant identity (§A.2)
  d = await dispatchHandler(
    { body: "/status", channel: "openclaw-weixin" },
    { channelId: "openclaw-weixin", accountId: "wxA", sessionKey: "agent:main:main" });
  if (d?.text !== "ok wxA") throw new Error("mt slash failed: " + JSON.stringify(d));
  if (reqs.find((q) => q.path === "/actions/run_status")?.body.account_id !== "wxA")
    throw new Error("mt slash missing account_id");

  // in multi_tenant, before_agent_reply is inert (before_dispatch owns routing)
  if (await handler({ cleanedBody: "hi" }, { trigger: "user" }) !== undefined)
    throw new Error("before_agent_reply must be inert in multi_tenant");

  dae.close();
  process.env.PERSONAL_AGENT_PORT = "1";
  writeFileSync(envPath, ""); // restore single_user
  console.log("multi_tenant before_dispatch routing: PASS");
}

console.log("ALL PASS");
process.exit(0);

// ── self-echo guard (A.8 live finding #2) ──
{
  const { rememberReply, isEchoOfOwnReply } = await import("./index.js");
  rememberReply("wxA", "这是机器人刚发出的回复 ✔ done");
  if (!isEchoOfOwnReply("wxA", "这是机器人刚发出的回复 ✔ done"))
    throw new Error("echo of own reply must be detected");
  if (!isEchoOfOwnReply("wxA", "  这是机器人刚发出的回复 ✔ done  "))
    throw new Error("echo detection must be whitespace-tolerant");
  if (isEchoOfOwnReply("wxB", "这是机器人刚发出的回复 ✔ done"))
    throw new Error("echo memory must be per-account");
  if (isEchoOfOwnReply("wxA", "一条正常的新消息"))
    throw new Error("normal messages must not be swallowed");
  if (isEchoOfOwnReply("wxA", ""))
    throw new Error("empty body is not an echo");

  // end-to-end: the mt handler consumes an echo silently (claimed, no text)
  const envPath = join(work, "no-such.env");
  writeFileSync(envPath, "DEPLOYMENT_MODE=multi_tenant\nSERVE_TOKEN=bridgetok\n");
  rememberReply("wxE", "刚发出的长回复内容");
  const e = await dispatchHandler(
    { body: "刚发出的长回复内容", channel: "openclaw-weixin" },
    { channelId: "openclaw-weixin", accountId: "wxE", sessionKey: "agent:main:main" });
  if (!(e?.handled === true && e.text === undefined))
    throw new Error("echo must be consumed with no reply: " + JSON.stringify(e));
  writeFileSync(envPath, "");
  console.log("self-echo guard: PASS");
}

// ── failure-text selection + reply_to passthrough (late-reply routing) ──
{
  const { failureText, STILL_WORKING } = await import("./index.js");
  if (failureText({ name: "TimeoutError" }) !== STILL_WORKING)
    throw new Error("timeout must read as still-working");
  if (failureText({ name: "AbortError" }) !== STILL_WORKING)
    throw new Error("abort must read as still-working");
  if (failureText({ name: "TypeError", message: "fetch failed" }) !== SAFE)
    throw new Error("connection failure must read as unavailable");
  if (failureText(undefined) !== SAFE) throw new Error("unknown error → SAFE");

  // reply_to rides in the /chat body so the daemon can late-deliver there
  const envPath = join(work, "no-such.env");
  writeFileSync(envPath, "DEPLOYMENT_MODE=multi_tenant\nSERVE_TOKEN=bridgetok\n");
  const reqs = [];
  const dae = createServer((req, res) => {
    let raw = ""; req.on("data", (c) => (raw += c));
    req.on("end", () => {
      reqs.push(JSON.parse(raw));
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ reply: "ok" }));
    });
  });
  await new Promise((resolve) => dae.listen(0, "127.0.0.1", resolve));
  process.env.PERSONAL_AGENT_PORT = String(dae.address().port);
  await dispatchHandler(
    { body: "hello", channel: "openclaw-weixin" },
    { channelId: "openclaw-weixin", accountId: "wxR", sessionKey: "agent:main:main",
      conversationId: "peer-42@im.wechat" });
  if (reqs[0]?.reply_to !== "peer-42@im.wechat")
    throw new Error("reply_to missing from chat body: " + JSON.stringify(reqs[0]));
  dae.close();
  process.env.PERSONAL_AGENT_PORT = "1";
  writeFileSync(envPath, "");
  console.log("failure text + reply_to routing: PASS");
}
