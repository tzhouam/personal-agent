"""Runtime application journeys use real stores and fake provider/transport edges."""

import asyncio
import base64
import copy
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import sys

import pytest

from assistant.agent.runtime.app import RuntimeApplication
from assistant.platform.config import Settings
from assistant.platform.registry import UserRegistry
from assistant.platform.runtime.config import RuntimeConfig
from assistant.platform.runtime.contracts import BackendConfig, Completion, DeliveryReceipt, ToolCall
from assistant.platform.runtime.inbox import Inbox
from assistant.platform.runtime.pi import PausePi, PiResult
from assistant.platform.runtime.store import RuntimeStore


class ScriptedPiBackend:
    """Replace the Pi session boundary while exercising real host dispatch and stores."""

    kind = "pi"
    capabilities = frozenset({"pi_session", "tools"})

    def __init__(self, *answers):
        """Accept scripted model decisions, all consumed within a single Pi session."""
        self.answers = list(answers)
        self.requests = []

    def _capture(self, request, history=None, context_messages=None):
        """Keep one independent request snapshot per Pi session invocation."""
        self.requests.append({"messages": copy.deepcopy(history or []) + copy.deepcopy(request.messages),
                              "system": request.system, "cwd": request.cwd,
                              "images": list(request.images),
                              "context_messages": copy.deepcopy(context_messages or [])})

    def complete(self, request):
        """The explicit doctor probe uses a plain Pi completion without host tools."""
        self._capture(request)
        if not self.answers:
            raise AssertionError("unexpected Pi execution")
        answer = self.answers.pop(0)
        return answer(request) if callable(answer) else answer

    def run_session(self, request, tool_handler=None, *, session, history=None,
                    context_messages=None, max_steps=12):
        """Simulate Pi's owned loop; production Python receives only authorized RPCs."""
        self._capture(request, history, context_messages)
        while self.answers:
            answer = self.answers.pop(0)
            answer = answer(request) if callable(answer) else answer
            for call in answer.tool_calls:
                try:
                    tool_handler(call.name, call.arguments)
                except PausePi as pause:
                    return PiResult("awaiting_approval", text=pause.text,
                                    approval_id=pause.approval_id)
            if not answer.tool_calls:
                return PiResult("done", text=answer.text, session_id="fixture-pi-session")
        raise AssertionError("unexpected Pi execution")


class NativeProjectPiBackend(ScriptedPiBackend):
    """Exercise actual confined file tools and real independent Python verification."""

    def run_session(self, request, tool_handler=None, *, session, history=None,
                    context_messages=None, max_steps=12):
        """Ask the host to write inside its registered workspace, as Pi would."""
        self._capture(request, history, context_messages)
        result = tool_handler("write_file", {"path": "answer.py", "content": "VALUE = 42\n"})
        assert result.get("ok", True) is True
        return PiResult("done", text="Created answer.py; the host must verify it.",
                        session_id="fixture-project-session")


def config(**updates):
    """Resolve a fixture backend route through the real configuration contract."""
    return RuntimeConfig(backend="fixture", backends={"fixture": BackendConfig(
        kind="pi", model="fixture-model", api_key="fixture-only-key")}, **updates)


def channel_specs():
    """Configure both transports without opening any actual network connection."""
    return {"slack": {"app_token": "fake-app", "bot_token": "fake-bot", "allowed_users": ("U1",),
                      "team_id": "T1", "bot_user_id": "B1"},
            "qq": {"allowed_users": ("123",), "self_id": "99", "allowed_groups": ("456",)}}


def slack_payload(**changes):
    """Represent an admitted Socket Mode mention with a stable reply thread."""
    event = {"type": "app_mention", "channel": "C1", "user": "U1", "ts": "10.001",
             "text": "<@B1> Check Orion", "thread_ts": "10.000"}
    event.update(changes)
    return {"type": "events_api", "envelope_id": "env-1", "payload": {
            "team_id": "T1", "event_id": "event-1", "event": event}}


def qq_payload(**changes):
    """Represent an admitted OneBot group mention with real segment spelling."""
    event = {"post_type": "message", "message_type": "group", "self_id": 99, "user_id": 123,
             "group_id": 456, "message_id": 42, "message": [
                 {"type": "at", "data": {"qq": "99"}},
                 {"type": "text", "data": {"text": " Check Orion"}}]}
    event.update(changes)
    return event


async def close_channels(app):
    """Always release constructed clients, even though none have connected."""
    await asyncio.gather(*(channel.close() for channel in app.channels.values()))


async def drain(app, complete):
    """Drain the actual inbox worker until an observable receipt is present."""
    worker = asyncio.create_task(app._inbox_worker())
    try:
        async def wait():
            """Bound the fixture without relying on worker sleep timing."""
            while not complete():
                if worker.done():
                    await worker
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait(), timeout=3)
    finally:
        app.stop.set()
        await asyncio.wait_for(worker, timeout=2)


def cli_setup(settings, monkeypatch, tmp_path, backend):
    """Pin CLI configuration and replace only its model-construction boundary."""
    from assistant import runtime_cli
    import assistant.agent.runtime.app as app_module
    route_file = tmp_path / "runtime-fixture.toml"
    route_file.write_text('[runtime]\nbackend = "fixture"\n[backends.fixture]\nkind = "pi"\n'
                          'model = "fixture-model"\napi_key_env = "FIXTURE_MODEL_KEY"\n')
    monkeypatch.setenv("FIXTURE_MODEL_KEY", "fixture-only-key")
    monkeypatch.setattr(runtime_cli, "Settings", lambda: settings)
    monkeypatch.setattr(app_module, "build_pi_backend", lambda route, data_dir: backend)
    return runtime_cli, ["--data-dir", str(settings.data_dir), "--config", str(route_file)]


def test_application_restart_retains_history_and_sourced_memory(settings, monkeypatch):
    import assistant.agent.runtime.app as app_module
    first_backend = ScriptedPiBackend(Completion(tool_calls=[ToolCall("remember", "memory_remember", {
        "text": "Orion uses SQLite", "project": "orion"})]), Completion(text="Saved Orion's decision."))
    second_backend = ScriptedPiBackend(Completion(text="Orion uses SQLite, from your earlier message."))
    backends = iter([first_backend, second_backend])
    monkeypatch.setattr(app_module, "build_pi_backend", lambda route, data_dir: next(backends))
    first = RuntimeApplication(settings, config())
    first.kernel().turn("Remember Orion uses SQLite", session="same-room", project="orion")
    restarted = RuntimeApplication(settings, config())
    result = restarted.kernel().turn("What database does Orion use?", session="same-room", project="orion")
    assert result.status == "done"
    context = second_backend.requests[0]
    assert "Saved Orion's decision" in json.dumps(context["messages"])
    assert "Orion uses SQLite" in context["system"]
    assert len(RuntimeStore(settings.data_dir).messages("same-room")) == 4


def test_application_preserves_explicit_settings_and_pins_data_root(settings, monkeypatch, tmp_path):
    explicit = settings.model_copy(update={"github_token": "fixture-only-token", "github_user": "fixture-owner",
                                          "finance_currency": "HKD", "runtime_project_roots": [tmp_path / "projects"]})
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "wrong-data-root"))
    monkeypatch.setenv("GITHUB_TOKEN", "unrelated-environment-token")
    app = RuntimeApplication(explicit, config())
    fresh = app.refresh_settings()
    assert fresh.data_dir == explicit.data_dir
    assert fresh.github_token == explicit.github_token and fresh.github_user == "fixture-owner"
    assert fresh.finance_currency == "HKD"
    assert fresh.runtime_project_roots == [tmp_path / "projects"]
    assert not (tmp_path / "wrong-data-root").exists()


def test_application_multitenant_reads_only_each_owner_credentials(tmp_path, monkeypatch):
    import assistant.agent.runtime.app as app_module
    root = Settings(_env_file=None, data_dir=tmp_path / "operator-root", deployment_mode="multi_tenant",
                    github_token="operator-fixture-token", smtp_user="operator@example.invalid")
    roster = UserRegistry(root.data_dir)
    roster.add_user("alice1")
    roster.add_user("bob22")
    user_config = root.data_dir / "users" / "alice1" / "config.env"
    user_config.parent.mkdir(parents=True)
    user_config.write_text("GITHUB_TOKEN=alice-fixture-token\nGITHUB_USER=alice-user\n")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "wrong-environment-root"))
    monkeypatch.setenv("GITHUB_TOKEN", "process-fixture-token")
    monkeypatch.setenv("SMTP_USER", "process@example.invalid")
    monkeypatch.setattr(app_module, "build_pi_backend", lambda route, data_dir: ScriptedPiBackend())
    app = RuntimeApplication(root, config())
    alice, bob = app.kernel("alice1"), app.kernel("bob22")
    assert alice.settings.data_dir == root.data_dir / "users" / "alice1"
    assert bob.settings.data_dir == root.data_dir / "users" / "bob22"
    assert alice.settings.github_token == "alice-fixture-token"
    assert alice.settings.smtp_user == bob.settings.smtp_user == ""
    assert bob.settings.github_token == "" and bob.settings.github_user == ""
    alice.memory.remember("Alice private Orion decision", "owner:alice")
    assert bob.memory.recall("Orion") == []
    with pytest.raises(ValueError, match="active registered"):
        app.kernel("unknown-user")
    assert not (tmp_path / "wrong-environment-root").exists()


def test_application_refreshes_pi_route_from_each_owner_configuration(tmp_path, monkeypatch):
    """Configured API adapters receive scoped custom credentials after a cached refresh."""
    import assistant.agent.runtime.app as app_module
    root = Settings(_env_file=None, data_dir=tmp_path / "tenants", deployment_mode="multi_tenant")
    roster = UserRegistry(root.data_dir)
    for owner in ("alice1", "bob22"):
        roster.add_user(owner)
    alice_file = root.data_dir / "users" / "alice1" / "config.env"
    alice_file.parent.mkdir(parents=True, exist_ok=True)
    alice_file.write_text("TENANT_PI_KEY=alice-fixture-key\n")
    monkeypatch.setenv("TENANT_PI_KEY", "unrelated-process-key")
    route_file = tmp_path / "shared-routing.toml"
    route_file.write_text('[runtime]\nbackend = "personal"\n[backends.personal]\n'
                          'kind = "deepseek"\nmodel = "fixture-model"\n'
                          'api_key_env = "TENANT_PI_KEY"\n')

    def factory(route, data_dir):
        """Expose the actual scoped route without running Pi or a provider."""
        backend = ScriptedPiBackend()
        backend.config, backend.data_dir = route, data_dir
        return backend

    monkeypatch.setattr(app_module, "build_pi_backend", factory)
    configured = app_module.load_runtime_config(root, path=route_file)
    app = RuntimeApplication(root, configured)
    alice, bob = app.kernel("alice1"), app.kernel("bob22")
    assert alice.backend.config.api_key == "alice-fixture-key"
    assert bob.backend.config.api_key == ""
    assert alice.backend.data_dir == alice.settings.data_dir
    assert bob.backend.data_dir == bob.settings.data_dir
    alice_file.write_text("TENANT_PI_KEY=rotated-alice-fixture-key\n")
    assert app.kernel("alice1") is alice
    assert alice.backend.config.api_key == "rotated-alice-fixture-key"
    assert alice.projects.backend is alice.backend


def test_project_worker_refreshes_owner_and_pi_route_without_another_chat(tmp_path, monkeypatch):
    """Queued work uses rotated credentials and model config without a chat refresh."""
    import assistant.agent.runtime.app as app_module
    root = Settings(_env_file=None, data_dir=tmp_path / "tenants", deployment_mode="multi_tenant")
    UserRegistry(root.data_dir).add_user("alice1")
    owner_file = root.data_dir / "users" / "alice1" / "config.env"
    owner_file.parent.mkdir(parents=True, exist_ok=True)
    owner_file.write_text("TENANT_PI_KEY=old-fixture-key\nGITHUB_USER=old-owner\n")
    route_file = tmp_path / "routing.toml"

    def write_route(model):
        """Change the persistent model route exactly as an operator would."""
        route_file.write_text('[runtime]\nbackend = "personal"\n[backends.personal]\n'
                              f'kind = "deepseek"\nmodel = "{model}"\n'
                              'api_key_env = "TENANT_PI_KEY"\n')

    write_route("old-model")
    used = []

    class ProjectBackend(NativeProjectPiBackend):
        """Record the selected route while exercising actual workspace host tools."""

        def run_session(self, request, *args, **kwargs):
            """Observe credentials only in memory; no network or real messages."""
            used.append((self.config.api_key, self.config.model, request.cwd, self.data_dir))
            return super().run_session(request, *args, **kwargs)

    def factory(route, data_dir):
        """Keep each constructed Pi transport bound to its own resolved configuration."""
        backend = ProjectBackend()
        backend.config, backend.data_dir = route, data_dir
        return backend

    monkeypatch.setattr(app_module, "build_pi_backend", factory)
    app = RuntimeApplication(root, app_module.load_runtime_config(root, path=route_file))
    cached = app.kernel("alice1")
    assert cached.backend.config.api_key == "old-fixture-key"
    workspace = tmp_path / "owner-project"
    workspace.mkdir()
    cached.projects.register("orion", workspace)
    queued = cached.projects.submit("orion", "Create answer.py")
    owner_file.write_text("TENANT_PI_KEY=rotated-fixture-key\nGITHUB_USER=rotated-owner\n")
    write_route("rotated-model")

    async def scenario():
        """Drive just the project worker until a real terminal task receipt exists."""
        worker = asyncio.create_task(app._project_worker())
        try:
            async def completed():
                """Bound observation independently of the worker's polling interval."""
                while cached.projects.status(queued["id"])["status"] in {"queued", "running"}:
                    if worker.done():
                        await worker
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(completed(), timeout=3)
        finally:
            app.stop.set()
            await asyncio.wait_for(worker, timeout=2)

    asyncio.run(scenario())
    assert cached.projects.status(queued["id"])["status"] == "done"
    assert (workspace / "answer.py").read_text() == "VALUE = 42\n"
    assert used == [("rotated-fixture-key", "rotated-model", workspace,
                     root.data_dir / "users" / "alice1")]
    assert cached.projects.settings.github_user == "rotated-owner"


def test_cli_skill_listing_mutation_and_exact_approval(settings, monkeypatch, tmp_path, capsys):
    from assistant.agent import orchestrator
    backend = ScriptedPiBackend()
    cli, options = cli_setup(settings, monkeypatch, tmp_path, backend)
    assert cli.main(["skills", "list", *options]) == 0
    names = {row["name"] for row in json.loads(capsys.readouterr().out)}
    assert {"daily", "finance_summary", "query_health", "recall_memory"} <= names
    assert cli.main(["skills", "run", "add_todo", "--args", '{"title":"CLI preserved todo"}', *options]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert cli.main(["skills", "run", "list_todos", *options]) == 0
    assert "CLI preserved todo" in json.loads(capsys.readouterr().out)["text"]
    calls = []
    monkeypatch.setattr(orchestrator, "run", lambda scoped, **kwargs: calls.append((scoped, kwargs)) or 0)
    assert cli.main(["skills", "run", "daily", *options, "--session", "owner-room"]) == 0
    pending = json.loads(capsys.readouterr().out)
    assert pending["status"] == "awaiting_approval" and calls == []
    assert cli.main(["approve", pending["approval"], *options, "--session", "other-room"]) == 1
    assert "another session" in capsys.readouterr().err and calls == []
    assert cli.main(["approve", pending["approval"], *options, "--session", "owner-room"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "done"
    assert len(calls) == 1 and calls[0][0].data_dir == settings.data_dir
    assert cli.main(["approve", pending["approval"], *options, "--session", "owner-room"]) == 1
    assert "consumed" in capsys.readouterr().err and len(calls) == 1
    assert not backend.requests


def test_cli_project_changes_real_scratch_files_and_retains_verification(settings, monkeypatch, tmp_path, capsys):
    backend = NativeProjectPiBackend()
    cli, options = cli_setup(settings, monkeypatch, tmp_path, backend)
    workspace = tmp_path / "real-verification-project"
    workspace.mkdir()
    (workspace / "verify.py").write_text("from answer import VALUE\nassert VALUE == 42\nprint('REAL_HOST_CHECK_OK')\n")
    command = [sys.executable, "verify.py"]
    assert cli.main(["project", "register", "orion", str(workspace), "--verify", json.dumps(command), *options]) == 0
    registered = json.loads(capsys.readouterr().out)
    assert registered["name"] == "orion" and registered["verification"] == [command]
    assert cli.main(["project", "run", "orion", "Create answer.py with VALUE=42", *options]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "done" and result["verified"] is True
    assert result["evidence"][0]["returncode"] == 0
    assert "REAL_HOST_CHECK_OK" in result["evidence"][0]["stdout"]
    assert {row["path"] for row in result["artifacts"]} >= {"answer.py"}
    assert (workspace / "answer.py").read_text() == "VALUE = 42\n"
    proof = Path(result["evidence_path"])
    assert proof.is_relative_to(settings.data_dir) and json.loads(proof.read_text())["verified"]
    assert cli.main(["project", "status", result["id"], *options]) == 0
    assert json.loads(capsys.readouterr().out)["verified"] is True
    assert len(backend.requests) == 1


def test_cli_data_dir_override_controls_all_durable_outputs(settings, monkeypatch, tmp_path, capsys):
    backend = ScriptedPiBackend(Completion(text="Saved conversation in the chosen root."))
    cli, options = cli_setup(settings, monkeypatch, tmp_path, backend)
    selected = tmp_path / "operator-selected-data"
    options[1] = str(selected)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "wrong-root"))
    assert cli.main(["memory", "add", "Orion decision", *options]) == 0
    capsys.readouterr()
    assert cli.main(["ask", "Recall Orion decision", *options]) == 0
    capsys.readouterr()
    assert "Orion decision" in backend.requests[0]["system"]
    assert (selected / "runtime_memory.sqlite").exists()
    assert RuntimeStore(selected).messages("cli")[-1]["content"].startswith("Saved conversation")
    assert not (settings.data_dir / "runtime_memory.sqlite").exists()
    assert not (tmp_path / "wrong-root").exists()


def test_cli_image_turn_keeps_pi_session_and_project_context(settings, monkeypatch, tmp_path, capsys):
    """Images keep the same CLI session, durable memory and Pi request boundary."""
    backend = ScriptedPiBackend(Completion(text="Pi received the image."))
    cli, options = cli_setup(settings, monkeypatch, tmp_path, backend)
    image = tmp_path / "fixture.png"
    image.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1IAAAAASUVORK5CYII="))
    assert cli.main(["ask", "Describe this", "--image", str(image), "--project", "orion",
                     "--session", "image-room", *options]) == 0
    assert capsys.readouterr().out.strip() == "Pi received the image."
    assert backend.requests[0]["images"] == [str(image)]
    assert "image" in backend.requests[0]["system"].lower()
    messages = RuntimeStore(settings.data_dir).messages("image-room")
    assert [message["content"] for message in messages] == ["Describe this", "Pi received the image."]
    assert RuntimeStore(settings.data_dir).messages("bridge:image-room") == []


def test_cli_doctor_only_calls_a_model_when_probe_is_explicit(settings, monkeypatch, tmp_path, capsys):
    backend = ScriptedPiBackend(Completion(text="PERSONAL_AGENT_RUNTIME_OK"))
    cli, options = cli_setup(settings, monkeypatch, tmp_path, backend)
    monkeypatch.setattr(cli, "build_pi_backend", lambda route, data_dir: backend)
    monkeypatch.setattr(cli.shutil, "which", lambda command: "/fixture/bin/" + command)
    monkeypatch.setattr(cli, "pi_status", lambda data_dir: {
        "available": True, "version": "1.4.0", "assets_ok": True, "node_ok": True})
    assert cli.main(["doctor", *options]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["backend"] == "fixture" and first["live_probe"] == "not_requested"
    assert first["engine"] == "pi" and first["pi"]["assets_ok"]
    assert backend.requests == []
    assert cli.main(["doctor", "--probe", *options]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["live_probe"] == "passed" and len(backend.requests) == 1
    assert backend.requests[0]["cwd"] == settings.data_dir


@pytest.mark.parametrize("kind", ["openai", "anthropic", "deepseek", "openai-compatible", "pi"])
def test_api_route_readiness_checks_credentials_and_model_without_cli_lookup(kind, monkeypatch, tmp_path):
    """Every Pi API provider reports readiness from its route, never a command name."""
    from assistant import runtime_cli

    def no_executable_lookup(command):
        """An API provider name must not be mistaken for a local CLI executable."""
        raise AssertionError("API availability attempted a CLI lookup")

    monkeypatch.setattr(runtime_cli.shutil, "which", no_executable_lookup)
    routes = RuntimeConfig(backend="ready", backends={
        "ready": BackendConfig(kind=kind, model="fixture-model", api_key="private-fixture-key"),
        "missing-key": BackendConfig(kind=kind, model="fixture-model"),
        "missing-model": BackendConfig(kind=kind, api_key="private-fixture-key")})
    rows = runtime_cli._routes(routes, tmp_path)
    assert [row["available"] for row in rows] == [True, False, False]
    assert all(row["engine"] == "pi" for row in rows)
    assert "private-fixture-key" not in json.dumps(rows)


@pytest.mark.parametrize("kind", ["codex", "claude", "gemini"])
def test_native_model_route_readiness_uses_its_explicit_executable(kind, monkeypatch, tmp_path):
    """CLI model adapters remain selectable without launching their processes."""
    from assistant import runtime_cli
    looked_up = []
    monkeypatch.setattr(runtime_cli.shutil, "which",
                        lambda command: looked_up.append(command) or "/fixture/" + command)
    routes = RuntimeConfig(backend="fixture", backends={
        "fixture": BackendConfig(kind=kind, command=("fixture-model-adapter",))})
    rows = runtime_cli._routes(routes, tmp_path)
    assert looked_up == ["fixture-model-adapter"]
    assert rows[0]["available"] and rows[0]["engine"] == "pi"


def test_pi_install_is_explicit_and_status_does_not_construct_an_agent(
        settings, monkeypatch, capsys, tmp_path):
    """Inspect readiness without models; only the install command may install the SDK."""
    from assistant import runtime_cli
    calls = []
    selected = tmp_path / "selected-pi-data"
    monkeypatch.setattr(runtime_cli, "Settings", lambda: settings)

    def status(data_dir):
        """Return unavailable readiness while recording its exact data scope."""
        calls.append(("status", data_dir))
        return {"available": False, "assets_ok": True, "node_ok": True,
                "fix": "assistant pi install"}

    def install(data_dir):
        """Fake installation, keeping the test independent of npm and the network."""
        calls.append(("install", data_dir))
        return {"available": True, "version": "1.4.0"}

    def forbidden(*args, **kwargs):
        """Neither SDK management operation may construct a model session."""
        raise AssertionError("unexpected agent construction")

    monkeypatch.setattr(runtime_cli, "pi_status", status)
    monkeypatch.setattr(runtime_cli, "install_pi", install)
    monkeypatch.setattr(runtime_cli, "RuntimeApplication", forbidden)
    monkeypatch.setattr(runtime_cli, "load_runtime_config", forbidden)
    options = ["--data-dir", str(selected)]
    assert runtime_cli.handles(["pi", "status"])
    assert runtime_cli.main(["pi", "status", *options]) == 1
    assert json.loads(capsys.readouterr().out)["fix"] == "assistant pi install"
    assert calls == [("status", selected)]
    assert runtime_cli.main(["pi", "install", *options]) == 0
    assert json.loads(capsys.readouterr().out)["available"] is True
    assert calls == [("status", selected), ("install", selected)]


def test_bridge_images_go_through_the_pi_kernel(settings, monkeypatch, tmp_path):
    """All image turns use the same kernel policy and preserve rejected image context."""
    import assistant.agent.chat.agent as legacy
    from assistant.agent.runtime.kernel import TurnResult
    calls = []

    class KernelSpy:
        """Capture only this composition boundary; policy is tested at the kernel."""

        def seed_history(self, session, history):
            """Retain the legacy bridge's history migration contract."""
            calls.append(("history", session, history))

        def turn(self, text, **kwargs):
            """Record the complete image context forwarded to actual Pi execution."""
            calls.append(("turn", text, kwargs))
            return TurnResult("Image processed through Pi.")

    def forbidden(*args, **kwargs):
        """An image must never enter the retained chat model/repair loop."""
        raise AssertionError("legacy image loop invoked")

    app = RuntimeApplication(settings, config())
    monkeypatch.setattr(app, "kernel", lambda *args, **kwargs: KernelSpy())
    monkeypatch.setattr(legacy, "handle_turn", forbidden)
    images = [str(tmp_path / "receipt.png")]
    rejected = ["unsupported.gif"]
    history = [{"role": "user", "content": "Prior conversation"}]
    result = app.handle_turn("Log this receipt", settings, session="email-thread",
                             history=history, image_paths=images, rejected_images=rejected)
    assert result.reply == "Image processed through Pi."
    assert calls[0] == ("history", "bridge:email-thread", history)
    assert calls[1] == ("turn", "Log this receipt", {
        "session": "bridge:email-thread", "cancel": app.stop,
        "image_paths": images, "rejected_images": rejected})


def test_slack_qq_ingest_kernel_receipt_and_restart_dedup(settings, monkeypatch):
    import assistant.agent.runtime.app as app_module
    backend = ScriptedPiBackend(Completion(text="Slack durable reply"), Completion(text="QQ durable reply"))
    monkeypatch.setattr(app_module, "build_pi_backend", lambda route, data_dir: backend)
    async def scenario():
        """Exercise both actual decoders, one durable worker and saved-reply sends."""
        app = RuntimeApplication(settings, config(channels=channel_specs()))
        sent = []
        try:
            slack = app.channels["slack", "default"]
            qq = app.channels["qq_onebot", "default"]
            assert slack.decode_event(slack_payload(user="outsider")) is None
            assert qq.decode_event(qq_payload(user_id=999)) is None
            events = [slack.decode_event(slack_payload()), qq.decode_event(qq_payload())]
            assert all(events)
            async def send(event, text):
                """The reply must already be durable before a transport sees it."""
                with sqlite3.connect(app.inboxes["default"].path) as db:
                    saved, state = db.execute("SELECT reply,state FROM inbox WHERE id=?", (event.dedup_key,)).fetchone()
                assert saved == text and state == "sending"
                sent.append((event, text))
                return DeliveryReceipt("sent", ("sent-" + event.channel,))
            slack.send = qq.send = send
            for event in events:
                await app.ingest(event)
                await app.ingest(event)
            assert len(app.inboxes["default"].list()) == 2
            await drain(app, lambda: len(sent) == 2)
            assert {row["state"] for row in app.inboxes["default"].list()} == {"sent"}
            assert len(backend.requests) == 2
            assert sent[0][0].thread_id == "10.000"
            assert sent[1][0].room_id == "456"
            assert events[0].session_key != events[1].session_key
        finally:
            await close_channels(app)
        restarted = RuntimeApplication(settings, config(channels=channel_specs()))
        try:
            for event in events:
                await restarted.ingest(event)
            assert restarted.inboxes["default"].claim() is None
            assert restarted.inboxes["default"].claim(delivery=True) is None
            assert len(backend.requests) == 2
        finally:
            await close_channels(restarted)
    asyncio.run(scenario())


def test_socket_mode_ack_happens_after_durable_application_admission(settings, monkeypatch):
    import assistant.agent.runtime.app as app_module
    monkeypatch.setattr(app_module, "build_pi_backend", lambda route, data_dir: ScriptedPiBackend())
    async def scenario():
        """An acknowledged event must survive even if model execution never starts."""
        app = RuntimeApplication(settings, config(channels={"slack": channel_specs()["slack"]}))
        sent = []
        class Socket:
            """Assert durable storage at the exact transport ACK boundary."""
            async def send(self, raw):
                assert len(Inbox(settings.data_dir).list()) == 1
                sent.append(json.loads(raw))
        try:
            slack = app.channels["slack", "default"]
            assert await slack.receive_envelope(slack_payload(), Socket())
            assert sent == [{"envelope_id": "env-1"}]
            assert Inbox(settings.data_dir).list()[0]["state"] == "queued"
        finally:
            await close_channels(app)
    asyncio.run(scenario())


def test_application_rejects_forged_owner_and_stops_disabled_owner_work(tmp_path, monkeypatch):
    import assistant.agent.runtime.app as app_module
    root = Settings(_env_file=None, data_dir=tmp_path / "tenants", deployment_mode="multi_tenant")
    roster = UserRegistry(root.data_dir)
    roster.add_user("alice1")
    backend = ScriptedPiBackend()
    monkeypatch.setattr(app_module, "build_pi_backend", lambda route, data_dir: backend)
    async def scenario():
        """Trusted transport ownership is rechecked before work and delivery."""
        spec = {**channel_specs()["slack"], "owner_id": "alice1"}
        app = RuntimeApplication(root, config(channels={"slack": spec}))
        sent = []
        try:
            channel = app.channels["slack", "default"]
            event = channel.decode_event(slack_payload())
            with pytest.raises(ValueError, match="owner"):
                await app.ingest(replace(event, owner_id="forged"))
            with pytest.raises(ValueError, match="configured adapter"):
                await app.ingest(replace(event, account_id="forged"))
            assert app.inboxes["alice1"].list() == []
            await app.ingest(event)
            roster.set_status("alice1", "disabled")
            with pytest.raises(ValueError, match="active registered"):
                await app.ingest(replace(event, event_id="second-event"))
            async def send(saved_event, text):
                sent.append(text)
                return DeliveryReceipt("sent", ("must-not-send",))
            channel.send = send
            await drain(app, lambda: app.inboxes["alice1"].list()[0]["state"] == "unknown")
            assert sent == [] and backend.requests == []
        finally:
            await close_channels(app)
    asyncio.run(scenario())


def test_delivery_only_retry_after_restart_never_reexecutes_model(settings, monkeypatch):
    import assistant.agent.runtime.app as app_module
    backend = ScriptedPiBackend(Completion(text="Original saved answer"))
    monkeypatch.setattr(app_module, "build_pi_backend", lambda route, data_dir: backend)
    async def scenario():
        """An uncertain send can be retried explicitly using its persisted reply."""
        app = RuntimeApplication(settings, config(channels={"qq": channel_specs()["qq"]}))
        try:
            channel = app.channels["qq_onebot", "default"]
            event = channel.decode_event(qq_payload())
            async def uncertain(event, text):
                return DeliveryReceipt("unknown", error="fixture connection lost after write")
            channel.send = uncertain
            await app.ingest(event)
            await drain(app, lambda: app.inboxes["default"].list()[0]["state"] == "unknown")
        finally:
            await close_channels(app)
        restarted = RuntimeApplication(settings, config(channels={"qq": channel_specs()["qq"]}))
        sent = []
        try:
            async def send(saved_event, text):
                sent.append((saved_event, text))
                return DeliveryReceipt("sent", ("retry-receipt",))
            restarted.channels["qq_onebot", "default"].send = send
            assert restarted.inboxes["default"].retry_delivery(event.dedup_key)
            await drain(restarted, lambda: len(sent) == 1)
            assert sent[0][1] == "Original saved answer" and len(backend.requests) == 1
            assert restarted.inboxes["default"].list()[0]["state"] == "sent"
        finally:
            await close_channels(restarted)
    asyncio.run(scenario())


def test_project_completion_routes_to_original_thread_once_after_restart(settings, monkeypatch, tmp_path):
    import assistant.agent.runtime.app as app_module
    backend = NativeProjectPiBackend()
    monkeypatch.setattr(app_module, "build_pi_backend", lambda route, data_dir: backend)
    workspace = tmp_path / "channel-project"
    workspace.mkdir()
    async def scenario():
        """Task completion becomes a saved channel reply without replaying coding."""
        app = RuntimeApplication(settings, config(channels={"slack": channel_specs()["slack"]}))
        try:
            event = app.channels["slack", "default"].decode_event(slack_payload())
            await app.ingest(event)
            inbox = app.inboxes["default"]
            claimed = inbox.claim()
            inbox.finish(claimed["id"], "sent", reply="Task accepted")
            kernel = app.kernel()
            kernel.projects.register("orion", workspace, verification=[[sys.executable, "-c", "from answer import VALUE; assert VALUE == 42"]])
            queued = kernel.projects.submit("orion", "Write answer.py", session=event.session_key)
            completed = kernel.projects.run(queued["id"])
            assert completed["status"] == "done" and completed["verified"] is True
            app.publish_completions(kernel)
            app.publish_completions(kernel)
            assert len(inbox.list()) == 2
        finally:
            await close_channels(app)
        restarted = RuntimeApplication(settings, config(channels={"slack": channel_specs()["slack"]}))
        sent = []
        try:
            restarted.publish_completions(restarted.kernel())
            assert len(restarted.inboxes["default"].list()) == 2
            async def send(result_event, text):
                sent.append((result_event, text))
                return DeliveryReceipt("sent", ("completion-receipt",))
            restarted.channels["slack", "default"].send = send
            await drain(restarted, lambda: len(sent) == 1)
            assert sent[0][0].thread_id == event.thread_id
            assert sent[0][0].room_id == event.room_id
            assert queued["id"] in sent[0][1] and "Verified: True" in sent[0][1]
            assert len(backend.requests) == 1
            assert {row["state"] for row in restarted.inboxes["default"].list()} == {"sent"}
        finally:
            await close_channels(restarted)
    asyncio.run(scenario())
