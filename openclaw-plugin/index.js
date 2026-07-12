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
const IMAGE_TIMEOUT_MS = 300_000; // vision pass first (local VLM cold-load)
const ACTION_TIMEOUT_MS = 20_000;
const PID_FILE = `${homedir()}/.personal-agent/chat_listener.pid`;
// Container clock is UTC; pin the owner's zone so "today" in chat replies and
// digest dates match his morning (needs the system tzdata package).
// Also strip inherited ANTHROPIC_* vars: the rebase-agent's shell exports
// (e.g. ANTHROPIC_MODEL=deepseek-…) ride into the gateway's env and would
// override the personal-agent's own .env (pydantic gives process env
// precedence) — its .env must be the single source of LLM config.
const childEnv = () => {
  const env = { ...process.env, TZ: process.env.PERSONAL_AGENT_TZ ?? "Asia/Hong_Kong" };
  for (const k of Object.keys(env)) if (k.startsWith("ANTHROPIC_")) delete env[k];
  return env;
};

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

function askExec(text, imagePaths = []) {
  return new Promise((resolve) => {
    execFile(
      ASSISTANT_BIN,
      ["ask", text, ...imagePaths.flatMap((p) => ["--image", p])],
      { timeout: imagePaths.length ? IMAGE_TIMEOUT_MS : TIMEOUT_MS,
        maxBuffer: 4 * 1024 * 1024, env: childEnv() },
      (err, stdout, stderr) => {
        const reply = (stdout ?? "").trim();
        if (!err && reply) return resolve({ ok: true, text: reply });
        const detail = (stderr ?? "").trim().split("\n").pop() || err?.message || "empty reply";
        resolve({ ok: false, error: String(detail).slice(0, 300) });
      },
    );
  });
}

async function ask(text, session, imagePaths = []) {
  // Image turns pay a vision-model pass first (local VLM cold-load ≈60-90s),
  // so give them a longer leash than plain text.
  const timeoutMs = imagePaths.length ? IMAGE_TIMEOUT_MS : TIMEOUT_MS;
  try {
    const body = { session, text };
    if (imagePaths.length) body.image_paths = imagePaths;
    const data = await daemonPost("/chat", body, timeoutMs);
    if (data?.reply) return { ok: true, text: data.reply };
    return { ok: false, error: "daemon returned an empty reply" };
  } catch {
    return askExec(text, imagePaths); // daemon down — degraded but never dark
  }
}

/**
 * Inbound media cache: the `before_agent_reply` hook only receives the text
 * body, but `message_received` (fired for the same inbound message, earlier
 * in the dispatch pipeline) carries `metadata.mediaPath` — the weixin channel
 * stages an incoming photo as a decrypted local file there. Cache the paths
 * per conversation/sender for a short window and let `before_agent_reply`
 * collect them, so an image (with or without a caption) reaches the daemon
 * as `image_paths`.
 */
const MEDIA_TTL_MS = 3 * 60_000;
const mediaCache = new Map(); // key → {paths: string[], ts: number}

export function cacheInboundMedia(event, ctx) {
  const md = event?.metadata ?? {};
  const paths = (md.mediaPaths ?? (md.mediaPath ? [md.mediaPath] : [])).filter(Boolean);
  const types = md.mediaTypes ?? (md.mediaType ? [md.mediaType] : []);
  const images = paths.filter((_, i) => String(types[i] ?? md.mediaType ?? "").startsWith("image"));
  if (!images.length) return;
  // sessionKey is the one identifier both hooks reliably share (weixin sets
  // no SenderId, and the reply hook has no conversationId — verified against
  // openclaw 2026.6.11). "*" is the single-owner fallback: this gateway only
  // talks to the owner, so an unmatched image can still bind to the very
  // next message.
  const keys = [event?.sessionKey, ctx?.conversationId, event?.senderId, md.senderId, "*"];
  for (const key of keys) {
    if (!key) continue;
    const entry = mediaCache.get(String(key));
    const fresh = entry && Date.now() - entry.ts < MEDIA_TTL_MS ? entry.paths : [];
    mediaCache.set(String(key), { paths: [...fresh, ...images].slice(-3), ts: Date.now() });
  }
  console.log(`[personal-agent-bridge] cached ${images.length} inbound image(s) under: ${keys.filter(Boolean).join(", ")}`);
}

export function takeInboundMedia(keys) {
  for (const key of keys) {
    if (!key) continue;
    const entry = mediaCache.get(String(key));
    if (!entry) continue;
    mediaCache.delete(String(key));
    if (Date.now() - entry.ts >= MEDIA_TTL_MS) continue;
    // The same paths are cached under every key of the inbound message
    // (conversationId AND senderId); consume them everywhere so a later
    // text-only message can't re-attach an already-answered image.
    const taken = new Set(entry.paths);
    for (const [k, e] of mediaCache) {
      const left = e.paths.filter((p) => !taken.has(p));
      if (!left.length) mediaCache.delete(k);
      else if (left.length !== e.paths.length) mediaCache.set(k, { ...e, paths: left });
    }
    return entry.paths;
  }
  return [];
}

/** "/todo add buy GPU due:2026-07-15" → {action, params, timeoutMs?} | {usage} | null
 * (null = not ours, let OpenClaw built-ins have it). */
export function parseSlash(body) {
  const m = body.match(/^\/(todo|read|digest|status|run|plan|search|remind|routine|fin)\b\s*(.*)$/s);
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
  if (family === "routine") {
    if (!rest || rest === "list") return { action: "list_routines", params: {} };
    const cancel = rest.match(/^cancel\s+(\S+)$/);
    if (cancel) return { action: "cancel_routine", params: { id: cancel[1] } };
    return { usage: "usage: /routine [list] | /routine cancel <id> — create by just telling me, e.g. \"every workday at 8:30 …\"" };
  }
  if (family === "remind") {
    if (!rest || rest === "list") return { action: "list_reminders", params: {} };
    const cancel = rest.match(/^cancel\s+(\S+)$/);
    if (cancel) return { action: "cancel_reminder", params: { id: cancel[1] } };
    const set = rest.match(/^(\S+)\s+(.+)$/s);
    if (set) return { action: "set_reminder", params: { when: set[1], message: set[2] } };
    return { usage: "usage: /remind [list] | /remind cancel <id> | /remind <+2h|HH:MM> <message>" };
  }
  if (family === "fin") {
    if (!rest || rest === "sum") return { action: "finance_summary", params: {} };
    const month = rest.match(/^sum\s+(\d{4}-\d{2})$/);
    if (month) return { action: "finance_summary", params: { month: month[1] } };
    if (rest === "list") return { action: "list_transactions", params: {} };
    const listMonth = rest.match(/^list\s+(\d{4}-\d{2})$/);
    if (listMonth) return { action: "list_transactions", params: { month: listMonth[1] } };
    const voided = rest.match(/^void\s+(\S+)$/);
    if (voided) return { action: "void_transaction", params: { id: voided[1] } };
    const logged = rest.match(/^(income|expense)\s+([\d.]+)(?:\s+(\S+))?(?:\s+(.+))?$/s);
    if (logged)
      return { action: "log_transaction",
               params: { kind: logged[1], amount: logged[2],
                         ...(logged[3] ? { category: logged[3] } : {}),
                         ...(logged[4] ? { note: logged[4] } : {}) } };
    return { usage: "usage: /fin [sum [YYYY-MM]] | /fin list [YYYY-MM] | " +
                    "/fin <income|expense> <amount> [category] [note] | /fin void <id>" };
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
  const unrel = rest.match(/^unrelated\s+(\S+)$/);
  if (unrel) return { action: "unrelated_reading", params: { id: unrel[1] } };
  return { usage: "usage: /read [list] | /read done <id> | /read unrelated <id>" };
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
    // Fire-and-forget on every inbound message; harvests staged image paths
    // for the reply hook below (the reply event itself is text-only).
    api.on("message_received", cacheInboundMedia);
    api.on(
      "before_agent_reply",
      async (event, ctx) => {
        // Claim only real user messages. Heartbeats and cron agent-turns
        // (ctx.trigger "heartbeat"/"cron") keep OpenClaw's normal behavior —
        // piping a cron prompt into the chat agent would misfire actions.
        if (ctx?.trigger && ctx.trigger !== "user") return;
        const body = (event?.cleanedBody ?? "").trim();
        const images = takeInboundMedia(
          [ctx?.sessionKey, ctx?.conversationId, ctx?.senderId, event?.senderId, "*"]);
        if (images.length)
          console.log(`[personal-agent-bridge] attaching ${images.length} image(s) to chat turn`);
        if (!body && !images.length) return;

        if (body.startsWith("/")) {
          const parsed = parseSlash(body);
          if (!parsed) return; // not ours — OpenClaw built-ins keep it
          return { handled: true, reply: { text: await handleSlash(parsed) } };
        }

        // weixin provides neither conversationId nor SenderId, so sessionKey is
        // what actually keys per-conversation memory (was falling to "default").
        const session = `oc:${ctx?.conversationId ?? ctx?.senderId ?? event?.senderId ?? ctx?.sessionKey ?? "default"}`;
        const result = await ask(body, session, images);
        const text = result.ok
          ? result.text
          : `(assistant bridge error: ${result.error} — check ~/.personal-agent and /rebase/personal-agent/.env)`;
        return { handled: true, reply: { text } };
      },
      { priority: 100 },
    );
  },
};
