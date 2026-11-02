import { chmod, lstat, mkdir, realpath } from "node:fs/promises";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { randomUUID } from "node:crypto";
import {
  createAgentSession,
  createExtensionRuntime,
  ModelRuntime,
  SessionManager,
  SettingsManager,
} from "@earendil-works/pi-coding-agent";
import {
  AssistantMessageEventStream,
  getCurrentSystemPrompt,
  getCurrentTools,
  InMemoryCredentialStore,
} from "@earendil-works/pi-ai";

const CLI_KINDS = new Set(["codex", "claude", "gemini"]);
const IMAGE_MIMES = new Set(["image/png", "image/jpeg", "image/webp", "image/gif"]);
const THINKING_LEVELS = new Set(["off", "minimal", "low", "medium", "high", "xhigh", "max"]);
const PERSISTENCE_METHODS = [
  "appendMessage", "appendCompaction", "appendCustomEntry", "appendCustomMessageEntry",
  "branchWithSummary", "appendSessionInfo", "appendContextEdit", "appendUsage",
];
const ZERO_USAGE = {
  input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
};

function record(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function redact(value, config) {
  const exact = config.api_key ? String(value).split(config.api_key).join("[redacted credential]") : String(value);
  return exact
    .replace(/\b(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{36,}|sk-[A-Za-z0-9_-]{12,}|xox[baprs]-[A-Za-z0-9-]{10,}|xapp-[A-Za-z0-9-]{10,}|AKIA[A-Z0-9]{16})\b/g, "[redacted credential]")
    .replace(/\b(Bearer\s+)[A-Za-z0-9._~+\/-]{12,}={0,2}/gi, "$1[redacted credential]")
    .replace(/\b((?:api[_-]?key|access[_-]?token|client[_-]?secret|password)["']?\s*[:=]\s*)(["']?)[^\s,"';}]{8,}\2/gi, "$1$2[redacted credential]$2");
}

function redactTree(value, config) {
  if (typeof value === "string") return redact(value, config);
  if (Array.isArray(value)) return value.map((item) => redactTree(item, config));
  if (!record(value)) return value;
  // Image bytes are opaque; changing base64 would corrupt the attachment.
  if (value.type === "image" && typeof value.data === "string") return { ...value };
  return Object.fromEntries(Object.entries(value).map(([key, item]) => [key,
    typeof item === "string" && item && /^(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password|authorization|cookie|secret|token)$/i.test(key)
      ? "[redacted credential]" : redactTree(item, config),
  ]));
}

/** Keep Pi's SessionManager authoritative while sanitizing its public durable writes. */
function protectPersistence(manager, config) {
  for (const name of PERSISTENCE_METHODS) {
    const append = manager[name].bind(manager);
    manager[name] = (...args) => append(...args.map((value) => redactTree(value, config)));
  }
  return manager;
}

function imagesFrom(value) {
  if (value === undefined) return [];
  if (!Array.isArray(value) || value.length > 8) throw new Error("images must contain at most 8 inline attachments");
  let total = 0;
  return value.map((image) => {
    if (!record(image) || Object.keys(image).some((key) => !["type", "data", "mimeType"].includes(key)) ||
      (image.type !== undefined && image.type !== "image") || !IMAGE_MIMES.has(image.mimeType) ||
      typeof image.data !== "string" || !image.data ||
      !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(image.data)) {
      throw new Error("Images require inline base64 data and a supported image MIME type; paths and URLs are unavailable");
    }
    total += Buffer.byteLength(image.data, "base64");
    if (total > 10 * 1024 * 1024) throw new Error("Image attachments exceed the 10 MiB decoded limit");
    return { type: "image", data: image.data, mimeType: image.mimeType };
  });
}

function textContent(content) {
  if (typeof content === "string") return content;
  return Array.isArray(content) ? content.filter((item) => item.type === "text").map((item) => item.text).join("\n") : "";
}

function usage(value = {}) {
  const input = Number(value.input ?? value.input_tokens ?? value.prompt_tokens ?? 0);
  const output = Number(value.output ?? value.output_tokens ?? value.completion_tokens ?? 0);
  return {
    ...ZERO_USAGE, ...value, input, output,
    cacheRead: Number(value.cacheRead ?? 0), cacheWrite: Number(value.cacheWrite ?? 0),
    totalTokens: Number(value.totalTokens ?? value.total_tokens ?? input + output),
    cost: { ...ZERO_USAGE.cost, ...(record(value.cost) ? value.cost : {}) },
  };
}

function functionCall(call) {
  const fn = call.function ?? call;
  let args = fn.arguments ?? {};
  if (typeof args === "string") {
    try { args = JSON.parse(args); } catch { throw new Error("Provider returned malformed tool arguments"); }
  }
  if (typeof fn.name !== "string" || !record(args)) throw new Error("Provider returned invalid tool call");
  return { type: "toolCall", id: call.id ?? randomUUID(), name: fn.name, arguments: args };
}

function assistantMessage(model, result) {
  const calls = (result.tool_calls ?? []).map(functionCall);
  const reason = result.stop_reason ?? result.finish_reason ?? (result.error ? "error" : calls.length ? "toolUse" : "stop");
  const stopReason = ({ tool_calls: "toolUse", max_tokens: "length", end_turn: "stop", completed: "stop" })[reason] ?? reason;
  if (!["stop", "toolUse", "length", "error", "aborted"].includes(stopReason)) throw new Error("Provider returned unknown finish reason");
  return {
    role: "assistant", content: [
      ...(result.text ? [{ type: "text", text: String(result.text) }] : []), ...calls,
    ], api: model.api, provider: model.provider, model: model.id,
    timestamp: Date.now(), stopReason, usage: usage(result.usage),
    ...(result.error ? { errorMessage: String(result.error) } : {}),
  };
}

function terminalMetadata(assistant, failure) {
  const stopReason = assistant?.stopReason ?? (failure ? "error" : "stop");
  const errorCode = stopReason === "length" ? "length" :
    failure === "Maximum agent steps exceeded" ? "step_limit" :
      failure === "Agent request timed out" ? "timeout" :
        failure === "Request canceled" ? "canceled" : failure ? "provider_error" : undefined;
  const status = /^(?:HTTP\s+)?([45]\d{2})\b/.exec(assistant?.errorMessage ?? "");
  return {
    stop_reason: stopReason, ...(errorCode ? { error_code: errorCode } : {}),
    ...(status ? { status_code: Number(status[1]) } : {}),
  };
}

/** Provider RPC carries one completion; Pi retains scheduling, tools, and retries. */
function cliStream(transport, config) {
  return (model, context, options) => {
    const stream = new AssistantMessageEventStream();
    const pending = async () => {
      let message;
      try {
        const messages = context.messages.filter((item) => item.role !== "system").map((item) => {
          if (item.role === "user") return { role: "user", content: textContent(item.content) };
          if (item.role === "toolResult") return {
            role: "tool", tool_call_id: item.toolCallId, name: item.toolName, content: textContent(item.content),
          };
          if (item.role === "assistant") return {
            role: "assistant", content: textContent(item.content),
            ...(item.content.some((part) => part.type === "toolCall") ? {
              tool_calls: item.content.filter((part) => part.type === "toolCall").map((call) => ({
                id: call.id, type: "function", function: { name: call.name, arguments: JSON.stringify(call.arguments) },
              })),
            } : {}),
          };
          throw new Error("Unsupported Pi transcript message in CLI provider");
        });
        const tools = getCurrentTools(context.messages).map((tool) => ({
          type: "function", function: { name: tool.name, description: tool.description, parameters: tool.parameters },
        }));
        const latestUser = context.messages.findLast((item) => item.role === "user");
        const images = Array.isArray(latestUser?.content) ? latestUser.content.filter((item) => item.type === "image") : [];
        const reply = await transport.request({ type: "model_request", id: randomUUID(), request: {
          messages, system: getCurrentSystemPrompt(context.messages), tools, model: model.id, ...(images.length ? { images } : {}),
        } }, options?.signal);
        if (reply.type !== "model_result") throw new Error("Expected model_result response");
        message = assistantMessage(model, redactTree(reply.result ?? reply, config));
      } catch (error) {
        message = assistantMessage(model, {
          stop_reason: options?.signal?.aborted ? "aborted" : "error", error: redact(error.message, config),
        });
      }
      stream.push({ type: "start", partial: message });
      if (["error", "aborted"].includes(message.stopReason)) {
        stream.push({ type: "error", reason: message.stopReason, error: message });
      } else {
        stream.push({ type: "done", reason: message.stopReason, message });
      }
      stream.end(message);
    };
    void pending();
    return stream;
  };
}

async function createRuntime(config, transport, hasImages) {
  const runtime = await ModelRuntime.create({
    credentials: new InMemoryCredentialStore(), modelsPath: null,
    refreshOnCreate: false, allowModelNetwork: false,
  });
  const kind = config.kind ?? "openai";
  const api = config.api ?? (kind === "anthropic" ? "anthropic-messages" : "openai-completions");
  const provider = config.provider ?? "personal-agent";
  if (typeof config.model !== "string" || !config.model) throw new Error("A model ID is required");
  const cli = CLI_KINDS.has(kind);
  const baseUrl = config.base_url ?? (kind === "anthropic" ? "https://api.anthropic.com" : kind === "deepseek" ? "https://api.deepseek.com" : "https://api.openai.com/v1");
  runtime.registerProvider(provider, {
    name: "Personal agent configured provider", api, baseUrl,
    ...(cli ? { streamSimple: cliStream(transport, config), apiKey: "cli-transport" } : {}),
    models: [{
      id: config.model, name: config.model, api, baseUrl,
      reasoning: config.reasoning ?? (config.thinking_level !== undefined && config.thinking_level !== "off"),
      input: config.input ?? (hasImages || config.supports_images ? ["text", "image"] : ["text"]),
      contextWindow: config.context_window ?? 128000, maxTokens: config.max_tokens ?? 8192,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      ...(record(config.compat) ? { compat: config.compat } : {}),
    }],
  });
  if (config.api_key && !cli) await runtime.setRuntimeApiKey(provider, config.api_key);
  const model = runtime.getModel(provider, config.model);
  if (!model) throw new Error("Configured Pi model could not be registered");
  return { runtime, model };
}

/** Disable implicit filesystem, skill, package, extension, and MCP discovery. */
function resources(system) {
  return {
    getExtensions: () => ({ extensions: [], errors: [], runtime: createExtensionRuntime() }),
    getSkills: () => ({ skills: [], diagnostics: [] }),
    getPrompts: () => ({ prompts: [], diagnostics: [] }),
    getThemes: () => ({ themes: [], diagnostics: [] }),
    getAgentsFiles: () => ({ agentsFiles: [] }),
    getSystemPrompt: () => system,
    getSystemPromptSource: () => undefined,
    getAppendSystemPrompt: () => [],
    getAppendSystemPromptSources: () => [],
    extendResources: () => {}, reload: async () => {},
  };
}

function validate(request) {
  if (!record(request) || request.type !== "start") throw new Error("Expected a start request");
  for (const field of ["cwd", "session_dir"]) {
    if (typeof request[field] !== "string" || !request[field]) throw new Error(`Missing ${field}`);
  }
  if (typeof request.prompt !== "string" || (!request.prompt && !request.images?.length)) throw new Error("Missing prompt or image attachment");
  if (!isAbsolute(request.cwd) || !isAbsolute(request.session_dir)) throw new Error("Session and workspace paths must be absolute");
  if (!record(request.model)) throw new Error("Missing model configuration");
  if (request.model.thinking_level !== undefined && !THINKING_LEVELS.has(request.model.thinking_level)) throw new Error("Invalid Pi thinking_level");
  imagesFrom(request.images);
  if (request.model.input !== undefined && (!Array.isArray(request.model.input) || !request.model.input.length || request.model.input.some((item) => !["text", "image"].includes(item)))) throw new Error("model.input must list text and/or image modalities");
  if (request.images?.length && request.model.supports_images === false) throw new Error("Configured model does not support images");
  if (request.images?.length && request.model.input && !request.model.input.includes("image")) throw new Error("Configured model does not support images");
  if (request.max_steps !== undefined && (!Number.isInteger(request.max_steps) || request.max_steps < 1)) throw new Error("max_steps must be a positive integer");
  if (request.timeout !== undefined && (!Number.isFinite(request.timeout) || request.timeout <= 0)) throw new Error("timeout must be positive seconds");
  const names = new Set();
  for (const declaration of request.tools ?? []) {
    const tool = declaration.function ?? declaration;
    if (!/^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(tool.name) || names.has(tool.name)) throw new Error("Invalid or duplicate tool name");
    if (!record(tool.parameters) || tool.parameters.type !== "object") throw new Error("Tool parameters must be an object JSON Schema");
    names.add(tool.name);
  }
}

async function managerFor(request, model) {
  await mkdir(request.session_dir, { recursive: true, mode: 0o700 });
  const sessionDir = await realpath(request.session_dir);
  await chmod(sessionDir, 0o700);
  let manager;
  if (request.session_file) {
    const file = resolve(request.session_file);
    if (dirname(file) !== sessionDir || !(await lstat(file)).isFile() || await realpath(file) !== file) {
      throw new Error("Resume session must be an existing file directly inside session_dir");
    }
    await chmod(file, 0o600);
    manager = protectPersistence(SessionManager.open(file, sessionDir, request.cwd), request.model);
  } else {
    manager = protectPersistence(SessionManager.create(request.cwd, sessionDir), request.model);
    for (const item of request.history ?? []) {
      if (item.role === "user") manager.appendMessage({ role: "user", content: typeof item.content === "string" ? item.content : [
        { type: "text", text: textContent(item.content) }, ...imagesFrom(item.content.filter((part) => part.type === "image")),
      ], timestamp: Date.now() });
      else if (item.role === "assistant") manager.appendMessage(assistantMessage(model, {
        text: textContent(item.content), tool_calls: item.tool_calls, usage: item.usage,
      }));
      else if (item.role === "tool") manager.appendMessage({
        role: "toolResult", toolCallId: item.tool_call_id, toolName: item.name ?? "tool",
        content: [{ type: "text", text: textContent(item.content) }], isError: Boolean(item.is_error), timestamp: Date.now(),
      });
      else throw new Error("Imported history supports user, assistant, and tool roles");
    }
  }
  return manager;
}

/** Execute one durable conversation turn with the actual Pi coding-agent session. */
export async function runSession(request, transport, options = {}) {
  validate(request);
  request = { ...request,
    prompt: redact(request.prompt, request.model), system: redact(request.system ?? "", request.model),
    history: redactTree(request.history, request.model), context_messages: redactTree(request.context_messages, request.model),
    tools: redactTree(request.tools, request.model), images: imagesFrom(request.images),
  };
  const previousUmask = process.umask(0o077);
  let session;
  let manager;
  let timer;
  let paused;
  let failure;
  let turns = 0;
  let settled = false;
  let lastAssistant;
  const runUsage = usage();
  const stop = (reason) => {
    failure ??= reason;
    if (session) void session.abort();
  };
  const canceled = () => stop("Request canceled");
  options.signal?.addEventListener("abort", canceled, { once: true });
  try {
    const configured = options.runtime && options.model ? { runtime: options.runtime, model: options.model } : await createRuntime(request.model, transport, request.images.length > 0);
    manager = await managerFor(request, configured.model);
    if (request.model.input === undefined && request.model.supports_images !== false &&
      manager.buildSessionContext().messages.some((message) => Array.isArray(message.content) && message.content.some((part) => part.type === "image"))) {
      configured.model = { ...configured.model, input: ["text", "image"] };
    }
    const customTools = (request.tools ?? []).map((declaration) => {
      const tool = declaration.function ?? declaration;
      return {
        name: tool.name, label: tool.name, description: tool.description ?? "", parameters: tool.parameters,
        executionMode: "sequential",
        execute: async (id, args, signal) => {
          if (paused || failure || signal?.aborted) throw new Error("Agent run has stopped");
          const reply = await transport.request({ type: "tool_call", id, name: tool.name, arguments: args }, signal);
          if (reply.type === "pause") {
            paused = redactTree({ reason: reply.reason ?? "approval_required", approval_id: reply.approval_id, text: reply.text ?? "Approval required" }, request.model);
            // abort() marks the run synchronously, then waits. Awaiting it here would deadlock tool execution.
            void session.abort();
            return { content: [{ type: "text", text: paused.text }], details: paused, isError: true };
          }
          if (reply.type !== "tool_result") throw new Error("Expected tool_result response");
          const result = redactTree(reply.result, request.model);
          return {
            content: [{ type: "text", text: typeof result === "string" ? result : JSON.stringify(result ?? null) }],
            details: result, isError: Boolean(reply.is_error),
          };
        },
      };
    });
    ({ session } = await createAgentSession({
      cwd: request.cwd, agentDir: join(request.session_dir, ".pi-host"),
      modelRuntime: configured.runtime, model: configured.model,
      thinkingLevel: request.model.thinking_level ?? "off",
      noTools: "all", tools: customTools.map((tool) => tool.name), customTools,
      resourceLoader: resources(request.system ?? ""), sessionManager: manager,
      settingsManager: SettingsManager.inMemory({
        compaction: { enabled: true }, retry: { enabled: true, maxRetries: 2 }, cacheWarming: "off", images: { autoResize: false },
      }),
    }));
    const previousFinish = session.agent.finishTurn;
    const previousTransform = session.agent.transformContext;
    session.agent.transformContext = async (messages, signal) => redactTree(
      previousTransform ? await previousTransform(messages, signal) : messages, request.model,
    );
    session.agent.finishTurn = async (turn, signal) => {
      // Pi can remove a truncated response through a subsequent context edit.
      // Capture its provider result before settlement so callers retain partial text and finish reason.
      lastAssistant = structuredClone(turn.message);
      const decision = await previousFinish?.(turn, signal);
      turns++;
      if (turn.message.stopReason === "length") {
        failure = "Pi model stopped with length (output truncated)";
        return { action: "end" };
      }
      if (turns >= (request.max_steps ?? 24) && turn.message.stopReason === "toolUse") {
        failure = "Maximum agent steps exceeded";
        return { action: "end" };
      }
      return decision;
    };
    session.subscribe((event) => {
      if (event.type === "agent_settled") settled = true;
      if (event.type === "message_end" && event.message.role === "assistant") {
        const current = event.message.usage;
        for (const key of ["input", "output", "cacheRead", "cacheWrite", "totalTokens"]) runUsage[key] += current[key] ?? 0;
        for (const key of Object.keys(runUsage.cost)) runUsage.cost[key] += current.cost?.[key] ?? 0;
      }
    });
    if (options.signal?.aborted) stop("Request canceled");
    timer = setTimeout(() => stop("Agent request timed out"), (request.timeout ?? 300) * 1000);
    const contextIds = new Set(manager.getEntries().filter((entry) => entry.type === "custom_message")
      .map((entry) => entry.details?.source_message_id).filter((id) => typeof id === "string"));
    for (const item of request.context_messages ?? []) {
      const id = item.details?.source_message_id;
      if (typeof id === "string" && contextIds.has(id)) continue;
      await session.sendCustomMessage({ customType: item.custom_type ?? "context", content: String(item.content), display: false, details: item.details }, { triggerTurn: false });
      if (typeof id === "string") contextIds.add(id);
    }
    if (!failure) await session.prompt(request.prompt, { images: request.images, expandPromptTemplates: false });
    await session.waitForIdle();
    const assistant = lastAssistant ?? [...session.messages].reverse().find((message) => message.role === "assistant");
    if (!paused && !failure) {
      if (!settled || !assistant) failure = "Pi session ended without a settled assistant response";
      else if (assistant.stopReason !== "stop") failure = assistant.errorMessage ?? `Pi model stopped with ${assistant.stopReason}`;
    }
    return {
      type: "result", request_id: request.request_id,
      status: paused ? "awaiting_approval" : failure ? "failed" : "done",
      text: redact(paused?.text ?? textContent(assistant?.content) ?? session.getLastAssistantText() ?? "", request.model),
      session_file: manager.getSessionFile(), session_id: manager.getSessionId(), usage: runUsage,
      ...terminalMetadata(assistant, failure),
      ...(paused ?? {}), ...(failure && !paused ? { error: redact(failure, request.model) } : {}),
    };
  } catch (error) {
    // Provider errors may echo headers; redact the only credential crossing this wire.
    const detail = String(error.message ?? error);
    return {
      type: "result", request_id: request.request_id, status: paused ? "awaiting_approval" : "failed",
      text: paused?.text ?? "", session_file: manager?.getSessionFile(), session_id: manager?.getSessionId(),
      stop_reason: "error", error_code: "provider_error",
      ...(paused ?? {}), ...(!paused ? { error: redact(detail, request.model) } : {}),
    };
  } finally {
    clearTimeout(timer);
    options.signal?.removeEventListener("abort", canceled);
    session?.dispose();
    const file = manager?.getSessionFile();
    if (file) await chmod(file, 0o600).catch(() => {});
    process.umask(previousUmask);
  }
}
