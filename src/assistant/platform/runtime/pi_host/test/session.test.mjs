import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { connect } from "node:net";
import { AssistantMessageEventStream, InMemoryCredentialStore } from "@earendil-works/pi-ai";
import { ModelRuntime } from "@earendil-works/pi-coding-agent";
import { runSession } from "../src/session.mjs";

const tools = [{ type: "function", function: {
  name: "project_work", description: "Run a project action",
  parameters: { type: "object", properties: { value: { type: "integer" } }, required: ["value"], additionalProperties: false },
} }];

const call = (id, value = 3) => ({ type: "toolCall", id, name: "project_work", arguments: { value } });
const pixel = { type: "image", mimeType: "image/png", data: "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6vCQAAAAASUVORK5CYII=" };
const assistant = (model, content, stopReason = "stop") => ({
  role: "assistant", content, stopReason, model: model.id, provider: model.provider, api: model.api,
  timestamp: Date.now(), usage: {
    input: 10, output: 2, cacheRead: 0, cacheWrite: 0, totalTokens: 12,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
  },
});

async function fixture(t, responses) {
  const base = await mkdtemp(join(tmpdir(), "personal-pi-host-"));
  t.after(() => rm(base, { recursive: true, force: true }));
  const cwd = join(base, "project");
  await mkdir(cwd);
  const contexts = [];
  const runtime = await ModelRuntime.create({ credentials: new InMemoryCredentialStore(), modelsPath: null, refreshOnCreate: false });
  runtime.registerProvider("host-test", {
    api: "openai-completions", apiKey: "fake-key-never-used", baseUrl: "https://invalid.example/v1",
    models: [{ id: "fake", name: "fake", reasoning: false, input: ["text"], contextWindow: 128000, maxTokens: 8192, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } }],
    streamSimple: (model, context, options) => {
      contexts.push(structuredClone(context));
      const stream = new AssistantMessageEventStream();
      const reply = responses.shift();
      assert.ok(reply, "Unexpected additional provider call");
      if (reply === "wait") {
        const abort = () => {
          const message = assistant(model, [], "aborted");
          stream.push({ type: "error", reason: "aborted", error: message }); stream.end(message);
        };
        if (options.signal?.aborted) abort();
        else options.signal?.addEventListener("abort", abort, { once: true });
      } else {
        const message = assistant(model, reply.content, reply.reason);
        if (reply.error) message.errorMessage = reply.error;
        stream.push({ type: "start", partial: message });
        if (["error", "aborted"].includes(message.stopReason)) stream.push({ type: "error", reason: message.stopReason, error: message });
        else stream.push({ type: "done", reason: message.stopReason, message });
        stream.end(message);
      }
      return stream;
    },
  });
  return {
    base, contexts,
    options: { runtime, model: runtime.getModel("host-test", "fake") },
    request: { type: "start", request_id: "test", cwd, session_dir: join(base, "sessions"),
      system: "Explicit system only", prompt: "Complete my project", model: { kind: "openai", model: "fake", api_key: "wire-secret" }, tools, timeout: 3 },
  };
}

test("actual Pi loop validates tools, persists sessions, and resumes exact context", async (t) => {
  const fx = await fixture(t, [
    { content: [call("first", "3")], reason: "toolUse" },
    { content: [{ type: "text", text: "Finished project" }] },
    { content: [{ type: "text", text: "Remembered project" }] },
  ]);
  await writeFile(join(fx.request.cwd, "AGENTS.md"), "UNTRUSTED AUTO DISCOVERY");
  const seen = [];
  const transport = { request: async (message) => { seen.push(message); return { type: "tool_result", id: message.id, result: "project created" }; } };
  const first = await runSession(fx.request, transport, fx.options);
  assert.equal(first.status, "done", first.error);
  assert.equal(first.text, "Finished project");
  assert.deepEqual(seen[0].arguments, { value: 3 });
  assert.equal(first.usage.totalTokens, 24);
  assert.equal((await stat(first.session_file)).mode & 0o777, 0o600);
  assert.equal((await stat(fx.request.session_dir)).mode & 0o777, 0o700);
  const persisted = await readFile(first.session_file, "utf8");
  assert.ok(persisted.includes("project created"));
  assert.ok(!persisted.includes("wire-secret"));
  assert.ok(!JSON.stringify(fx.contexts).includes("UNTRUSTED AUTO DISCOVERY"));
  const provided = fx.contexts[0].messages.filter((item) => item.role === "system").flatMap((item) => item.toolsAdded ?? []);
  assert.deepEqual(provided.map((tool) => tool.name), ["project_work"]);
  const second = await runSession({ ...fx.request, session_file: first.session_file, prompt: "Continue", history: [{ role: "user", content: "SHOULD NOT REIMPORT" }] }, transport, fx.options);
  assert.equal(second.status, "done", second.error);
  assert.equal(second.session_id, first.session_id);
  assert.ok(JSON.stringify(fx.contexts[2]).includes("Finished project"));
  assert.ok(!JSON.stringify(fx.contexts[2]).includes("SHOULD NOT REIMPORT"));
});

test("approval pause aborts the real Pi loop before a second same-batch tool", async (t) => {
  const fx = await fixture(t, [{ content: [call("blocked"), call("must-not-run")], reason: "toolUse" }]);
  const seen = [];
  const result = await runSession(fx.request, { request: async (message) => {
    seen.push(message.id);
    return { type: "pause", id: message.id, reason: "approval_required", approval_id: "approval-1", text: "Please approve project action" };
  } }, fx.options);
  assert.equal(result.status, "awaiting_approval", result.error);
  assert.equal(result.approval_id, "approval-1");
  assert.deepEqual(seen, ["blocked"]);
  assert.equal(fx.contexts.length, 1);
});

test("max_steps ends through Pi finishTurn without an extra model request", async (t) => {
  const fx = await fixture(t, [{ content: [call("one")], reason: "toolUse" }]);
  const result = await runSession({ ...fx.request, max_steps: 1 }, { request: async () => ({ type: "tool_result", result: "ok" }) }, fx.options);
  assert.equal(result.status, "failed");
  assert.match(result.error, /Maximum agent steps/);
  assert.equal(fx.contexts.length, 1);
});

test("timeout aborts provider work and returns a terminal failure", async (t) => {
  const fx = await fixture(t, ["wait"]);
  const started = Date.now();
  const result = await runSession({ ...fx.request, timeout: 0.025 }, {}, fx.options);
  assert.equal(result.status, "failed");
  assert.match(result.error, /timed out/);
  assert.ok(Date.now() - started < 2000);
});

test("truncated calls fail without executing tools or reporting completion", async (t) => {
  const fx = await fixture(t, [{ content: [call("truncated")], reason: "length" }]);
  let executed = 0;
  const result = await runSession({ ...fx.request, max_steps: 1 }, { request: async () => { executed++; } }, fx.options);
  assert.equal(result.status, "failed");
  assert.match(result.error, /length/);
  assert.equal(result.stop_reason, "length", JSON.stringify(result));
  assert.equal(result.error_code, "length");
  assert.equal(executed, 0);
});

test("truncated plain completions preserve partial text and provider finish reason", async (t) => {
  const fx = await fixture(t, [{ content: [{ type: "text", text: '{"partial":' }], reason: "length" }]);
  const result = await runSession(fx.request, {}, fx.options);
  assert.equal(result.status, "failed");
  assert.equal(result.stop_reason, "length");
  assert.equal(result.error_code, "length");
  assert.equal(result.text, '{"partial":');
  assert.equal(fx.contexts.length, 1);
});

test("provider auth errors expose bounded status and redact persisted error text", async (t) => {
  const fx = await fixture(t, [{ content: [], reason: "error", error: "401 Invalid API key wire-secret" }]);
  const result = await runSession(fx.request, {}, fx.options);
  assert.equal(result.status, "failed");
  assert.equal(result.stop_reason, "error");
  assert.equal(result.status_code, 401);
  assert.ok(!result.error.includes("wire-secret"));
  assert.ok(!(await readFile(result.session_file, "utf8")).includes("wire-secret"));
});

test("secrets are sanitized before Pi history, prompt, tool results, and durable writes", async (t) => {
  const secrets = ["wire-secret", "sk-unitsecret1234567890", `ghp_${"A".repeat(36)}`, "xoxb-1234567890-abcdefghijkl", "xapp-1234567890-abcdefghijkl", "AKIA1234567890ABCDEF", "bearer-token-1234567890", "password-value-123456"];
  const sensitive = `${secrets.slice(0, 6).join(" ")} Bearer ${secrets[6]} password=${secrets[7]}`;
  const fx = await fixture(t, [
    { content: [{ type: "text", text: sensitive }, call("redaction")], reason: "toolUse" },
    { content: [{ type: "text", text: sensitive }] },
  ]);
  const result = await runSession({ ...fx.request, prompt: sensitive, system: sensitive,
    history: [{ role: "user", content: sensitive }, { role: "assistant", content: sensitive }],
    context_messages: [{ content: sensitive, details: { nested: { secret: sensitive } } }],
  }, { request: async () => ({ type: "tool_result", result: { nested: [sensitive], api_key: "custom-key-without-known-prefix" } }) }, fx.options);
  assert.equal(result.status, "done", result.error);
  const durable = await readFile(result.session_file, "utf8");
  for (const secret of secrets) {
    assert.ok(!durable.includes(secret), `Persisted secret: ${secret}`);
    assert.ok(!JSON.stringify(fx.contexts).includes(secret), `Model context secret: ${secret}`);
    assert.ok(!result.text.includes(secret), `Returned secret: ${secret}`);
  }
  assert.ok(durable.includes("[redacted credential]"));
  assert.ok(!durable.includes("custom-key-without-known-prefix"));
});

test("inline images reach Pi prompt, remain durable, and require no file discovery", async (t) => {
  const fx = await fixture(t, [{ content: [{ type: "text", text: "Image received" }] }]);
  fx.options.model.input = ["text", "image"];
  const result = await runSession({ ...fx.request, prompt: "", images: [pixel] }, {}, fx.options);
  assert.equal(result.status, "done", result.error);
  const user = fx.contexts[0].messages.findLast((item) => item.role === "user");
  assert.ok(user.content.some((item) => item.type === "image" && item.data === pixel.data));
  assert.ok((await readFile(result.session_file, "utf8")).includes(pixel.data));
});

test("image input rejects paths, URLs, invalid base64, and configured text-only models", async (t) => {
  const fx = await fixture(t, []);
  for (const image of [
    { path: "/etc/passwd", mimeType: "image/png" },
    { ...pixel, url: "https://invalid.example/image.png" },
    { ...pixel, data: "not base64!" },
  ]) await assert.rejects(runSession({ ...fx.request, images: [image] }, {}, fx.options), /inline base64/);
  await assert.rejects(runSession({ ...fx.request, images: [pixel], model: { ...fx.request.model, input: ["text"] } }, {}, fx.options), /does not support images/);
  assert.equal(fx.contexts.length, 0);
});

test("context receipt source IDs deduplicate across Pi session restarts", async (t) => {
  const fx = await fixture(t, [
    { content: [{ type: "text", text: "First" }] }, { content: [{ type: "text", text: "Second" }] },
  ]);
  const receipt = { custom_type: "approval_result", content: "One durable approval receipt", details: { source_message_id: "audit:42" } };
  const first = await runSession({ ...fx.request, context_messages: [receipt, receipt, { content: "Repeated unidentified context" }] }, {}, fx.options);
  const second = await runSession({ ...fx.request, session_file: first.session_file, context_messages: [receipt, { content: "Repeated unidentified context" }] }, {}, fx.options);
  assert.equal(second.status, "done", second.error);
  const entries = (await readFile(first.session_file, "utf8")).trim().split("\n").map(JSON.parse);
  assert.equal(entries.filter((entry) => entry.type === "custom_message" && entry.details?.source_message_id === "audit:42").length, 1);
  assert.equal(entries.filter((entry) => entry.type === "custom_message" && entry.content === "Repeated unidentified context").length, 2);
});

test("OpenAI-compatible API uses Pi's native HTTP streaming provider", async (t) => {
  const fx = await fixture(t, []);
  const requests = [];
  const server = createServer(async (req, res) => {
    let body = "";
    for await (const chunk of req) body += chunk;
    requests.push({ body: JSON.parse(body), authorization: req.headers.authorization });
    res.writeHead(200, { "content-type": "text/event-stream" });
    const first = requests.length === 1;
    const delta = first ? { tool_calls: [{ index: 0, id: "native-tool", type: "function", function: { name: "project_work", arguments: '{"value":9}' } }] } : { content: "Native Pi API complete" };
    res.write(`data: ${JSON.stringify({ id: "test-response", object: "chat.completion.chunk", created: 1, model: "local-test", choices: [{ index: 0, delta, finish_reason: null }] })}\n\n`);
    res.write(`data: ${JSON.stringify({ id: "test-response", object: "chat.completion.chunk", created: 1, model: "local-test", choices: [{ index: 0, delta: {}, finish_reason: first ? "tool_calls" : "stop" }], usage: { prompt_tokens: 8, completion_tokens: 3, total_tokens: 11 } })}\n\n`);
    res.end("data: [DONE]\n\n");
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const port = server.address().port;
  const result = await runSession({ ...fx.request, images: [pixel], model: {
    kind: "openai", model: "local-test", base_url: `http://127.0.0.1:${port}/v1`, api_key: "local-http-key",
  } }, { request: async (message) => {
    assert.deepEqual(message.arguments, { value: 9 });
    return { type: "tool_result", result: "Native action completed" };
  } });
  assert.equal(result.status, "done", result.error);
  assert.equal(result.text, "Native Pi API complete");
  assert.equal(requests.length, 2);
  assert.equal(requests[0].authorization, "Bearer local-http-key");
  assert.deepEqual(requests[0].body.tools.map((tool) => tool.function.name), ["project_work"]);
  const imageUser = requests[0].body.messages.findLast((message) => message.role === "user");
  assert.ok(imageUser.content.some((part) => part.type === "image_url" && part.image_url.url.startsWith("data:image/png;base64,")));
  assert.ok(requests[1].body.messages.some((message) => message.role === "tool"));
  assert.ok(!(await readFile(result.session_file, "utf8")).includes("local-http-key"));
  const resumed = await runSession({ ...fx.request, session_file: result.session_file, prompt: "Recall the attached image", model: {
    kind: "openai", model: "local-test", base_url: `http://127.0.0.1:${port}/v1`, api_key: "local-http-key",
  } }, {});
  assert.equal(resumed.status, "done", resumed.error);
  assert.ok(requests[2].body.messages.some((message) => message.role === "user" && Array.isArray(message.content) && message.content.some((part) => part.type === "image_url")));
});

test("fresh history uses durable SessionManager imports and approval context is visible", async (t) => {
  const fx = await fixture(t, [{ content: [{ type: "text", text: "Imported" }] }]);
  const result = await runSession({ ...fx.request,
    history: [{ role: "user", content: "Old project" }, { role: "assistant", content: "Saved memory" }],
    context_messages: [{ custom_type: "approval_result", content: "Approved action succeeded" }],
  }, {}, fx.options);
  assert.equal(result.status, "done", result.error);
  const saved = await readFile(result.session_file, "utf8");
  assert.ok(saved.includes("Old project"));
  assert.ok(saved.includes("Approved action succeeded"));
  assert.ok(JSON.stringify(fx.contexts[0]).includes("Approved action succeeded"));
});

test("missing or outside resume paths fail instead of silently resetting", async (t) => {
  const fx = await fixture(t, []);
  const missing = await runSession({ ...fx.request, session_file: join(fx.request.session_dir, "missing.jsonl") }, {}, fx.options);
  assert.equal(missing.status, "failed");
  const outside = join(fx.base, "outside.jsonl");
  await writeFile(outside, "{}");
  const scoped = await runSession({ ...fx.request, session_file: outside }, {}, fx.options);
  assert.equal(scoped.status, "failed");
  assert.match(scoped.error, /inside session_dir/);
});

test("CLI completions enter the actual Pi provider and tool loop", async (t) => {
  const fx = await fixture(t, []);
  const messages = [];
  const result = await runSession({ ...fx.request, images: [pixel], model: { kind: "gemini", model: "gemini-test" } }, {
    request: async (message) => {
      messages.push(message);
      if (message.type === "tool_call") return { type: "tool_result", result: "CLI action succeeded" };
      if (messages.length === 1) return { type: "model_result", result: { text: "", tool_calls: [{ id: "cli-tool", name: "project_work", arguments: { value: 7 } }], finish_reason: "tool_calls" } };
      return { type: "model_result", result: { text: "CLI complete", finish_reason: "stop" } };
    },
  });
  assert.equal(result.status, "done", result.error);
  assert.equal(result.text, "CLI complete");
  assert.deepEqual(messages.map((message) => message.type), ["model_request", "tool_call", "model_request"]);
  assert.deepEqual(messages[0].request.images, [pixel]);
  assert.ok(messages[2].request.messages.some((item) => item.role === "tool" && item.content === "CLI action succeeded"));
});

test("stdio malformed initial input emits one safe failure", async () => {
  const child = spawn(process.execPath, [new URL("../src/host.mjs", import.meta.url).pathname], { stdio: ["pipe", "pipe", "pipe"] });
  let output = "";
  child.stdout.on("data", (chunk) => { output += chunk; });
  child.stdin.end("{invalid}\n");
  await new Promise((resolve) => child.on("exit", resolve));
  const result = JSON.parse(output.trim());
  assert.equal(result.status, "failed");
  assert.match(result.error, /Invalid JSONL/);
});

test("stdio CLI protocol completes while caller keeps stdin open", async (t) => {
  const fx = await fixture(t, []);
  const child = spawn(process.execPath, [new URL("../src/host.mjs", import.meta.url).pathname], { stdio: ["pipe", "pipe", "pipe"] });
  t.after(() => child.kill());
  let buffer = "";
  let result;
  child.stdout.on("data", (chunk) => {
    buffer += chunk;
    let newline;
    while ((newline = buffer.indexOf("\n")) >= 0) {
      const message = JSON.parse(buffer.slice(0, newline));
      buffer = buffer.slice(newline + 1);
      if (message.type === "model_request") child.stdin.write(`${JSON.stringify({ type: "model_result", id: message.id, result: { text: "Stdio complete" } })}\n`);
      else if (message.type === "result") result = message;
    }
  });
  const exited = new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("Host did not exit after final result")), 5000);
    child.on("exit", (code) => { clearTimeout(timer); resolve(code); });
  });
  child.stdin.write(`${JSON.stringify({ ...fx.request, model: { kind: "gemini", model: "fake" } })}\n`);
  assert.equal(await exited, 0);
  assert.equal(result.status, "done", result.error);
  assert.equal(result.text, "Stdio complete");
});

test("host native API honors environment CONNECT proxy, NO_PROXY, and explicit disable", async (t) => {
  const fx = await fixture(t, []);
  const origin = createServer(async (req, res) => {
    for await (const _chunk of req) { /* Drain the provider request. */ }
    res.writeHead(200, { "content-type": "text/event-stream" });
    res.write(`data: ${JSON.stringify({ id: "proxy-test", object: "chat.completion.chunk", model: "proxy-model", choices: [{ index: 0, delta: { content: "Proxy behavior verified" }, finish_reason: null }] })}\n\n`);
    res.write(`data: ${JSON.stringify({ id: "proxy-test", object: "chat.completion.chunk", model: "proxy-model", choices: [{ index: 0, delta: {}, finish_reason: "stop" }] })}\n\n`);
    res.end("data: [DONE]\n\n");
  });
  await new Promise((resolve) => origin.listen(0, "127.0.0.1", resolve));
  const originPort = origin.address().port;
  const sockets = new Set();
  let tunnels = 0;
  const proxy = createServer();
  proxy.on("connect", (_req, downstream, head) => {
    tunnels++;
    const upstream = connect(originPort, "127.0.0.1", () => {
      downstream.write("HTTP/1.1 200 Connection Established\r\n\r\n");
      if (head.length) upstream.write(head);
      downstream.pipe(upstream); upstream.pipe(downstream);
    });
    for (const socket of [upstream, downstream]) {
      sockets.add(socket);
      socket.on("close", () => sockets.delete(socket));
      socket.on("error", () => { downstream.destroy(); upstream.destroy(); });
    }
  });
  await new Promise((resolve) => proxy.listen(0, "127.0.0.1", resolve));
  t.after(async () => {
    for (const socket of sockets) socket.destroy();
    await Promise.all([new Promise((resolve) => proxy.close(resolve)), new Promise((resolve) => origin.close(resolve))]);
  });
  const proxyUrl = `http://127.0.0.1:${proxy.address().port}`;
  async function invoke(baseUrl, noProxy, disabled = false) {
    const env = { ...process.env, HTTP_PROXY: proxyUrl, HTTPS_PROXY: proxyUrl, NO_PROXY: noProxy };
    delete env.http_proxy; delete env.https_proxy; delete env.no_proxy; delete env.NODE_USE_ENV_PROXY;
    if (disabled) env.NODE_USE_ENV_PROXY = "0";
    const child = spawn(process.execPath, [new URL("../src/host.mjs", import.meta.url).pathname], { env, stdio: ["pipe", "pipe", "pipe"] });
    t.after(() => child.kill());
    let output = "";
    let errors = "";
    child.stdout.on("data", (chunk) => { output += chunk; });
    child.stderr.on("data", (chunk) => { errors += chunk; });
    const exited = new Promise((resolve, reject) => {
      const timer = setTimeout(() => { child.kill(); reject(new Error("Proxy test timed out")); }, 7000);
      child.on("exit", (code) => { clearTimeout(timer); resolve(code); });
    });
    child.stdin.write(`${JSON.stringify({ ...fx.request, tools: [], model: { kind: "openai", model: "proxy-model", base_url: baseUrl, api_key: "local-proxy-test-key" } })}\n`);
    assert.equal(await exited, 0, errors);
    const result = JSON.parse(output.trim());
    assert.equal(result.status, "done", result.error);
    assert.equal(result.text, "Proxy behavior verified");
  }
  await invoke("http://api.unit.invalid/v1", "");
  assert.equal(tunnels, 1);
  await invoke(`http://127.0.0.1:${originPort}/v1`, "127.0.0.1");
  assert.equal(tunnels, 1, "NO_PROXY must bypass the configured proxy");
  await invoke(`http://127.0.0.1:${originPort}/v1`, "", true);
  assert.equal(tunnels, 1, "NODE_USE_ENV_PROXY=0 must disable proxy initialization");
});
