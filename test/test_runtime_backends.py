"""Provider contracts use fake transports/processes; live checks are explicit CLI work."""

import asyncio
import json
import os
from pathlib import Path
import sys
import threading
import time

import httpx
import pytest

from assistant.platform.runtime import backends
from assistant.platform.runtime.backends import BackendCancelled, BackendError, build_backend
from assistant.platform.runtime.contracts import BackendConfig, BackendRequest, ToolCall

TOOLS = [{"type": "function", "function": {"name": "read_project", "description": "Read a project",
          "parameters": {"type": "object", "properties": {"name": {"type": "string"}}}}}]


def route_http(monkeypatch, handler):
    """Replace the transport while exercising the real request/response adapter."""
    real = httpx.AsyncClient
    monkeypatch.setattr(backends.httpx, "AsyncClient",
                        lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))


def fake_cli(tmp_path, kind, events, exit_code=0):
    """Emit protocol records from a real child and retain invocation evidence."""
    script = tmp_path / f"{kind}_runner.py"
    script.write_text(
        "import sys,json\nfrom pathlib import Path\n"
        f"Path({str(tmp_path / 'argv.json')!r}).write_text(json.dumps(sys.argv[1:]))\n"
        f"Path({str(tmp_path / 'prompt.txt')!r}).write_text(sys.stdin.read())\n"
        + "\n".join(f"print({json.dumps(e)!r}, flush=True)" for e in events)
        + f"\nsys.exit({exit_code})\n")
    return BackendConfig(kind=kind, command=(sys.executable, str(script)), timeout=5,
                         allow_unrestricted=(kind == "gemini"))


def protocol(kind, text):
    """Return a minimal authoritative success stream for each native CLI."""
    if kind == "codex":
        return [{"type": "thread.started", "thread_id": "thread-a"},
                {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
                {"type": "turn.completed", "usage": {"input_tokens": 12}}]
    if kind == "claude":
        return [{"type": "system", "session_id": "thread-a"},
                {"type": "result", "subtype": "success", "result": text, "usage": {"input_tokens": 12}}]
    if kind == "gemini":
        return [{"type": "init", "session_id": "thread-a"},
                {"type": "message", "role": "assistant", "content": text},
                {"type": "result", "status": "success", "stats": {"input_tokens": 12}}]
    return [{"type": "session", "id": "thread-a"},
            {"type": "message_end", "message": {"role": "assistant", "stopReason": "stop",
             "content": [{"type": "text", "text": text}], "usage": {"input_tokens": 12}}},
            {"type": "agent_end"}]


def test_openai_tool_history_roundtrip(monkeypatch, tmp_path):
    """Standard tool-call IDs and results survive an OpenAI-compatible request."""
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        assert request.url == "https://compatible.test/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer fake-key"
        return httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls", "message": {
            "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {
                "name": "read_project", "arguments": '{"name":"alpha"}'}}]}}],
            "usage": {"prompt_tokens": 10}})

    route_http(monkeypatch, handler)
    messages = [{"role": "user", "content": "Find alpha"}, {"role": "tool", "tool_call_id": "old", "content": "old result"}]
    backend = build_backend(BackendConfig(kind="openai", model="test-model", api_key="fake-key", base_url="https://compatible.test/v1"))
    result = backend.complete(BackendRequest(messages=messages, system="controller", tools=TOOLS, cwd=tmp_path))
    assert result.tool_calls == [ToolCall("c1", "read_project", {"name": "alpha"})]
    assert result.usage == {"prompt_tokens": 10}
    assert seen["messages"][0] == {"role": "system", "content": "controller"}
    assert seen["messages"][-1]["tool_call_id"] == "old"
    assert seen["tools"] == TOOLS


def test_anthropic_translates_tool_history(monkeypatch, tmp_path):
    """Anthropic receives tool_use/tool_result blocks, not OpenAI-only fields."""
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        assert request.url == "https://compatible.test/anthropic/v1/messages"
        assert request.headers["x-api-key"] == "fake-key"
        return httpx.Response(200, json={"stop_reason": "tool_use", "content": [
            {"type": "text", "text": "Looking"},
            {"type": "tool_use", "id": "c2", "name": "read_project", "input": {"name": "beta"}}],
            "usage": {"input_tokens": 7}})

    route_http(monkeypatch, handler)
    messages = [{"role": "system", "content": "memory"}, {"role": "user", "content": "go"},
                {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "function": {
                    "name": "read_project", "arguments": '{"name":"alpha"}'}}]},
                {"role": "tool", "tool_call_id": "c1", "content": "alpha result"}]
    result = build_backend(BackendConfig(kind="anthropic", model="test", api_key="fake-key",
                           base_url="https://compatible.test/anthropic")).complete(
        BackendRequest(messages=messages, system="controller", tools=TOOLS, cwd=tmp_path))
    assert seen["system"] == "controller\n\nmemory"
    assert seen["messages"][1]["content"] == [{"type": "tool_use", "id": "c1", "name": "read_project", "input": {"name": "alpha"}}]
    assert seen["messages"][2]["content"][0]["tool_use_id"] == "c1"
    assert seen["tools"][0]["input_schema"] == TOOLS[0]["function"]["parameters"]
    assert result.text == "Looking" and result.tool_calls[0].id == "c2"


@pytest.mark.parametrize("reply", [{}, {"choices": [{"message": {"content": "partial"}}]},
    {"choices": [{"finish_reason": "stop", "message": {"refusal": "no"}}]}])
def test_api_rejects_missing_terminal_or_refusal(monkeypatch, tmp_path, reply):
    """Partial protocol output must never become a successful completion."""
    route_http(monkeypatch, lambda req: httpx.Response(200, json=reply))
    backend = build_backend(BackendConfig(kind="openai", model="test", api_key="fake"))
    with pytest.raises(BackendError):
        backend.complete(BackendRequest(messages=[], cwd=tmp_path))


def test_api_error_does_not_disclose_response_or_key(monkeypatch, tmp_path):
    """Provider error bodies can echo credentials and must stay out of exceptions."""
    route_http(monkeypatch, lambda req: httpx.Response(401, text="secret-test-token"))
    backend = build_backend(BackendConfig(kind="openai", model="test", api_key="secret-test-token"))
    with pytest.raises(BackendError, match="HTTP 401") as caught:
        backend.complete(BackendRequest(messages=[], cwd=tmp_path))
    assert "secret-test-token" not in str(caught.value)


def test_api_cancellation_interrupts_inflight_io(monkeypatch, tmp_path):
    """A cancellation event cancels the pending asynchronous HTTP task promptly."""
    started, cancelled = threading.Event(), threading.Event()

    async def handler(request):
        started.set()
        await asyncio.sleep(10)
        return httpx.Response(200, json={})

    route_http(monkeypatch, handler)
    trigger = threading.Thread(target=lambda: (started.wait(2), cancelled.set()))
    trigger.start()
    began = time.monotonic()
    with pytest.raises(BackendCancelled):
        build_backend(BackendConfig(kind="openai", model="test", api_key="fake")).complete(
            BackendRequest(messages=[], cwd=tmp_path, cancel=cancelled))
    trigger.join()
    assert time.monotonic() - began < 2


@pytest.mark.parametrize("kind", ["codex", "claude", "gemini", "pi"])
def test_cli_structured_controller_normalizes_tools(tmp_path, kind):
    """Every CLI can act as a structured controller without shell-expanded prompts."""
    payload = json.dumps({"text": "checking", "tool_calls": [{"id": "c1", "name": "read_project", "arguments": {"name": "alpha"}}]})
    backend = build_backend(fake_cli(tmp_path, kind, protocol(kind, payload)))
    result = backend.complete(BackendRequest(messages=[{"role": "user", "content": "literal $(whoami)\nsecond line"}],
                                            tools=TOOLS, cwd=tmp_path))
    assert result.tool_calls == [ToolCall("c1", "read_project", {"name": "alpha"})]
    assert result.text == "checking" and result.session_id == "thread-a"
    assert "literal $(whoami)" in (tmp_path / "prompt.txt").read_text()
    args = json.loads((tmp_path / "argv.json").read_text())
    assert not any("literal" in arg for arg in args)
    assert "--dangerously-bypass-approvals-and-sandbox" not in args
    assert "bypassPermissions" not in args


def test_codex_resume_uses_explicit_thread_and_supported_flags(tmp_path):
    """Resume has no --sandbox/--cd flags and must not select another thread."""
    backend = build_backend(fake_cli(tmp_path, "codex", protocol("codex", "done")))
    backend.complete(BackendRequest(messages=[], cwd=tmp_path, session_id="exact-thread", allow_write=True))
    args = json.loads((tmp_path / "argv.json").read_text())
    assert args[:2] == ["exec", "resume"]
    assert "exact-thread" in args and '--last' not in args
    assert '--sandbox' not in args and '--cd' not in args
    assert 'sandbox_mode="workspace-write"' in args
    assert '--ignore-user-config' in args
    assert args[args.index('--disable') + 1] == 'apps'


def test_codex_controller_disables_native_tools_and_inherited_connectors(tmp_path):
    """Host approvals cannot be bypassed using a native connector or shell tool."""
    payload = json.dumps({"text": "ready", "tool_calls": []})
    backend = build_backend(fake_cli(tmp_path, "codex", protocol("codex", payload)))
    backend.complete(BackendRequest(messages=[], tools=TOOLS, cwd=tmp_path))
    args = json.loads((tmp_path / "argv.json").read_text())
    disabled = {args[i + 1] for i, arg in enumerate(args) if arg == '--disable'}
    assert disabled >= {'shell_tool', 'unified_exec', 'multi_agent', 'apps', 'plugins',
                        'code_mode_host', 'browser_use', 'computer_use', 'image_generation'}
    assert '--ignore-user-config' in args and 'web_search="disabled"' in args


@pytest.mark.parametrize("session", [None, "exact-session"])
def test_codex_custom_provider_config_is_kept_in_exec_scope(tmp_path, session):
    """Codex must not drop provider routing when host -c flags are appended."""
    from dataclasses import replace
    base = fake_cli(tmp_path, 'codex', protocol('codex', 'done'))
    provider = 'model_provider="deepseek"'
    environment = 'model_providers.deepseek.env_key="DEEPSEEK_API_KEY"'
    catalog = 'model_catalog_json="/tmp/nonsecret-model-catalog.json"'
    config = replace(base, command=base.command + ('-c', provider, '--config', environment,
                                                   '--config=' + catalog))
    build_backend(config).complete(BackendRequest(messages=[], cwd=tmp_path, session_id=session))
    args = json.loads((tmp_path / 'argv.json').read_text())
    scope = args.index('resume') + 1 if session else args.index('exec') + 1
    assert all(value in args[scope:] for value in (provider, environment, catalog))
    assert not any(value in args[:scope] for value in (provider, environment, catalog))
    assert 'approval_policy="never"' in args[scope:]


@pytest.mark.parametrize("override", ['sandbox_mode="danger-full-access"',
    'approval_policy="on-request"', 'web_search="live"', 'features.apps=true'])
def test_codex_custom_command_cannot_replace_host_safety_policy(tmp_path, override):
    """Provider routing is configurable; host permission policy remains fixed."""
    from dataclasses import replace
    base = fake_cli(tmp_path, 'codex', protocol('codex', 'done'))
    config = replace(base, command=base.command + ('-c', override))
    with pytest.raises(BackendError, match='host safety'):
        build_backend(config).complete(BackendRequest(messages=[], tools=TOOLS, cwd=tmp_path))


def test_gemini_controller_requires_explicit_unrestricted_opt_in(tmp_path):
    """Gemini plan mode is not a guarantee that native connector tools are off."""
    backend = build_backend(BackendConfig(kind='gemini'))
    with pytest.raises(BackendError, match='allow_unrestricted'):
        backend.complete(BackendRequest(messages=[], tools=TOOLS, cwd=tmp_path))


def test_cli_session_identifier_cannot_inject_flags(tmp_path):
    """A resume identifier occupies an argv positional slot, not a CLI option."""
    backend = build_backend(BackendConfig(kind="codex"))
    with pytest.raises(BackendError, match="session identifier"):
        backend.complete(BackendRequest(messages=[], cwd=tmp_path, session_id="--dangerously-bypass-approvals-and-sandbox"))


@pytest.mark.parametrize("kind", ["claude", "gemini", "pi"])
def test_native_writes_require_explicit_unrestricted_opt_in(tmp_path, kind):
    """A working directory is not an OS sandbox for these native CLIs."""
    backend = build_backend(BackendConfig(kind=kind))
    with pytest.raises(BackendError, match="allow_unrestricted"):
        backend.complete(BackendRequest(messages=[], cwd=tmp_path, allow_write=True))
    assert "external_sandbox_required" in backend.capabilities


@pytest.mark.parametrize("events,exit_code", [([], 0), ([{"type": "turn.failed", "error": {"message": "expired credential"}}], 0),
    ([{"type": "turn.completed"}], 3), ([{"type": "error", "message": "bad"}, {"type": "turn.completed"}], 0)])
def test_cli_needs_exit_and_terminal_success(tmp_path, events, exit_code):
    """Zero exit without completion and terminal provider errors remain failures."""
    backend = build_backend(fake_cli(tmp_path, "codex", events, exit_code))
    with pytest.raises(BackendError):
        backend.complete(BackendRequest(messages=[], cwd=tmp_path))


def test_pi_retry_is_not_terminal_success(tmp_path):
    """Pi may automatically continue after an agent_end marked willRetry."""
    backend = build_backend(fake_cli(tmp_path, "pi", [{"type": "agent_end", "willRetry": True}]))
    with pytest.raises(BackendError, match="terminal"):
        backend.complete(BackendRequest(messages=[], cwd=tmp_path))


def test_pi_final_success_after_native_retry(tmp_path):
    """A transient failed message can be replaced by Pi's authoritative retry."""
    events = [{"type": "message_end", "message": {"role": "assistant", "stopReason": "error", "errorMessage": "rate limited"}},
              {"type": "agent_end", "willRetry": True}] + protocol("pi", "recovered")
    backend = build_backend(fake_cli(tmp_path, "pi", events))
    assert backend.complete(BackendRequest(messages=[], cwd=tmp_path)).text == "recovered"


def test_openai_keeps_configured_compatible_base_path(monkeypatch, tmp_path):
    """Compatibility endpoints such as Google's must not acquire an invented /v1."""
    def handler(request):
        assert str(request.url) == "https://compatible.test/v1beta/openai/chat/completions"
        assert json.loads(request.content)["max_tokens"] == 333
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]})
    route_http(monkeypatch, handler)
    backend = build_backend(BackendConfig(kind="openai", model="test", api_key="fake", base_url="https://compatible.test/v1beta/openai"))
    assert backend.complete(BackendRequest(messages=[], cwd=tmp_path, max_tokens=333)).text == "ok"


def test_cli_rejects_unstructured_controller_output(tmp_path):
    """A native agent claiming it used a tool is not a host tool call."""
    backend = build_backend(fake_cli(tmp_path, "codex", protocol("codex", "I read your project")))
    with pytest.raises(BackendError, match="structured JSON"):
        backend.complete(BackendRequest(messages=[], tools=TOOLS, cwd=tmp_path))


def test_cli_output_is_bounded(tmp_path, monkeypatch):
    """A runaway subprocess cannot fill the host's memory with stdout/stderr."""
    monkeypatch.setattr(backends, "_MAX_OUTPUT", 1024)
    script = tmp_path / "flood.py"
    script.write_text("import sys,time\nprint('x'*100000,flush=True)\ntime.sleep(10)\n")
    backend = build_backend(BackendConfig(kind="codex", command=(sys.executable, str(script)), timeout=3))
    with pytest.raises(BackendError, match="output exceeds"):
        backend.complete(BackendRequest(messages=[], cwd=tmp_path))


@pytest.mark.parametrize("cancelled", [False, True])
def test_cli_timeout_and_cancellation_stop_process(tmp_path, cancelled):
    """Both caller cancellation and elapsed deadlines stop a waiting child."""
    script = tmp_path / "wait.py"
    pidfile = tmp_path / "pid"
    script.write_text(f"import os,time\nfrom pathlib import Path\nPath({str(pidfile)!r}).write_text(str(os.getpid()))\ntime.sleep(20)\n")
    flag = threading.Event()
    trigger = threading.Timer(0.2, flag.set) if cancelled else None
    if trigger:
        trigger.start()
    backend = build_backend(BackendConfig(kind="codex", command=(sys.executable, str(script)), timeout=0.5))
    with pytest.raises(BackendCancelled if cancelled else BackendError):
        backend.complete(BackendRequest(messages=[], cwd=tmp_path, cancel=flag))
    if trigger:
        trigger.join()
    if os.name == "posix" and pidfile.exists():
        with pytest.raises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group semantics")
def test_cli_timeout_also_kills_grandchild_ignoring_termination(tmp_path):
    """Killing a CLI leader is insufficient when its command ignores SIGTERM."""
    script = tmp_path / "parent.py"
    pidfile = tmp_path / "grandchild-pid"
    child = ("import signal,os,time;from pathlib import Path;"
             "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
             f"Path({str(pidfile)!r}).write_text(str(os.getpid()));time.sleep(20)")
    script.write_text(f"import subprocess,sys,time\nsubprocess.Popen([sys.executable,'-c',{child!r}])\ntime.sleep(20)\n")
    backend = build_backend(BackendConfig(kind="codex", command=(sys.executable, str(script)), timeout=0.5))
    with pytest.raises(BackendError, match="timed out"):
        backend.complete(BackendRequest(messages=[], cwd=tmp_path))
    assert pidfile.exists()
    proc = Path('/proc') / pidfile.read_text() / 'status'
    deadline = time.monotonic() + 1
    while proc.exists() and "\nState:\tZ" not in proc.read_text() and time.monotonic() < deadline:
        time.sleep(0.01)
    if proc.exists():
        # Linux may briefly retain the reparented child as a dead zombie.
        assert "\nState:\tZ" in proc.read_text()


def test_provider_selection_never_silently_falls_back():
    """Missing routes and credentials remain actionable configuration errors."""
    with pytest.raises(BackendError, match="unsupported"):
        build_backend(BackendConfig(kind="unknown"))
    with pytest.raises(BackendError, match="credential"):
        build_backend(BackendConfig(kind="openai", model="test")).complete(BackendRequest(messages=[]))
