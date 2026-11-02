# Personal agent Pi host

This package embeds the actual `@earendil-works/pi-coding-agent` SDK, pinned to
1.4.0. Pi owns provider requests, the agent/tool loop, tool argument validation,
automatic compaction, retries, and durable JSONL sessions. Python supplies channel
adapters, memory, skills, project state, approvals, and confined tool execution.

Development is based on the owner fork <https://github.com/tzhouam/pi>, forked
from <https://github.com/earendil-works/pi> at
`c9f4c7b9fcd0f3e6b5bbc35c7c9b6cda35580eb8` (Pi 1.4.0). The current integration
installs the exact published 1.4.0 SDK packages corresponding to that fork base;
it does not claim to build npm dependencies from the fork. Future Pi core changes
can be developed and built in the owner fork. `package.json` records this source
provenance in `piSource`.

Requires Node.js 22.19 or newer. Install the exact lockfile with
`npm ci --ignore-scripts`. The Python `assistant pi install` command installs this
package in the agent data directory. Run the host with `node src/host.mjs`.
The host initializes the same environment proxy behavior as Pi's CLI on every
supported Node version. It respects `HTTP_PROXY`, `HTTPS_PROXY`, lowercase variants,
and `NO_PROXY`, preserves Node CA trust, and keeps TLS verification enabled. An
explicit `NODE_USE_ENV_PROXY=0` disables this initialization.

One process handles one `start` request and emits one final `result`. All input and
output are JSONL. API keys travel only on stdin and stay in an in-memory credential
store; the host does not load global Pi auth or settings. Explicit session files
must exist directly inside `session_dir`. Session directories and files have modes
0700 and 0600. No built-in tools, extensions, skills, prompt files, project instruction
files, or MCP servers are discovered implicitly.
Prompt, system text, imported history, context messages, and tool results are
sanitized for the configured API key and common credential patterns before Pi sees
them. Pi's public SessionManager write methods also sanitize model-generated
messages, errors, summaries, and metadata before durable writes.

`start` fields:

- `request_id`, absolute `cwd`, absolute `session_dir`, optional `session_file`.
- `system`, `prompt`, OpenAI function-schema `tools`, `max_steps` (default 24),
  `timeout` in seconds (default 300).
- `model`: `kind`, `model`, optional `api`, `provider`, `base_url`, `api_key`,
  `context_window`, `max_tokens`, `reasoning`, `thinking_level`, and `compat`.
  OpenAI-compatible APIs use Pi's `openai-completions` implementation;
  `anthropic` uses `anthropic-messages`. `deepseek` sets its standard API URL.
- `history`: optional standard user/assistant/tool messages, imported through
  SessionManager only when creating a session.
- `images`: at most eight inline `{type:"image",data:<base64>,mimeType:<image MIME>}`
  attachments, up to 10 MiB decoded in total. PNG, JPEG, WebP, and GIF are supported;
  paths and URLs are rejected. Pi receives the attachment through its native prompt
  API. An explicit text-only `model.input` rejects image input.
- `context_messages`: optional `{custom_type, content, details}` records appended
  with Pi's `sendCustomMessage` before the prompt. Approval outcomes belong in
  `content` so the model can see them.
  A string `details.source_message_id` deduplicates a receipt within the request and
  across persisted session restarts. Context without that identifier is retained.

The host emits `tool_call: {id,name,arguments}` and waits for
`tool_result: {id,result,is_error?}` or
`pause: {id,reason,approval_id,text}`. Tools run sequentially; pausing immediately
aborts Pi before another tool can run. An input `cancel` also aborts the session.

For CLI model kinds `codex`, `claude`, and `gemini`, the host registers a real Pi
provider which emits `model_request: {id,request:{messages,system,tools,model}}`.
Python executes one CLI completion and returns
`model_result: {id,result:{text,tool_calls,usage,finish_reason,error?}}`.
Image-bearing CLI completions include native image blocks in `request.images`.
Tool calls accept `{id,name,arguments}` or OpenAI's function wrapper. This RPC
delegates the provider request; Pi still owns the agent loop. CLI support does not
imply CLI execution in verification.

Final `result` includes `request_id`, `status` (`done`, `awaiting_approval`, or
`failed`), `text`, `session_file`, `session_id`, and usage. Pauses include approval
metadata; failures include `error`. The host awaits `prompt()` and idle state,
checking `agent_settled`; `agent_end` alone does not count as completion. Token
truncation, timeout, cancellation, and step exhaustion remain failures.
`stop_reason` preserves the provider finish reason, including `length` with partial
text. `error_code` identifies truncation, step limit, timeout, cancellation, or a
provider error; an unambiguous HTTP error prefix supplies `status_code`.

Local checks use no live provider:

```sh
npm run check
node --test test/session.test.mjs
```
