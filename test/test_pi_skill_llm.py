"""Retained skill model calls select Pi across all route and recovery paths."""

import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest

from assistant.platform import llm
from assistant.platform.config import Settings
from assistant.platform.runtime.contracts import Completion
from assistant.platform.runtime.pi import pi_status


def _capture(monkeypatch, *, reply=None):
    """Observe the Pi seam while preserving real role and response adaptation."""
    calls = []

    class Backend:
        """A deterministic Pi transport, with no provider or CLI execution."""

        def __init__(self, route, data_dir):
            """Capture the selected provider and owner scope."""
            self.route, self.data_dir = route, data_dir

        def complete(self, request):
            """Return one settled provider result without simulating a tool loop."""
            calls.append((self.route, self.data_dir, request))
            return reply or Completion(text='{"answer": "from Pi"}', usage={"input": 7, "output": 3})

    monkeypatch.setattr(llm, "build_pi_backend", Backend)

    def unexpected_anthropic(**_kwargs):
        """Fail if a workflow bypasses Pi through the old SDK."""
        raise AssertionError("Anthropic client bypassed Pi")

    monkeypatch.setattr(llm.anthropic, "Anthropic", unexpected_anthropic)
    return calls


@pytest.mark.parametrize("selected", ["", "anthropic", "openai", "codex"])
def test_default_and_explicit_workflow_routes_use_pi(settings, monkeypatch, selected):
    """Blank/default Anthropic and other configured routes share one engine."""
    calls = _capture(monkeypatch)
    settings = settings.model_copy(update={
        "runtime_backend": selected, "anthropic_api_key": "scratch-provider-key",
        "anthropic_model": "anthropic-test", "openai_model": "openai-test"})
    client = llm.LLM(settings)
    assert client.complete_json("Return JSON", system="Skill instructions", max_tokens=123) == {
        "answer": "from Pi"}
    route, data_dir, request = calls[0]
    assert route.kind == (selected or "anthropic")
    assert data_dir == settings.data_dir
    assert request.max_tokens == 123 and request.system == "Skill instructions"
    assert "Return JSON" in request.messages[0]["content"]
    assert "[temporal anchor]" in request.messages[0]["content"]
    assert len(calls) == 1


def test_role_override_and_probe_use_pi_owner_scope(settings, monkeypatch):
    """Optional skill routes and explicit health probes keep their exact config."""
    calls = _capture(monkeypatch)
    settings = settings.model_copy(update={
        "runtime_backend": "anthropic", "anthropic_api_key": "scratch-default-key",
        "llm_roles": {"research": {"model": "research-test",
                      "base_url": "https://research.example/anthropic",
                      "api_key": "scratch-research-key", "thinking": "disabled"}}})
    client = llm.LLM(settings)
    client.complete("Research", role="research")
    route, data_dir, request = calls[-1]
    assert route.model == "research-test" and route.api_key == "scratch-research-key"
    assert route.base_url == "https://research.example/anthropic"
    assert request.thinking_level == "off" and data_dir == settings.data_dir
    client.force_probe_route("probe-test", base_url="https://probe.example/anthropic",
                             api_key="scratch-probe-key")
    assert calls[-1][0].model == "probe-test" and calls[-1][0].api_key == "scratch-probe-key"
    assert len(calls) == 2


def test_workflow_images_remain_inline_pi_attachments(settings, monkeypatch, tmp_path):
    """The legacy image interface becomes Pi's bounded inline attachment input."""
    calls = _capture(monkeypatch)
    settings = settings.model_copy(update={"runtime_backend": "anthropic"})
    image = tmp_path / "receipt.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nimage-test")
    llm.LLM(settings).complete("Read this receipt", images=[str(image)])
    request = calls[0][2]
    assert request.images == [{"type": "image", "mimeType": "image/png",
                               "data": base64.b64encode(image.read_bytes()).decode()}]
    assert "Read this receipt" in request.messages[0]["content"]


def test_pi_length_metadata_reaches_structured_recovery(settings, monkeypatch):
    """A truncated Pi completion remains partial and cannot look settled."""
    calls = _capture(monkeypatch, reply=Completion(text='{"partial":', finish_reason="length"))
    settings = settings.model_copy(update={"runtime_backend": "anthropic"})
    client = llm.LLM(settings)
    with pytest.raises(llm.StructuredOutputTruncatedError):
        client.complete_json("Return JSON", max_tokens=16000)
    assert len(calls) == 1


def test_actual_pi_skill_completion_and_bounded_json_recovery(settings):
    """Real Pi streaming preserves partial metadata and the larger retry budget."""
    if not pi_status(settings.data_dir)["available"]:
        pytest.skip("run assistant pi install for actual SDK integration coverage")
    requests = []

    class Handler(BaseHTTPRequestHandler):
        """Serve synthetic native provider responses on loopback only."""

        def log_message(self, *_args):
            """Keep provider request details out of test output."""

        def do_POST(self):
            """Stream one partial JSON response then a complete recovered one."""
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            first = len(requests) == 1
            text, reason = ('{"partial":', "length") if first else ('{"verified": true}', "stop")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for delta, finish in (({"content": text}, None), ({}, reason)):
                chunk = {"id": "local-completion", "object": "chat.completion.chunk",
                         "created": 1, "model": "local-test", "choices": [
                             {"index": 0, "delta": delta, "finish_reason": finish}]}
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        settings = settings.model_copy(update={
            "runtime_backend": "openai", "openai_model": "local-test",
            "openai_base_url": f"http://127.0.0.1:{server.server_port}/v1",
            "openai_api_key": "scratch-local-http-key"})
        result = llm.LLM(settings).complete_json("Return JSON", max_tokens=64)
        assert result == {"verified": True}
        assert len(requests) == 2
        assert [body.get("max_tokens", body.get("max_completion_tokens"))
                for body in requests] == [64, 128]
        assert all(body["model"] == "local-test" for body in requests)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
