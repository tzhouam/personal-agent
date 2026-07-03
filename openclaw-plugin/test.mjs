// Hermetic handler + supervisor unit tests, no gateway needed:
//   /opt/node24/bin/node test.mjs
// Builds a stub assistant binary and an isolated HOME, then imports index.js
// (which reads PERSONAL_AGENT_BIN and HOME at module load).
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawn } from "node:child_process";

const work = mkdtempSync(join(tmpdir(), "pab-test-"));
const stub = join(work, "stub-assistant");
writeFileSync(
  stub,
  '#!/bin/sh\nif [ "$1" = "ask" ]; then echo "$@"; exit 0; fi\n' +
    'if [ "$STUB_MODE" = "exit" ]; then exit 1; fi\nexec sleep 30\n',
  { mode: 0o755 },
);
process.env.PERSONAL_AGENT_BIN = stub;
process.env.HOME = work;
mkdirSync(join(work, ".personal-agent"), { recursive: true });

const { default: plugin, chatListenService } = await import("./index.js");

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const alive = (pid) => { try { process.kill(pid, 0); return true; } catch { return false; } };
const log = { info() {}, warn() {}, error() {}, debug() {} };

// ---- reply hook ----
let handler, opts;
const services = [];
plugin.register({
  on: (name, h, o) => { if (name === "before_agent_reply") { handler = h; opts = o; } },
  registerService: (s) => services.push(s),
});
if (!handler || opts.priority !== 100) throw new Error("hook not registered");
if (services.length !== 1 || services[0].id !== "chat-listen-supervisor") throw new Error("service not registered");

let r = await handler({ cleanedBody: " 我有哪些待办？ " }, { trigger: "user" });
if (!(r?.handled === true && r.reply.text.includes("我有哪些待办？"))) throw new Error("user path failed: " + JSON.stringify(r));

for (const [event, ctx, label] of [
  [{ cleanedBody: "x" }, { trigger: "heartbeat" }, "heartbeat"],
  [{ cleanedBody: "run the pipeline" }, { trigger: "cron" }, "cron agent-turn"],
  [{ cleanedBody: "/new" }, { trigger: "user" }, "slash command"],
  [{ cleanedBody: "  " }, { trigger: "user" }, "empty body"],
]) {
  if (await handler(event, ctx) !== undefined) throw new Error(`${label} should fall through`);
}
console.log("reply hook: PASS");

// ---- supervisor ----
const pidFile = join(work, ".personal-agent", "chat_listener.pid");

// 1. start() kills a stale pid-lock holder, then spawns the listener
const stale = spawn("/bin/sleep", ["30"]);
writeFileSync(pidFile, String(stale.pid));
process.env.STUB_MODE = "run";
const svc = chatListenService();
svc.start({ logger: log });
await sleep(2500);
if (alive(stale.pid)) throw new Error("stale listener not killed");
let st = svc._state();
if (!st.child || !alive(st.child.pid)) throw new Error("listener not spawned");
console.log("supervisor start + stale takeover: PASS");

// 2. stop() terminates the child and blocks respawn
const childPid = st.child.pid;
svc.stop({ logger: log });
await sleep(800);
if (alive(childPid)) throw new Error("listener not stopped");
if (svc._state().child) throw new Error("child handle not cleared");
console.log("supervisor stop: PASS");

// 3. quick exits double the backoff (crash-loop damping)
process.env.STUB_MODE = "exit";
const svc2 = chatListenService();
svc2.start({ logger: log });
await sleep(2500); // 1.5s spawn delay + immediate exit
st = svc2._state();
if (st.backoffMs !== 10_000) throw new Error(`backoff not doubled: ${st.backoffMs}`);
svc2.stop({ logger: log });
console.log("supervisor backoff: PASS");

console.log("ALL PASS");
process.exit(0);
