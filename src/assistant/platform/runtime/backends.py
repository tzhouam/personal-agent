"""Bounded model adapters and native CLI runners behind one small contract.

API backends return host tool calls. CLI controllers return the same structured
result using constrained invocation modes; project requests may use native tools.
Pi, Claude and Gemini require explicit opt-in for native writes because their
permission flags are not an operating-system sandbox. Gemini also needs this
opt-in for controller turns because its plan mode cannot reliably disable every
configured native/MCP tool. Codex ignores inherited user configuration while
retaining CODEX_HOME authentication; model/provider routing must be explicit in
BackendConfig.model and command argv (including global ``-c`` overrides).
"""

import asyncio
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time
from typing import Any

import httpx

from assistant.platform.runtime.contracts import (
    Backend, BackendConfig, BackendRequest, Completion, ToolCall,
)

_MAX_OUTPUT = 2 * 1024 * 1024
_MAX_INPUT = 1024 * 1024
_CLI_KINDS = frozenset({"codex", "claude", "gemini", "pi"})


class BackendError(RuntimeError):
    """A provider did not produce an authoritative, usable completion."""


class BackendCancelled(BackendError):
    """The caller cancelled a request before it completed."""


def _check_cancel(request: BackendRequest) -> None:
    """Respect cancellation before starting work or exposing its result."""
    if request.cancel is not None and request.cancel.is_set():
        raise BackendCancelled("backend request cancelled")


def _timeout(config: BackendConfig, request: BackendRequest) -> float:
    """Apply both the route's limit and the individual request's deadline."""
    timeout = min(float(config.timeout), float(request.timeout))
    if not math.isfinite(timeout) or timeout <= 0:
        raise BackendError("backend timeout must be positive")
    return timeout


def _arguments(value: Any) -> dict:
    """Accept the JSON object shape shared by native API tool protocols."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise BackendError("tool arguments are not valid JSON") from exc
    if not isinstance(value, dict):
        raise BackendError("tool arguments must be an object")
    return value


def _tool_call(value: dict, *, anthropic: bool = False) -> ToolCall:
    """Normalize a provider tool record while keeping its correlation ID."""
    if not isinstance(value, dict):
        raise BackendError("provider returned an invalid tool call")
    function = value if anthropic else value.get("function", {})
    if not isinstance(function, dict):
        raise BackendError("provider returned an invalid tool function")
    call_id, name = value.get("id"), function.get("name")
    if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
        raise BackendError("provider returned an invalid tool call")
    arguments = value.get("input", {}) if anthropic else function.get("arguments", {})
    return ToolCall(call_id, name, _arguments(arguments))


def _safe_error(message: Any, config: BackendConfig) -> str:
    """Expose a short diagnostic without credentials, endpoints or raw dumps."""
    text = str(message or "provider failed")
    for secret in (config.api_key,):
        if secret:
            text = text.replace(secret, "[redacted]")
    text = re.sub(r"(?:https?|wss?)://\S+|sk-[A-Za-z0-9_-]+|Bearer\s+\S+",
                  "[redacted]", text, flags=re.IGNORECASE)
    text = re.sub(r"(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password)\s*[=:]\s*[^\s,;]+",
                  "credential=[redacted]", text)
    return re.sub(r"[\x00-\x1f]", " ", text)[:400]


def _endpoint(config: BackendConfig) -> str:
    """Resolve a compatible API base URL without duplicating version paths."""
    suffix = "/messages" if config.kind == "anthropic" else "/chat/completions"
    default = "https://api.anthropic.com/v1" if config.kind == "anthropic" else "https://api.openai.com/v1"
    base = (config.base_url or default).rstrip("/")
    if base.endswith(suffix):
        return base
    if config.kind == "anthropic" and not base.endswith("/v1"):
        base += "/v1"
    return base + suffix


def _anthropic_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """Translate standard assistant/tool history to Anthropic content blocks."""
    system, out = [], []
    for message in messages:
        role = message.get("role")
        if role == "system":
            system.append(str(message.get("content") or ""))
            continue
        content = message.get("content") or ""
        if role == "tool":
            role = "user"
            blocks = [{"type": "tool_result", "tool_use_id": message.get("tool_call_id"),
                       "content": content if isinstance(content, str) else json.dumps(content)}]
        else:
            if role not in ("user", "assistant"):
                raise BackendError("unsupported message role")
            blocks = ([{"type": "text", "text": content}] if isinstance(content, str) and content
                      else list(content) if isinstance(content, list) else [])
            for call in message.get("tool_calls", []):
                normalized = _tool_call(call)
                blocks.append({"type": "tool_use", "id": normalized.id,
                               "name": normalized.name, "input": normalized.arguments})
        if not blocks:
            blocks = [{"type": "text", "text": " "}]
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})
    return "\n\n".join(system), out


class APIBackend:
    """Anthropic or OpenAI-compatible model with host-owned tool execution."""

    capabilities = frozenset({"tools"})

    def __init__(self, config: BackendConfig):
        """Retain a route without inspecting global credential files."""
        self.config, self.kind = config, config.kind

    def _payload(self, request: BackendRequest) -> dict:
        """Build a provider-specific request from normalized chat messages."""
        if not self.config.model:
            raise BackendError("API backend requires a model")
        if self.kind == "anthropic":
            embedded_system, messages = _anthropic_messages(request.messages)
            payload = {"model": self.config.model, "max_tokens": request.max_tokens, "messages": messages}
            system = "\n\n".join(x for x in (request.system, embedded_system) if x)
            if system:
                payload["system"] = system
            if request.tools:
                payload["tools"] = [{"name": t["function"]["name"],
                                     "description": t["function"].get("description", ""),
                                     "input_schema": t["function"].get("parameters", {"type": "object"})}
                                    for t in request.tools]
            return payload
        messages = list(request.messages)
        if request.system:
            messages.insert(0, {"role": "system", "content": request.system})
        payload = {"model": self.config.model, "messages": messages, "max_tokens": request.max_tokens}
        if request.tools:
            payload["tools"] = request.tools
        return payload

    async def _http(self, request: BackendRequest, payload: dict) -> dict:
        """Cancel HTTP IO promptly and bound both elapsed time and response bytes."""
        headers = ({"x-api-key": self.config.api_key, "anthropic-version": "2023-06-01"}
                   if self.kind == "anthropic" else {"Authorization": "Bearer " + self.config.api_key})
        timeout = _timeout(self.config, request)

        async def fetch():
            """Read a bounded JSON response with inherited proxy and CA settings."""
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("POST", _endpoint(self.config), json=payload, headers=headers) as response:
                    if response.status_code >= 400:
                        raise BackendError(f"{self.kind} HTTP {response.status_code}")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > _MAX_OUTPUT:
                            raise BackendError("provider response exceeds output limit")
                    try:
                        result = json.loads(body)
                    except ValueError as exc:
                        raise BackendError("provider returned invalid JSON") from exc
                    if not isinstance(result, dict):
                        raise BackendError("provider response must be an object")
                    return result

        task = asyncio.create_task(fetch())
        deadline = time.monotonic() + timeout
        try:
            while not task.done():
                _check_cancel(request)
                if time.monotonic() >= deadline:
                    raise BackendError("backend request timed out")
                await asyncio.wait({task}, timeout=0.05)
            _check_cancel(request)
            return await task
        except httpx.HTTPError as exc:
            raise BackendError(f"{self.kind} transport failed ({type(exc).__name__})") from exc
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def complete(self, request: BackendRequest) -> Completion:
        """Return a validated terminal model message or a bounded failure."""
        _check_cancel(request)
        if not self.config.api_key:
            raise BackendError(f"{self.kind} API credential is not configured")
        payload = self._payload(request)
        result = asyncio.run(self._http(request, payload))
        if result.get("error"):
            raise BackendError(f"{self.kind} returned a provider error")
        if self.kind == "anthropic":
            if result.get("stop_reason") not in {"end_turn", "tool_use", "max_tokens", "stop_sequence"}:
                raise BackendError("Anthropic response is missing terminal status")
            blocks = result.get("content", [])
            if not isinstance(blocks, list) or any(not isinstance(b, dict) for b in blocks):
                raise BackendError("Anthropic response has invalid content blocks")
            return Completion(
                text="".join(b.get("text", "") for b in blocks if b.get("type") == "text"),
                tool_calls=[_tool_call(b, anthropic=True) for b in blocks if b.get("type") == "tool_use"],
                usage=result.get("usage", {}), finish_reason=result["stop_reason"])
        choices = result.get("choices", [])
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict) or not choices[0].get("finish_reason"):
            raise BackendError("OpenAI-compatible response is missing terminal status")
        choice = choices[0]
        message = choice.get("message", {})
        if not isinstance(message, dict) or not isinstance(message.get("content") or "", str):
            raise BackendError("OpenAI-compatible response has an invalid message")
        if message.get("refusal"):
            raise BackendError("provider refused the request")
        return Completion(text=message.get("content") or "",
                          tool_calls=[_tool_call(c) for c in message.get("tool_calls", [])],
                          usage=result.get("usage", {}), finish_reason=choice["finish_reason"])


def _stop_process(process: subprocess.Popen) -> None:
    """Stop the entire subprocess tree created for this one bounded request."""
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    else:
        if process.poll() is not None:
            return
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False)
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        process.wait(timeout=5)
    finally:
        # The group can still contain a grandchild even after its leader exits.
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _run_process(args: list[str], prompt: str, request: BackendRequest, timeout: float) -> tuple[int, str, str]:
    """Drain both pipes concurrently; cancel, timeout, or output overflow kills the group."""
    encoded = prompt.encode("utf-8")
    if len(encoded) > _MAX_INPUT:
        raise BackendError("backend input exceeds limit")
    flags = {"start_new_session": True} if os.name == "posix" else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    try:
        process = subprocess.Popen(args, cwd=request.cwd, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, **flags)
    except OSError as exc:
        raise BackendError(f"could not start backend ({type(exc).__name__})") from exc
    output, errors = bytearray(), bytearray()
    oversized = threading.Event()
    lock = threading.Lock()

    def drain(pipe, target):
        """Keep stderr from deadlocking a child and bound retained data."""
        try:
            while chunk := pipe.read(8192):
                with lock:
                    remaining = _MAX_OUTPUT - len(output) - len(errors)
                    target.extend(chunk[:max(0, remaining)])
                    if len(chunk) > remaining:
                        oversized.set()
        finally:
            pipe.close()

    def feed():
        """Write large prompts without blocking cancellation in the caller."""
        try:
            process.stdin.write(encoded)
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    readers = [threading.Thread(target=drain, args=(process.stdout, output), daemon=True),
               threading.Thread(target=drain, args=(process.stderr, errors), daemon=True)]
    for reader in readers:
        reader.start()
    writer = threading.Thread(target=feed, daemon=True)
    writer.start()
    deadline = time.monotonic() + timeout
    try:
        while process.poll() is None:
            _check_cancel(request)
            if oversized.is_set():
                raise BackendError("backend output exceeds limit")
            if time.monotonic() >= deadline:
                raise BackendError("backend request timed out")
            try:
                process.wait(timeout=0.05)
            except subprocess.TimeoutExpired:
                pass
        # A child that outlived its CLI leader may retain the pipe handles.
        # All subprocesses belong to this request's process group.
        for reader in readers:
            reader.join(timeout=0.1)
        if any(reader.is_alive() for reader in readers):
            _stop_process(process)
            for reader in readers:
                reader.join(timeout=1)
        _check_cancel(request)
        if oversized.is_set():
            raise BackendError("backend output exceeds limit")
        return process.returncode, output.decode("utf-8", "replace"), errors.decode("utf-8", "replace")
    finally:
        _stop_process(process)
        writer.join(timeout=1)
        for reader in readers:
            reader.join(timeout=1)


def _controller_result(text: str, completion: Completion) -> Completion:
    """Read a CLI controller's strict JSON without executing native tool claims."""
    stripped = text.strip()
    if stripped.startswith("```json") and stripped.endswith("```"):
        stripped = stripped[7:-3].strip()
    try:
        result = json.loads(stripped)
    except ValueError as exc:
        raise BackendError("CLI controller did not return structured JSON") from exc
    if not isinstance(result, dict) or not isinstance(result.get("text", ""), str):
        raise BackendError("CLI controller returned an invalid result")
    calls = result.get("tool_calls", [])
    if not isinstance(calls, list):
        raise BackendError("CLI controller tool_calls must be a list")
    normalized = []
    for call in calls:
        if not isinstance(call, dict):
            raise BackendError("CLI controller returned an invalid tool call")
        normalized.append(_tool_call({"id": call.get("id"), "function": {
            "name": call.get("name"), "arguments": call.get("arguments", {})}}))
    completion.text, completion.tool_calls = result.get("text", ""), normalized
    return completion


class CLIBackend:
    """A native CLI runner with explicit controller and project-execution modes."""

    def __init__(self, config: BackendConfig):
        """Advertise the actual isolation capability of each native runner."""
        self.config, self.kind = config, config.kind
        isolation = "workspace_sandbox" if self.kind == "codex" else "external_sandbox_required"
        self.capabilities = frozenset({"native_agent", "tools", isolation})

    def _args(self, request: BackendRequest) -> list[str]:
        """Use explicit sessions and permission modes, preserving configured auth."""
        controller = bool(request.tools)
        if request.session_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", request.session_id):
            raise BackendError("invalid native session identifier")
        native_write = request.allow_write and not controller
        if self.kind == "gemini" and not self.config.allow_unrestricted:
            raise BackendError("gemini controller and native execution require allow_unrestricted=true and external isolation")
        if native_write and self.kind != "codex" and not self.config.allow_unrestricted:
            raise BackendError(f"{self.kind} native writes require allow_unrestricted=true and external isolation")
        args = list(self.config.command or (self.kind,))
        if self.kind == "codex":
            # Codex 0.163's global clap arguments replace root-level -c
            # values when exec has its own -c arguments. Keep every override
            # in the deepest command scope, before the host's safety settings.
            prefix, overrides, index = [args[0]], [], 1
            safety_keys = {"approval_policy", "sandbox_mode", "sandbox_workspace_write",
                           "permissions", "permission_profile", "web_search"}
            controller_features = {"apps", "plugins", "shell_tool", "unified_exec", "multi_agent",
                                   "code_mode_host", "browser_use", "computer_use", "image_generation"}
            while index < len(args):
                argument = args[index]
                override = None
                if argument in {"-c", "--config"}:
                    if index + 1 >= len(args):
                        raise BackendError("Codex command configuration requires key=value")
                    candidate = args[index + 1]
                    # Preserve a wrapper's own -c program, rather than treating
                    # arbitrary source code as a Codex config override.
                    if re.match(r"^[A-Za-z_][A-Za-z0-9_.-]*\s*=", candidate):
                        override, index = candidate, index + 1
                elif argument.startswith("--config="):
                    override = argument.partition("=")[2]
                if override is not None:
                    key = override.partition("=")[0].strip()
                    protected = key.split(".", 1)[0] in safety_keys
                    feature = key.removeprefix("features.")
                    if protected or (key.startswith("features.") and feature in controller_features
                                     and (controller or feature in {"apps", "plugins"})):
                        raise BackendError(f"Codex command cannot override host safety setting {key}")
                    overrides += ["-c", override]
                else:
                    if argument in {"--dangerously-bypass-approvals-and-sandbox", "--approve-for-me",
                                    "--sandbox", "-s", "--ask-for-approval", "-a"}:
                        raise BackendError("Codex command cannot override host sandbox or approvals")
                    prefix.append(argument)
                index += 1
            args = prefix
            args += ["exec"]
            isolation = ["--ignore-user-config", "--disable", "apps", "--disable", "plugins",
                         "-c", 'web_search="disabled"']
            if controller:
                for feature in ("shell_tool", "unified_exec", "multi_agent", "code_mode_host",
                                "browser_use", "computer_use", "image_generation"):
                    isolation += ["--disable", feature]
            if request.session_id and not controller:
                args += ["resume", *overrides, *isolation, "-c", 'sandbox_mode="' + ("workspace-write" if native_write else "read-only") + '"',
                         "-c", 'approval_policy="never"', "--skip-git-repo-check", request.session_id, "--json", "-"]
                if self.config.model:
                    args[-1:-1] = ["--model", self.config.model]
            else:
                args += [*overrides, *isolation, "--json", "--skip-git-repo-check", "--sandbox",
                         "workspace-write" if native_write else "read-only", "-c", 'approval_policy="never"']
                if controller:
                    args.append("--ephemeral")
                args += ["--cd", str(request.cwd)]
                if self.config.model:
                    args += ["--model", self.config.model]
                args.append("-")
        elif self.kind == "claude":
            args += ["-p", "--output-format", "stream-json", "--verbose", "--permission-mode",
                     "bypassPermissions" if native_write else "plan"]
            if controller:
                args += ["--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
            elif request.session_id:
                args += ["--resume", request.session_id]
            if self.config.model:
                args += ["--model", self.config.model]
        elif self.kind == "gemini":
            args += ["--output-format", "stream-json", "--approval-mode", "yolo" if native_write else "plan", "-p", "-"]
            if request.session_id and not controller:
                args += ["--resume", request.session_id]
            if self.config.model:
                args += ["-m", self.config.model]
        else:
            args += ["--mode", "json"]
            if controller:
                args += ["--no-session", "--no-tools", "--no-mcp", "--no-extensions", "--no-context-files", "--no-skills"]
            elif not native_write:
                args += ["--tools", "read,grep,find,ls", "--no-mcp", "--no-extensions"]
            if request.session_id and not controller:
                args += ["--session-id", request.session_id]
            if self.config.model:
                args += ["--model", self.config.model]
        return args

    def _prompt(self, request: BackendRequest) -> str:
        """Keep prompts on stdin, with host tools described only as structured data."""
        prompt = request.system + "\n\nConversation JSON:\n" + json.dumps(request.messages, ensure_ascii=False)
        if request.tools:
            prompt += ("\n\nYou are the host's structured controller. Do not call native CLI tools. "
                       "Return ONLY JSON with this shape: {\"text\":\"reply\",\"tool_calls\":"
                       "[{\"id\":\"unique-id\",\"name\":\"tool name\",\"arguments\":{}}]}. "
                       "Use an empty tool_calls list for a final reply. Only request tools from this schema:\n"
                       + json.dumps(request.tools, ensure_ascii=False))
        return prompt

    def _parse(self, output: str, exit_code: int) -> Completion:
        """Require explicit terminal success; silence or partial events are failures."""
        completion = Completion()
        terminal = False
        failed = ""
        pi_retrying = False
        for line in output.split("\n"):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind in {"error", "turn.failed"}:
                error = event.get("error", event.get("message", "provider failed"))
                failed = error.get("message", "provider failed") if isinstance(error, dict) else str(error)
            if self.kind == "codex":
                if kind == "thread.started":
                    completion.session_id = event.get("thread_id")
                if kind == "item.completed" and event.get("item", {}).get("type") == "agent_message":
                    completion.text = event["item"].get("text", "")
                if kind == "turn.completed":
                    terminal, completion.usage = True, event.get("usage", {})
            elif self.kind == "claude":
                if kind == "system" and event.get("session_id"):
                    completion.session_id = event["session_id"]
                if kind == "result":
                    if event.get("is_error") or event.get("subtype") not in (None, "success"):
                        failed = event.get("result") or "Claude run failed"
                    else:
                        terminal = True
                        completion.text = event.get("result", "")
                        completion.session_id = event.get("session_id", completion.session_id)
                        completion.usage = event.get("usage", {})
            elif self.kind == "gemini":
                if kind == "init":
                    completion.session_id = event.get("session_id")
                if kind == "message" and event.get("role") == "assistant":
                    completion.text += event.get("content", "")
                if kind == "result":
                    terminal = event.get("status") == "success"
                    if not terminal:
                        failed = "Gemini run did not succeed"
                    completion.usage = event.get("stats", {})
            else:
                if kind == "session":
                    completion.session_id = event.get("id")
                if kind == "message_end" and event.get("message", {}).get("role") == "assistant":
                    message = event["message"]
                    if message.get("stopReason") in ("error", "aborted"):
                        failed = message.get("errorMessage", "Pi run failed")
                    elif pi_retrying:
                        failed, pi_retrying = "", False
                    completion.text = "".join(b.get("text", "") for b in message.get("content", []) if b.get("type") == "text")
                    completion.usage = message.get("usage", {})
                if kind in {"agent_end", "agent_settled"} and not event.get("willRetry"):
                    terminal = True
                elif kind == "agent_end" and event.get("willRetry"):
                    terminal, pi_retrying = False, True
        if exit_code or failed:
            raise BackendError(f"{self.kind} failed: " + _safe_error(failed or f"process exit {exit_code}", self.config))
        if not terminal:
            raise BackendError(f"{self.kind} exited without a successful terminal result")
        return completion

    def complete(self, request: BackendRequest) -> Completion:
        """Run one explicit, bounded CLI turn with truthful failure propagation."""
        _check_cancel(request)
        if not Path(request.cwd).is_dir():
            raise BackendError("backend workspace does not exist")
        args = self._args(request)
        code, output, _stderr = _run_process(args, self._prompt(request), request, _timeout(self.config, request))
        completion = self._parse(output, code)
        return _controller_result(completion.text, completion) if request.tools else completion


def build_backend(config: BackendConfig) -> Backend:
    """Build a supported route; never silently change provider or credentials."""
    aliases = {"openai-compatible": "openai", "anthropic-compatible": "anthropic", "claudecode": "claude"}
    kind = aliases.get(config.kind, config.kind)
    if kind != config.kind:
        from dataclasses import replace
        config = replace(config, kind=kind)
    if kind in {"anthropic", "openai"}:
        return APIBackend(config)
    if kind in _CLI_KINDS:
        return CLIBackend(config)
    raise BackendError(f"unsupported backend kind: {kind}")
