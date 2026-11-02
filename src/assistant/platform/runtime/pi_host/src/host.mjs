import { createInterface } from "node:readline";
import { runSession } from "./session.mjs";
import { configureHttpProxy } from "./network.mjs";

process.umask(0o077);
configureHttpProxy();
const controller = new AbortController();
const pending = new Map();
let started = false;
let complete = false;
const write = (value) => process.stdout.write(`${JSON.stringify(value)}\n`);
const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });

const transport = {
  request(message, signal) {
    return new Promise((resolve, reject) => {
      if (signal?.aborted) { reject(new Error("Request aborted")); return; }
      const aborted = () => { pending.delete(message.id); reject(new Error("Request aborted")); };
      signal?.addEventListener("abort", aborted, { once: true });
      pending.set(message.id, {
        resolve(reply) { signal?.removeEventListener("abort", aborted); resolve(reply); },
        reject(error) { signal?.removeEventListener("abort", aborted); reject(error); },
      });
      write(message);
    });
  },
};

function protocolError(message) {
  if (!complete) {
    controller.abort();
    if (!started) {
      complete = true;
      write({ type: "result", status: "failed", text: "", error: message });
      lines.close();
    }
  }
}

lines.on("line", (line) => {
  if (complete) return;
  let message;
  try {
    if (Buffer.byteLength(line) > 16 * 1024 * 1024) throw new Error("Input message exceeds size limit");
    message = JSON.parse(line);
    if (!message || typeof message !== "object") throw new Error("Input must be an object");
  } catch {
    protocolError("Invalid JSONL protocol message");
    return;
  }
  if (!started) {
    if (message.type !== "start") { protocolError("First message must be start"); return; }
    started = true;
    void runSession(message, transport, { signal: controller.signal }).then((result) => {
      complete = true;
      write(result);
      lines.close();
    }, () => {
      complete = true;
      write({ type: "result", request_id: message.request_id, status: "failed", text: "", error: "Invalid Pi host request" });
      lines.close();
    });
    return;
  }
  if (message.type === "cancel") { controller.abort(); return; }
  const id = message.id ?? (message.type === "pause" && pending.size === 1 ? pending.keys().next().value : undefined);
  const waiter = pending.get(id);
  if (!waiter || !["tool_result", "model_result", "pause"].includes(message.type)) {
    protocolError("Unexpected response in Pi protocol");
    return;
  }
  pending.delete(id);
  waiter.resolve(message);
});
lines.on("close", () => {
  if (!complete) controller.abort();
  for (const waiter of pending.values()) waiter.reject(new Error("Input stream closed"));
  pending.clear();
});
process.on("SIGTERM", () => controller.abort());
process.on("SIGINT", () => controller.abort());
