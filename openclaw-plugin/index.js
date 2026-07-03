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

import { execFile } from "node:child_process";

const ASSISTANT_BIN = process.env.PERSONAL_AGENT_BIN ?? "/rebase/.venv/bin/assistant";
const TIMEOUT_MS = 120_000;

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

export default {
  id: "personal-agent-bridge",
  name: "Personal Agent Bridge",
  description:
    "Answers every inbound message with the personal-agent (`assistant ask`) instead of the gateway's own LLM.",
  configSchema: { type: "object", additionalProperties: false },
  register(api) {
    api.on(
      "before_agent_reply",
      async (event, ctx) => {
        // Heartbeats and slash commands keep OpenClaw's normal behavior.
        if (ctx?.trigger === "heartbeat") return;
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
