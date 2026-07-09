/**
 * personal-agent-bridge — OpenClaw plugin.
 *
 * Registers a `before_agent_reply` typed hook (first `{handled: true}` wins,
 * runs before any model call) that routes the owner's message to the
 * personal-agent and returns its output as the reply. The gateway's own LLM
 * agent never runs for user messages, so there is no persona to drift and no
 * prompt-obedience risk: OpenClaw is transport only, the personal-agent is
 * the single brain.
 *
 * Transport, in order:
 *   1. HTTP to the `assistant serve` daemon (127.0.0.1:SERVE_PORT) — fast,
 *      and /chat carries a per-conversation session id so multi-turn
 *      references work.
 *   2. Fallback: exec `assistant ask` (the pre-daemon path) when the daemon
 *      is unreachable — degraded (no session memory) but never dark.
 *
 * Slash commands /todo /read /digest /status are answered straight from the
 * daemon's /actions endpoints with no LLM call at all; every other
 * "/command" still falls through to OpenClaw's built-ins.
 *
 * Requires in openclaw.json:
 *   plugins.entries.personal-agent-bridge.enabled: true
 *   plugins.entries.personal-agent-bridge.hooks.allowConversationAccess: true
 *     (non-bundled plugins may not register conversation hooks without it)
 * Do NOT set hooks.timeouts.before_agent_reply — an aborted hook falls
 * through to the gateway's own LLM; the timeouts below already bound the
 * calls and keep failures inside the bridge.
 */

import { execFile, spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";

const ASSISTANT_BIN = process.env.PERSONAL_AGENT_BIN ?? "/rebase/.venv/bin/assistant";
const ENV_FILE = process.env.PERSONAL_AGENT_ENV ?? "/rebase/personal-agent/.env";
const TIMEOUT_MS = 120_000;
const ACTION_TIMEOUT_MS = 20_000;
const PID_FILE = `${homedir()}/.personal-agent/chat_listener.pid`;
// Container clock is UTC; pin the owner's zone so "today" in chat replies and
// digest dates match his morning (needs the system tzdata package).
const childEnv = () => ({ ...process.env, TZ: process.env.PERSONAL_AGENT_TZ ?? "Asia/Hong_Kong" });

/** SERVE_PORT/SERVE_TOKEN from the personal-agent .env — re-read per call so
 * a token rotation applies without a gateway restart. */
function daemonConfig() {
  const cfg = { port: 8377, token: "" };
  try {
    for (const line of readFileSync(ENV_FILE, "utf8").split("\n")) {
      const m = line.match(/^\s*(SERVE_PORT|SERVE_TOKEN)\s*=\s*(\S*)\s*$/);
      if (m) cfg[m[1] === "SERVE_PORT" ? "port" : "token"] = m[2];
    }
  } catch {
    // no .env — defaults
  }
  if (process.env.PERSONAL_AGENT_PORT) cfg.port = process.env.PERSONAL_AGENT_PORT;
  cfg.port = parseInt(cfg.port, 10) || 8377;
  return cfg;
}

async function daemonPost(path, body, timeoutMs) {
  const { port, token } = daemonConfig();
  const headers = { "Content-Type": "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  const res = await fetch(`http://127.0.0.1:${port}${path}`, {
    method: "POST",
    headers,
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(timeoutMs),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data?.error ?? `HTTP ${res.status}`);
  return data;
}

function askExec(text) {
  return new Promise((resolve) => {
    execFile(
      ASSISTANT_BIN,
      ["ask", text],
      { timeout: TIMEOUT_MS, maxBuffer: 4 * 1024 * 1024, env: childEnv() },
      (err, stdout, stderr) => {
        const reply = (stdout ?? "").trim();
        if (!err && reply) return resolve({ ok: true, text: reply });
        const detail = (stderr ?? "").trim().split("\n").pop() || err?.message || "empty reply";
        resolve({ ok: false, error: String(detail).slice(0, 300) });
      },
    );
  });
}

async function ask(text, session) {
  try {
    const data = await daemonPost("/chat", { session, text }, TIMEOUT_MS);
    if (data?.reply) return { ok: true, text: data.reply };
    return { ok: false, error: "daemon returned an empty reply" };
  } catch {
    return askExec(text); // daemon down — degraded but never dark
  }
}

/** "/todo add buy GPU due:2026-07-15" → {action, params, timeoutMs?} | {usage} | null
 * (null = not ours, let OpenClaw built-ins have it). */
export function parseSlash(body) {
  const m = body.match(/^\/(todo|read|digest|status|run|plan|search)\b\s*(.*)$/s);
  if (!m) return null;
  const [, family, restRaw] = m;
  const rest = restRaw.trim();
  if (family === "status") return { action: "run_status", params: {} };
  if (family === "digest") return { action: "trigger_run", params: {} };
  if (family === "run") {
    if (!rest) return { usage: "usage: /run <research|website|todos|resume|curate|consolidate|all>" };
    return { action: "run_phase", params: { phase: rest.split(/\s+/)[0] }, timeoutMs: 90_000 };
  }
  if (family === "plan") {
    if (!rest) return { usage: "usage: /plan <the task, one sentence>" };
    return { action: "plan_task", params: { request: rest }, timeoutMs: TIMEOUT_MS };
  }
  if (family === "search") {
    if (!rest) return { usage: "usage: /search <query>" };
    return { action: "web_search", params: { query: rest }, timeoutMs: TIMEOUT_MS };
  }
  if (family === "todo") {
    if (!rest || rest === "list") return { action: "list_todos", params: {} };
    const done = rest.match(/^done\s+(\S+)$/);
    if (done) return { action: "done_todo", params: { id: done[1] } };
    const add = rest.match(/^add\s+(.+)$/s);
    if (add) {
      const params = { title: add[1].trim(), source: "wechat" };
      const due = params.title.match(/\s+due:(\d{4}-\d{2}-\d{2})$/);
      if (due) {
        params.title = params.title.slice(0, due.index).trim();
        params.due = due[1];
      }
      return { action: "add_todo", params };
    }
    return { usage: "usage: /todo [list] | /todo add <title> [due:YYYY-MM-DD] | /todo done <id>" };
  }
  // family === "read"
  if (!rest || rest === "list") return { action: "list_reading", params: {} };
  const done = rest.match(/^done\s+(\S+)$/);
  if (done) return { action: "done_reading", params: { id: done[1] } };
  return { usage: "usage: /read [list] | /read done <id>" };
}

async function handleSlash(parsed) {
  if (parsed.usage) return parsed.usage;
  try {
    const data = await daemonPost(`/actions/${parsed.action}`, parsed.params,
                                  parsed.timeoutMs ?? ACTION_TIMEOUT_MS);
    return data?.result ?? "(empty result)";
  } catch (err) {
    return `(assistant daemon unreachable: ${String(err?.message ?? err).slice(0, 200)} — is \`assistant serve\` running?)`;
  }
}

/**
 * Gateway-supervised `assistant serve` daemon (HTTP endpoints + email chat
 * polling — OpenClaw has no IMAP channel). Started/stopped with the gateway
 * via api.registerService, so the gateway is the single daemon to relaunch
 * after a container restart. `serve` holds the same pid file the old
 * standalone listener used, so stale-pid takeover keeps working across the
 * migration. Never spawn at module top level: plugin discovery evaluates
 * this entry; services only start() on full gateway startup.
 */
export function serveService() {
  let child = null;
  let timer = null;
  let stopped = false;
  let backoffMs = 5_000;

  const killStaleListener = (logger) => {
    // A daemon orphaned by a SIGKILL'd gateway still holds the pid lock;
    // take it over so our supervised child doesn't exit forever on startup.
    try {
      const pid = parseInt(readFileSync(PID_FILE, "utf8").trim(), 10);
      if (pid > 1) {
        process.kill(pid, "SIGTERM");
        logger?.info?.(`[personal-agent-bridge] killed stale serve/listener pid ${pid}`);
      }
    } catch {
      // no pid file or already dead
    }
  };

  const spawnOnce = (logger) => {
    const startedAt = Date.now();
    child = spawn(ASSISTANT_BIN, ["serve"], {
      stdio: ["ignore", "inherit", "inherit"],
      env: childEnv(),
    });
    logger?.info?.(`[personal-agent-bridge] assistant serve started (pid ${child.pid})`);
    child.on("exit", (code, sig) => {
      child = null;
      if (stopped) return;
      backoffMs = Date.now() - startedAt > 5 * 60_000 ? 5_000 : Math.min(backoffMs * 2, 300_000);
      logger?.warn?.(
        `[personal-agent-bridge] assistant serve exited (code=${code} sig=${sig}); respawn in ${backoffMs / 1000}s`,
      );
      timer = setTimeout(() => spawnOnce(logger), backoffMs);
      timer.unref?.();
    });
  };

  return {
    id: "serve-supervisor",
    start(ctx) {
      killStaleListener(ctx?.logger);
      // Give the SIGTERM'd stale process a moment to release the pid lock.
      timer = setTimeout(() => spawnOnce(ctx?.logger), 1_500);
      timer.unref?.();
    },
    stop(ctx) {
      stopped = true;
      if (timer) clearTimeout(timer);
      if (child) {
        ctx?.logger?.info?.("[personal-agent-bridge] stopping assistant serve");
        const doomed = child;
        doomed.kill("SIGTERM");
        setTimeout(() => {
          try { doomed.kill("SIGKILL"); } catch { /* already gone */ }
        }, 5_000).unref?.();
      }
    },
    // test seam
    _state: () => ({ child, stopped, backoffMs }),
  };
}

export default {
  id: "personal-agent-bridge",
  name: "Personal Agent Bridge",
  description:
    "Answers every inbound message with the personal-agent (serve daemon, exec fallback) instead of the gateway's own LLM.",
  configSchema: { type: "object", additionalProperties: false },
  register(api) {
    api.registerService?.(serveService());
    api.on(
      "before_agent_reply",
      async (event, ctx) => {
        // Claim only real user messages. Heartbeats and cron agent-turns
        // (ctx.trigger "heartbeat"/"cron") keep OpenClaw's normal behavior —
        // piping a cron prompt into the chat agent would misfire actions.
        if (ctx?.trigger && ctx.trigger !== "user") return;
        const body = (event?.cleanedBody ?? "").trim();
        if (!body) return;

        if (body.startsWith("/")) {
          const parsed = parseSlash(body);
          if (!parsed) return; // not ours — OpenClaw built-ins keep it
          return { handled: true, reply: { text: await handleSlash(parsed) } };
        }

        const session = `oc:${ctx?.conversationId ?? ctx?.senderId ?? event?.senderId ?? "default"}`;
        const result = await ask(body, session);
        const text = result.ok
          ? result.text
          : `(assistant bridge error: ${result.error} — check ~/.personal-agent and /rebase/personal-agent/.env)`;
        return { handled: true, reply: { text } };
      },
      { priority: 100 },
    );
  },
};
