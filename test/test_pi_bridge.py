"""Pi bridge policy tests use real subprocesses and a local synthetic API only."""

import base64
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import stat
import threading
import time

import pytest

from assistant.platform.runtime import pi
from assistant.platform.runtime.backends import BackendCancelled, BackendError
from assistant.platform.runtime.contracts import BackendConfig, BackendRequest
from assistant.platform.runtime.pi import PausePi, PiBackend

TOOLS = [{"type": "function", "function": {"name": "read_project", "description": "Read scratch data",
          "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}]


@contextmanager
def local_api(respond):
    """Serve OpenAI SSE responses locally while retaining request evidence in memory."""
    requests = []

    class Handler(BaseHTTPRequestHandler):
        """No logs contain the synthetic authorization value or prompt payload."""

        def do_POST(self):
            """Implement only the route needed by Pi's actual compatible provider."""
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append({"path": self.path, "authorization": self.headers.get("Authorization"),
                             "payload": payload})
            delta, reason = respond(len(requests), payload)
            frames = [{"id": "synthetic", "object": "chat.completion.chunk", "created": 1,
                       "model": "test-model", "choices": [{"index": 0, "delta": delta,
                                                            "finish_reason": None}]},
                      {"id": "synthetic", "object": "chat.completion.chunk", "created": 1,
                       "model": "test-model", "choices": [{"index": 0, "delta": {},
                                                            "finish_reason": reason}]}]
            body = ("".join("data: " + json.dumps(frame) + "\n\n" for frame in frames)
                    + "data: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            """Keep the test server quiet and credentials out of captured output."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def real_backend(tmp_path, endpoint):
    """Require the actual installed SDK; never silently replace the engine in integration tests."""
    if not pi.pi_status(tmp_path)["available"]:
        pytest.skip("install pinned Pi SDK with npm ci --ignore-scripts to run this integration test")
    return PiBackend(BackendConfig(kind="openai-compatible", model="test-model", base_url=endpoint,
                                  api_key="synthetic-private-route-credential", timeout=10), tmp_path)


def tool_delta():
    """A real provider tool call, not an injected bridge event."""
    return {"role": "assistant", "tool_calls": [{"index": 0, "id": "call-one", "type": "function",
             "function": {"name": "read_project", "arguments": "{}"}}]}


def test_real_pi_tool_loop_resume_and_private_transcript(tmp_path):
    """The real SDK performs two model turns, dispatches one host tool and resumes its JSONL."""
    def respond(number, payload):
        if number == 1:
            return tool_delta(), "tool_calls"
        return {"role": "assistant", "content": "Read complete."}, "stop"

    with local_api(respond) as (endpoint, requests):
        backend = real_backend(tmp_path, endpoint)
        executed = []
        result = backend.run_session(BackendRequest(messages=[{"role": "user", "content": "Read scratch"}],
                  cwd=tmp_path, tools=TOOLS, max_tokens=75), lambda name, args: executed.append((name, args)) or
                  {"value": "synthetic-private-route-credential"}, session="room")
        assert result.status == "done" and result.text == "Read complete."
        assert executed == [("read_project", {})]
        assert len(requests) == 2
        assert requests[0]["authorization"] == "Bearer synthetic-private-route-credential"
        assert requests[0]["path"] == "/v1/chat/completions"
        budget = requests[0]["payload"].get("max_tokens", requests[0]["payload"].get("max_completion_tokens"))
        assert budget == 75
        assert any(message.get("role") == "tool" for message in requests[1]["payload"]["messages"])
        transcript = Path(result.session_file)
        assert "synthetic-private-route-credential" not in transcript.read_text()
        assert stat.S_IMODE(transcript.stat().st_mode) == 0o600
        assert stat.S_IMODE(transcript.parent.stat().st_mode) == 0o700
        resumed = backend.run_session(BackendRequest(messages=[{"role": "user", "content": "Continue"}],
                                      cwd=tmp_path), session="room", context_messages=[{
                                          "custom_type": "approval_result", "content": "Approved action actually finished."}])
        assert resumed.status == "done" and resumed.session_file == result.session_file
        context = json.dumps(requests[-1]["payload"]["messages"])
        assert "Read scratch" in context and "Approved action actually finished." in context
        transcript.unlink()
        with pytest.raises(BackendError, match="existing scoped session"):
            backend.run_session(BackendRequest(messages=[{"role": "user", "content": "Missing"}], cwd=tmp_path), session="room")
        assert len(requests) == 3


def test_real_pi_approval_pause_ends_before_next_tool(tmp_path):
    """Approval pauses the real SDK before any later call in the model batch is dispatched."""
    def respond(number, payload):
        delta = tool_delta()
        delta["tool_calls"].append({"index": 1, "id": "call-two", "type": "function",
                                    "function": {"name": "read_project", "arguments": "{}"}})
        return delta, "tool_calls"

    with local_api(respond) as (endpoint, requests):
        backend = real_backend(tmp_path, endpoint)
        calls = []
        def pause(name, arguments):
            calls.append(name)
            raise PausePi("approval-one", "Approve this exact action")
        result = backend.run_session(BackendRequest(messages=[{"role": "user", "content": "Read"}],
                                     cwd=tmp_path, tools=TOOLS), pause, session="pause")
        assert result.status == "awaiting_approval" and result.approval_id == "approval-one"
        assert calls == ["read_project"] and len(requests) == 1


def test_real_pi_truncation_is_failed_but_plain_completion_exposes_length(tmp_path):
    """A truncated agent turn is failed; a plain caller may retry with a larger budget."""
    with local_api(lambda *_: ({"role": "assistant", "content": "partial"}, "length")) as (endpoint, requests):
        backend = real_backend(tmp_path, endpoint)
        request = BackendRequest(messages=[{"role": "user", "content": "Return JSON"}], cwd=tmp_path)
        result = backend.run_session(request, session="truncated")
        assert result.status == "failed" and result.stop_reason == "length"
        completion = backend.complete(request)
        assert completion.finish_reason == "length" and completion.text == "partial"
        assert len(requests) == 2


def fake_host(monkeypatch, tmp_path, source):
    """Use a tiny subprocess only for failure paths independent of the Pi SDK internals."""
    path = tmp_path / "failure-host.mjs"
    path.write_text(source)
    monkeypatch.setattr(pi, "pi_status", lambda *_: {"available": True, "entry": str(path)})
    return PiBackend(BackendConfig(kind="openai", model="synthetic", timeout=2), tmp_path / "data")


@pytest.mark.parametrize("payload,reason", [("[]", "protocol object"), ("not-json", "invalid JSON")])
def test_invalid_host_output_fails_without_echo(monkeypatch, tmp_path, payload, reason):
    """Malformed subprocess content is rejected without propagating raw potentially secret data."""
    backend = fake_host(monkeypatch, tmp_path,
                        "process.stdout.write(" + json.dumps(payload + "\n") + ");")
    with pytest.raises(BackendError, match=reason):
        backend.run_session(BackendRequest(messages=[{"role": "user", "content": "hello"}], cwd=tmp_path), session="bad")


def test_cancellation_kills_unresponsive_host(monkeypatch, tmp_path):
    """Cancellation reaches a subprocess that ignores the protocol and stays alive."""
    backend = fake_host(monkeypatch, tmp_path, "setInterval(()=>{},10000);")
    cancel = threading.Event()
    timer = threading.Timer(0.1, cancel.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(BackendCancelled):
            backend.run_session(BackendRequest(messages=[{"role": "user", "content": "hello"}],
                                cwd=tmp_path, cancel=cancel), session="cancel")
    finally:
        timer.cancel()
    assert time.monotonic() - started < 2


def test_output_budget_is_bounded_before_newline(monkeypatch, tmp_path):
    """A child cannot force an unbounded line allocation before the wire limit is checked."""
    backend = fake_host(monkeypatch, tmp_path, "process.stdout.write('x'.repeat(8192));setInterval(()=>{},10000);")
    monkeypatch.setattr(pi, "_MAX_WIRE", 4096)
    with pytest.raises(BackendError, match="wire budget"):
        backend.run_session(BackendRequest(messages=[{"role": "user", "content": "hello"}], cwd=tmp_path), session="overflow")


def test_session_lock_wait_is_cancellable(monkeypatch, tmp_path):
    """Concurrent sessions wait without holding personal-store locks and can be cancelled."""
    backend = fake_host(monkeypatch, tmp_path, "")
    request = BackendRequest(messages=[{"role": "user", "content": "hello"}], cwd=tmp_path,
                             cancel=threading.Event())
    lock = tmp_path / "lock"
    acquired = threading.Event()
    release = threading.Event()
    def hold():
        with pi._session_lock(lock, request, time.monotonic()+5):
            acquired.set()
            release.wait(2)
    holder = threading.Thread(target=hold)
    holder.start()
    assert acquired.wait(1)
    request.cancel.set()
    try:
        with pytest.raises(BackendCancelled):
            with pi._session_lock(lock, request, time.monotonic()+1):
                pytest.fail("a concurrent session acquired the held lock")
    finally:
        release.set()
        holder.join(timeout=2)


def test_symlink_private_directories_and_lock_rejected(tmp_path):
    """Session directories and lock files cannot redirect writes outside owner storage."""
    base = tmp_path / "data"
    outside = tmp_path / "outside"
    base.mkdir()
    outside.mkdir()
    (base / "pi").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        pi._private_directory(base, "pi", "sessions")
    lock = tmp_path / "lock"
    lock.symlink_to(outside / "target")
    with pytest.raises(BackendError, match="private Pi session lock"):
        with pi._session_lock(lock, BackendRequest([]), time.monotonic()+1):
            pytest.fail("followed a lock symlink")
    assert not (outside / "target").exists()


def test_images_are_bounded_and_explicit(tmp_path):
    """Both existing inline legacy blocks and local paths encode into the same Pi format."""
    image = tmp_path / "tiny.png"
    image.write_bytes(b"png-placeholder")
    inline = {"type": "image", "mimeType": "image/png", "data": base64.b64encode(image.read_bytes()).decode()}
    assert pi._images([str(image), inline]) == [inline, inline]
    with pytest.raises(ValueError, match="eight"):
        pi._images([inline] * 9)
    with pytest.raises(ValueError, match="base64"):
        pi._images([{**inline, "data": "not base64"}])
    with pytest.raises(ValueError, match="invalid inline"):
        pi._images([{**inline, "url": "https://example.invalid/img.png"}])


def test_install_uses_pinned_assets_and_disables_lifecycle(monkeypatch, tmp_path):
    """The explicit install command copies only shipped sources and disables npm scripts."""
    monkeypatch.setattr(pi, "_node_status", lambda: (True, "24.19.0"))
    monkeypatch.setattr(pi.shutil, "which", lambda _: "/synthetic/executable")
    seen = []
    def run(argv, **kwargs):
        seen.append((argv, kwargs))
        return type("Result", (), {"returncode": 0})()
    monkeypatch.setattr(pi.subprocess, "run", run)
    monkeypatch.setattr(pi, "pi_status", lambda *_: {"available": True})
    assert pi.install_pi(tmp_path)["available"]
    assert seen[0][0] == ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"]
    target = tmp_path / "pi" / "host"
    assert (target / "package-lock.json").read_bytes() == (pi._ASSETS / "package-lock.json").read_bytes()
    assert not (target / "test").exists()
    assert stat.S_IMODE((target / "src").stat().st_mode) == 0o700


def test_status_rejects_old_node_and_reports_stable_fix(monkeypatch, tmp_path):
    """Doctor receives readiness details and useful setup guidance without a model call."""
    monkeypatch.setattr(pi, "_node_status", lambda: (False, "20.1.0"))
    state = pi.pi_status(tmp_path)
    assert not state["available"] and not state["node_ok"]
    assert state["node_version"] == "20.1.0" and state["assets_ok"]
    assert state["fix"].startswith("assistant pi install --data-dir ")


def test_real_pi_cli_rpc_is_single_completion_controller_even_without_tools(monkeypatch, tmp_path):
    """Pi owns the turn while an authenticated CLI adapter is only a constrained completion RPC."""
    if not pi.pi_status(tmp_path)["available"]:
        pytest.skip("install pinned Pi SDK for integration tests")
    requests = []
    class FakeCLI:
        """Synthetic transport: no CLI binary or external provider is executed."""
        def __init__(self, config):
            self.config = config
        def complete(self, request):
            requests.append(request)
            return pi.Completion(text="One constrained completion")
    monkeypatch.setattr(pi, "CLIBackend", FakeCLI)
    backend = PiBackend(BackendConfig(kind="claude", timeout=10), tmp_path)
    result = backend.run_session(BackendRequest(messages=[{"role": "user", "content": "hello"}],
                                 cwd=tmp_path), session="cli-rpc")
    assert result.status == "done" and result.text == "One constrained completion"
    assert len(requests) == 1 and requests[0].controller_mode and requests[0].tools == []


def test_real_pi_receives_inline_image(tmp_path):
    """Native Pi API providers receive the actual image bytes instead of a text-only substitute."""
    image = {"type": "image", "mimeType": "image/png", "data":
             "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="}
    with local_api(lambda *_: ({"role": "assistant", "content": "Image received"}, "stop")) as (endpoint, requests):
        backend = real_backend(tmp_path, endpoint)
        result = backend.run_session(BackendRequest(messages=[{"role": "user", "content": "See attached"}],
                                     cwd=tmp_path, images=[image]), session="image")
        assert result.status == "done"
        assert image["data"] in json.dumps(requests[0]["payload"])


def test_plain_error_exposes_only_sanitized_status_metadata(monkeypatch, tmp_path):
    """Legacy health routing can classify a failure without retaining a raw credential."""
    backend = PiBackend(BackendConfig(kind="openai", model="test"), tmp_path)
    monkeypatch.setattr(backend, "run_session", lambda *_args, **_kwargs: pi.PiResult(
        status="failed", error="401 authentication failed [redacted credential]", status_code=401,
        error_code="provider_error", stop_reason="error"))
    with pytest.raises(BackendError) as raised:
        backend.complete(BackendRequest(messages=[{"role": "user", "content": "hello"}], cwd=tmp_path))
    assert raised.value.status_code == 401
    assert raised.value.body["error"]["type"] == "provider_error"
