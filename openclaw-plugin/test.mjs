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

const { default: plugin, serveService, parseSlash } = await import("./index.js");

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const alive = (pid) => { try { process.kill(pid, 0); return true; } catch { return false; } };
const log = { info() {}, warn() {}, error() {}, debug() {} };

// ---- registration ----
let handler, opts;
const services = [];
plugin.register({
  on: (name, h, o) => { if (name === "before_agent_reply") { handler = h; opts = o; } },
  registerService: (s) => services.push(s),
});
if (!handler || opts.priority !== 100) throw new Error("hook not registered");
if (services.length !== 1 || services[0].id !== "serve-supervisor") throw new Error("service not registered");

// ---- parseSlash ----
const cases = [
  ["/status", { action: "run_status", params: {} }],
  ["/digest", { action: "trigger_run", params: {} }],
  ["/todo", { action: "list_todos", params: {} }],
  ["/todo list", { action: "list_todos", params: {} }],
  ["/todo done t3", { action: "done_todo", params: { id: "t3" } }],
  ["/todo add buy a GPU due:2026-07-15",
   { action: "add_todo", params: { title: "buy a GPU", source: "wechat", due: "2026-07-15" } }],
  ["/todo add 复查 PR", { action: "add_todo", params: { title: "复查 PR", source: "wechat" } }],
  ["/read", { action: "list_reading", params: {} }],
  ["/read done r2", { action: "done_reading", params: { id: "r2" } }],
  ["/run research", { action: "run_phase", params: { phase: "research" }, timeoutMs: 90_000 }],
  ["/plan book a dinner for 6 on Friday",
   { action: "plan_task", params: { request: "book a dinner for 6 on Friday" }, timeoutMs: 120_000 }],
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

console.log("ALL PASS");
process.exit(0);
