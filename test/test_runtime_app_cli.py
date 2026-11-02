"""Runtime application journeys use real stores and fake provider/transport edges."""

import asyncio
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
from assistant.platform.runtime.store import RuntimeStore


class ScriptedBackend:
    """Capture real composition requests without invoking a model or subprocess."""

    kind = "scripted"
    capabilities = frozenset({"tools"})

    def __init__(self, *answers):
        """Accept completions or callbacks that inspect the real backend request."""
        self.answers = list(answers)
        self.requests = []

    def complete(self, request):
        """Capture independent snapshots before the tool loop mutates history."""
        self.requests.append({"messages": copy.deepcopy(request.messages), "system": request.system,
                              "cwd": request.cwd, "allow_write": request.allow_write})
        if not self.answers:
            raise AssertionError("unexpected provider execution")
        answer = self.answers.pop(0)
        return answer(request) if callable(answer) else answer


class NativeProjectBackend(ScriptedBackend):
    """Only the native edit is fake; host verification executes real Python code."""

    kind = "native-fixture"
    capabilities = frozenset({"native_agent"})

    def __init__(self):
        """Use one predictable project edit for repeatable CLI acceptance."""
        super().__init__(self.edit)

    @staticmethod
    def edit(request):
        """Honor the host's explicit write request in its registered workspace."""
        assert request.allow_write is True
        (request.cwd / "answer.py").write_text("VALUE = 42\n")
        return Completion(text="Created answer.py; the host must verify it.", session_id="native-fixture-session")


def config(**updates):
    """Resolve a fixture backend route through the real configuration contract."""
    return RuntimeConfig(backend="fixture", backends={"fixture": BackendConfig(kind="codex")}, **updates)


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
    route_file.write_text('[runtime]\nbackend = "fixture"\n[backends.fixture]\nkind = "codex"\n')
    monkeypatch.setattr(runtime_cli, "Settings", lambda: settings)
    monkeypatch.setattr(app_module, "build_backend", lambda route: backend)
    return runtime_cli, ["--data-dir", str(settings.data_dir), "--config", str(route_file)]


def test_application_restart_retains_history_and_sourced_memory(settings, monkeypatch):
    import assistant.agent.runtime.app as app_module
    first_backend = ScriptedBackend(Completion(tool_calls=[ToolCall("remember", "memory_remember", {
        "text": "Orion uses SQLite", "project": "orion"})]), Completion(text="Saved Orion's decision."))
    second_backend = ScriptedBackend(Completion(text="Orion uses SQLite, from your earlier message."))
    backends = iter([first_backend, second_backend])
    monkeypatch.setattr(app_module, "build_backend", lambda route: next(backends))
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
    monkeypatch.setattr(app_module, "build_backend", lambda route: ScriptedBackend())
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


def test_cli_skill_listing_mutation_and_exact_approval(settings, monkeypatch, tmp_path, capsys):
    from assistant.agent import orchestrator
    backend = ScriptedBackend()
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
    backend = NativeProjectBackend()
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
    backend = ScriptedBackend(Completion(text="Saved conversation in the chosen root."))
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


def test_cli_doctor_only_calls_a_model_when_probe_is_explicit(settings, monkeypatch, tmp_path, capsys):
    backend = ScriptedBackend(Completion(text="PERSONAL_AGENT_RUNTIME_OK"))
    cli, options = cli_setup(settings, monkeypatch, tmp_path, backend)
    monkeypatch.setattr(cli, "build_backend", lambda route: backend)
    monkeypatch.setattr(cli.shutil, "which", lambda command: "/fixture/bin/" + command)
    assert cli.main(["doctor", *options]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["backend"] == "fixture" and first["live_probe"] == "not_requested"
    assert backend.requests == []
    assert cli.main(["doctor", "--probe", *options]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["live_probe"] == "passed" and len(backend.requests) == 1
    assert backend.requests[0]["cwd"] == settings.data_dir


def test_slack_qq_ingest_kernel_receipt_and_restart_dedup(settings, monkeypatch):
    import assistant.agent.runtime.app as app_module
    backend = ScriptedBackend(Completion(text="Slack durable reply"), Completion(text="QQ durable reply"))
    monkeypatch.setattr(app_module, "build_backend", lambda route: backend)
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
    monkeypatch.setattr(app_module, "build_backend", lambda route: ScriptedBackend())
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
    backend = ScriptedBackend()
    monkeypatch.setattr(app_module, "build_backend", lambda route: backend)
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
    backend = ScriptedBackend(Completion(text="Original saved answer"))
    monkeypatch.setattr(app_module, "build_backend", lambda route: backend)
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
    backend = NativeProjectBackend()
    monkeypatch.setattr(app_module, "build_backend", lambda route: backend)
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
