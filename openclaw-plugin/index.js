/**
 * personal-agent-bridge — OpenClaw plugin.
 *
 * Registers a `before_agent_reply` typed hook (first `{handled: true}` wins,
 * runs before any model call) that pipes the owner's message into the
 * personal-agent CLI and returns its output as the reply. The gateway's own
 * LLM agent never runs for user messages, so there is no persona to drift and
 * no prompt-obedience risk: OpenClaw is transport only, the personal-agent is
 * the single brain.
 *
 * Requires in openclaw.json:
 *   plugins.entries.personal-agent-bridge.enabled: true
 *   plugins.entries.personal-agent-bridge.hooks.allowConversationAccess: true
 *     (non-bundled plugins may not register conversation hooks without it)
 * Do NOT set hooks.timeouts.before_agent_reply — an aborted hook falls
 * through to the gateway's own LLM; the child-process timeout below already
 * bounds the call and keeps the failure inside the bridge.
 */

import { execFile, spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";

const ASSISTANT_BIN = process.env.PERSONAL_AGENT_BIN ?? "/rebase/.venv/bin/assistant";
const TIMEOUT_MS = 120_000;
const PID_FILE = `${homedir()}/.personal-agent/chat_listener.pid`;

function ask(text) {
  return new Promise((resolve) => {
    execFile(
      ASSISTANT_BIN,
      ["ask", text],
      { timeout: TIMEOUT_MS, maxBuffer: 4 * 1024 * 1024 },
      (err, stdout, stderr) => {
        const reply = (stdout ?? "").trim();
        if (!err && reply) return resolve({ ok: true, text: reply });
        const detail = (stderr ?? "").trim().split("\n").pop() || err?.message || "empty reply";
        resolve({ ok: false, error: String(detail).slice(0, 300) });
      },
    );
  });
}

/**
 * Gateway-supervised chat listener (email polling — OpenClaw has no IMAP
 * channel). Started/stopped with the gateway via api.registerService, so the
 * gateway is the single daemon to relaunch after a container restart.
 * Never spawn at module top level: plugin discovery evaluates this entry;
 * services only start() on full gateway startup.
 */
export function chatListenService() {
  let child = null;
  let timer = null;
  let stopped = false;
  let backoffMs = 5_000;

  const killStaleListener = (logger) => {
    // A listener orphaned by a SIGKILL'd gateway still holds the pid lock;
    // take it over so our supervised child doesn't exit forever on startup.
    try {
      const pid = parseInt(readFileSync(PID_FILE, "utf8").trim(), 10);
      if (pid > 1) {
        process.kill(pid, "SIGTERM");
        logger?.info?.(`[personal-agent-bridge] killed stale chat-listen pid ${pid}`);
      }
    } catch {
      // no pid file or already dead
    }
  };

  const spawnOnce = (logger) => {
    const startedAt = Date.now();
    child = spawn(ASSISTANT_BIN, ["chat-listen"], { stdio: ["ignore", "inherit", "inherit"] });
    logger?.info?.(`[personal-agent-bridge] chat-listen started (pid ${child.pid})`);
    child.on("exit", (code, sig) => {
      child = null;
      if (stopped) return;
      backoffMs = Date.now() - startedAt > 5 * 60_000 ? 5_000 : Math.min(backoffMs * 2, 300_000);
      logger?.warn?.(
        `[personal-agent-bridge] chat-listen exited (code=${code} sig=${sig}); respawn in ${backoffMs / 1000}s`,
      );
      timer = setTimeout(() => spawnOnce(logger), backoffMs);
      timer.unref?.();
    });
  };

  return {
    id: "chat-listen-supervisor",
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
        ctx?.logger?.info?.("[personal-agent-bridge] stopping chat-listen");
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
    "Answers every inbound message with the personal-agent (`assistant ask`) instead of the gateway's own LLM.",
  configSchema: { type: "object", additionalProperties: false },
  register(api) {
    api.registerService?.(chatListenService());
    api.on(
      "before_agent_reply",
      async (event, ctx) => {
        // Claim only real user messages. Heartbeats and cron agent-turns
        // (ctx.trigger "heartbeat"/"cron") keep OpenClaw's normal behavior —
        // piping a cron prompt into `assistant ask` would misfire chat actions.
        if (ctx?.trigger && ctx.trigger !== "user") return;
        const body = (event?.cleanedBody ?? "").trim();
        if (!body || body.startsWith("/")) return;

        const result = await ask(body);
        const text = result.ok
          ? result.text
          : `(assistant bridge error: ${result.error} — check ~/.personal-agent and /rebase/personal-agent/.env)`;
        return { handled: true, reply: { text } };
      },
      { priority: 100 },
    );
  },
};
