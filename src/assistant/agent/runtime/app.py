"""Composition root for the new kernel, durable channel inbox and project workers."""

import asyncio
from dataclasses import asdict, replace
import logging
from pathlib import Path
import threading

from assistant.platform.config import DEFAULT_UID, Settings
from assistant.platform.registry import UserRegistry
from assistant.platform.runtime.channels import QQChannel, QQConfig, SlackChannel, SlackConfig
from assistant.platform.runtime.config import RuntimeConfig, load_runtime_config
from assistant.platform.runtime.contracts import ChannelEvent
from assistant.platform.runtime.inbox import Inbox
from assistant.platform.runtime.pi import build_pi_backend
from assistant.agent.runtime.kernel import Kernel

log = logging.getLogger("assistant.runtime")


class RuntimeApplication:
    """Own process lifecycle; adapters know neither personal tools nor tenancy."""

    def __init__(self, settings: Settings, config: RuntimeConfig | None = None):
        """Resolve configuration once; model calls happen only when work arrives."""
        self.config = config or load_runtime_config(settings)
        environment = Settings().model_dump()
        self._overrides = {key: value for key, value in settings.model_dump().items()
                           if value != environment.get(key)}
        self.settings = settings.model_copy(update={
            "runtime_backend": self.config.backend, "runtime_config": self.config.path})
        self._kernels: dict[str, Kernel] = {}
        self._kernel_lock = threading.Lock()
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.channels = {}
        self.inboxes = {}
        self._channel_ok = {}
        self._failed = False
        for name, spec in self.config.channels.items():
            if name == "slack":
                channel = SlackChannel(SlackConfig(**spec))
            elif name in ("qq", "qq_onebot"):
                channel = QQChannel(QQConfig(**spec))
            else:
                raise ValueError(f"unknown channel type {name!r}")
            owner = channel.config.owner_id
            scoped = self.scoped_settings(owner)
            self.channels[("slack" if name == "slack" else "qq_onebot",
                           channel.config.account_id)] = channel
            channel.set_acceptor(self.ingest)
            self.inboxes.setdefault(owner, Inbox(scoped.data_dir))
            self._channel_ok[name + "_configured_ok"] = True

    def refresh_settings(self) -> Settings:
        """Reload credentials while pinning this process's identity and config root."""
        fresh = Settings()
        return fresh.model_copy(update={**self._overrides,
            "data_dir": self.settings.data_dir, "uid": self.settings.uid,
            "deployment_mode": self.settings.deployment_mode,
            "serve_port": self.settings.serve_port,
            "runtime_config": self.config.path, "runtime_backend": self.config.backend,
            "runtime_allow_unrestricted": self.settings.runtime_allow_unrestricted})

    def scoped_settings(self, owner: str) -> Settings:
        """Resolve owner identity from trusted configuration and the active roster."""
        base = self.refresh_settings()
        if base.deployment_mode != "multi_tenant":
            if owner != DEFAULT_UID:
                raise ValueError("single-user channels must use owner_id='default'")
            return base
        user = UserRegistry(base.data_dir).get(owner)
        if not user or user.get("status") != "active":
            raise ValueError("channel owner is not an active registered user")
        return Settings.for_user(owner, base=base)

    def kernel(self, owner: str = DEFAULT_UID, *, scoped: Settings | None = None) -> Kernel:
        """Cache runtime state per owner, never a shared personal-memory context."""
        scoped = scoped or self.scoped_settings(owner)
        with self._kernel_lock:
            route = (load_runtime_config(scoped, path=self.config.path,
                                         backend=self.config.backend).route()
                     if self.config.path is not None else self.config.route())
            backend = build_pi_backend(route, scoped.data_dir)
            if owner not in self._kernels:
                settings = scoped
                kernel = Kernel(settings, backend, max_steps=self.config.max_steps,
                                timeout=self.config.timeout)
                self._kernels[owner] = kernel
                if settings.deployment_mode == "single_user":
                    for name, spec in self.config.projects.items():
                        kernel.projects.register(name, Path(spec["path"]),
                                                  verification=spec.get("verification"))
            else:
                kernel = self._kernels[owner]
                kernel.settings = scoped
                kernel.memory.settings = scoped
                kernel.skills.settings = scoped
                kernel.projects.settings = scoped
                kernel.backend = backend
                kernel.projects.backend = backend
            return self._kernels[owner]

    def handle_turn(self, text, settings, *, session, history=None,
                    image_paths=None, rejected_images=None):
        """Adapt the new kernel to existing HTTP/email delivery and image gates."""
        from assistant.agent.chat.agent import TurnResult
        kernel = self.kernel(settings.uid, scoped=settings)
        key = "bridge:" + session
        if history:
            kernel.seed_history(key, history)
        result = kernel.turn(text, session=key, cancel=self.stop,
                             image_paths=image_paths, rejected_images=rejected_images)
        outcome = ("success" if result.status == "done" else
                   "neutral" if result.status == "awaiting_approval" else "fail")
        return TurnResult(result.text, outcome)

    async def ingest(self, event: ChannelEvent) -> None:
        """Persist a trusted envelope promptly; no provider call blocks ingestion."""
        if (event.channel, event.account_id) not in self.channels:
            raise ValueError("event does not belong to a configured adapter")
        channel = self.channels[event.channel, event.account_id]
        if channel.config.owner_id != event.owner_id:
            raise ValueError("event owner does not match configured adapter")
        self.scoped_settings(event.owner_id)  # disabled owners are refused immediately
        self.inboxes[event.owner_id].put(event)

    async def _inbox_worker(self):
        """Run independent conversations and send stored replies exactly once per claim."""
        import json
        while not self.stop.is_set():
            worked = False
            for owner, inbox in list(self.inboxes.items()):
                delivery = inbox.claim(delivery=True)
                if delivery:
                    worked = True
                    event = ChannelEvent(**json.loads(delivery["envelope"]))
                    try:
                        self.scoped_settings(owner)
                        channel = self.channels[event.channel, event.account_id]
                        receipt = await channel.send(event, delivery["reply"])
                        inbox.finish(delivery["id"], receipt.status, receipt=asdict(receipt),
                                     error=receipt.error)
                    except asyncio.CancelledError:
                        inbox.finish(delivery["id"], "unknown", error="shutdown during delivery")
                        raise
                    except Exception as exc:
                        inbox.finish(delivery["id"], "unknown", error=type(exc).__name__)
                    continue
                pending = inbox.claim()
                if pending:
                    worked = True
                    event = ChannelEvent(**json.loads(pending["envelope"]))
                    try:
                        kernel = self.kernel(owner)
                        result = await asyncio.to_thread(
                            kernel.turn, event.text, session=event.session_key, cancel=self.stop)
                        inbox.finish(pending["id"], "ready", reply=result.text)
                    except asyncio.CancelledError:
                        inbox.finish(pending["id"], "interrupted", error="shutdown during turn")
                        raise
                    except Exception as exc:
                        inbox.finish(pending["id"], "ready", reply=(
                            f"The assistant could not complete this turn ({type(exc).__name__}). "
                            "No success is claimed; inspect the local runtime logs."),
                            error=type(exc).__name__)
                        log.error("channel turn failed: %s", type(exc).__name__)
            if not worked:
                await asyncio.sleep(0.2)

    async def _project_worker(self):
        """Drain durable queues independently of conversational turn execution."""
        while not self.stop.is_set():
            worked = False
            for kernel in list(self._kernels.values()):
                try:
                    scoped = self.scoped_settings(kernel.settings.uid)
                    kernel = self.kernel(kernel.settings.uid, scoped=scoped)
                except ValueError:
                    continue
                pending = kernel.projects.next_queued()
                if pending:
                    worked = True
                    result = await asyncio.to_thread(kernel.projects.run, pending["id"])
                    kernel.remember_project(result)
                    # A complete report remains queryable after restart and is added
                    # to the originating conversation without inventing a delivery.
                    kernel.store.add_message(result["session"], "assistant",
                        f"Project task {result['id']}: {result['status']}\n{result['report']}",
                        origin="external_result")
                self.publish_completions(kernel)
            if not worked:
                await asyncio.sleep(0.3)

    def publish_completions(self, kernel: Kernel) -> None:
        """Reconcile task completion to saved channel replies without redoing work."""
        inbox = self.inboxes.get(kernel.settings.uid)
        if inbox is None:
            return
        for task in kernel.store.runs(limit=1000):
            if task["status"] not in {"done", "failed", "cancelled", "interrupted"}:
                continue
            kernel.remember_project(task)
            if any(e["kind"] == "channel_completion_queued" for e in kernel.store.events(task["id"])):
                continue
            text = (f"Project task {task['id']}: {task['status']}\n"
                    f"Verified: {task['verified']}\n{task['report'] or task['error']}")
            if inbox.put_reply(task["session"], task["id"], text):
                kernel.store.record_event(task["id"], "channel_completion_queued", {})

    async def _run_async(self):
        """Pair every adapter/worker start with cancellation and connection cleanup."""
        self.loop = asyncio.get_running_loop()
        tasks = [asyncio.create_task(channel.run(self.ingest), name=key[0])
                 for key, channel in self.channels.items()]
        tasks += [asyncio.create_task(self._inbox_worker()) for _ in range(self.config.workers)]
        tasks += [asyncio.create_task(self._project_worker()) for _ in range(self.config.workers)]
        try:
            while not self.stop.is_set():
                if self.settings.deployment_mode == "multi_tenant":
                    active = set(UserRegistry(self.settings.data_dir).active())
                    for owner, kernel in list(self._kernels.items()):
                        if owner not in active:
                            for task in kernel.store.runs(limit=1000):
                                if task["status"] in {"queued", "running"}:
                                    kernel.projects.cancel(task["id"])
                for task in tasks:
                    if task.done() and not task.cancelled():
                        error = task.exception()
                        if error is not None:
                            raise RuntimeError(f"runtime worker {task.get_name()} stopped") from error
                await asyncio.sleep(0.2)
        finally:
            self.stop.set()
            self._cancel_projects()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.gather(*(c.close() for c in self.channels.values()), return_exceptions=True)

    def start(self):
        """Start only after the outer daemon holds exclusive process ownership."""
        if self.thread and self.thread.is_alive():
            return
        owners = ({c.config.owner_id for c in self.channels.values()}
                  or ({DEFAULT_UID} if self.settings.deployment_mode == "single_user" else set()))
        if self.settings.deployment_mode == "multi_tenant":
            owners |= set(UserRegistry(self.settings.data_dir).active())
        for owner in owners:
            self.kernel(owner).projects.recover()
        for inbox in self.inboxes.values():
            inbox.recover()
        self.stop.clear()
        self._failed = False
        def target():
            """A failed transport worker is visible through the health endpoint."""
            try:
                asyncio.run(self._run_async())
            except Exception as exc:
                self._failed = True
                log.error("native channel runtime stopped: %s", type(exc).__name__)
        self.thread = threading.Thread(target=target, name="agent-runtime", daemon=True)
        self.thread.start()

    def close(self):
        """Stop provider work and request cancellation of every active project."""
        self.stop.set()
        self._cancel_projects()
        if self.thread:
            self.thread.join(timeout=10)

    def _cancel_projects(self):
        """Stop thread-backed project work before cancelling asyncio wrappers."""
        for kernel in list(self._kernels.values()):
            try:
                for task in kernel.store.runs(status="running", limit=1000):
                    kernel.projects.cancel(task["id"])
            except Exception as exc:
                log.error("project cleanup failed: %s", type(exc).__name__)

    def health(self) -> dict:
        """Readiness reflects worker liveness as well as configuration validity."""
        return {**self._channel_ok,
                **{f"{kind}_{account}_connected_ok": channel.connected
                   for (kind, account), channel in self.channels.items()},
                "runtime_workers_ok": (self.thread is not None and self.thread.is_alive()
                                       and not self._failed and not self.stop.is_set())}

    def serve(self) -> int:
        """Keep the existing loopback bridge, onboarding and schedules operational."""
        from assistant.agent.app import build_services
        from assistant.platform.serve import run_serve
        legacy = build_services()
        def start():
            """Compose both channel lifecycles under the existing daemon PID lock."""
            legacy.startup_channels()
            self.start()
        def stop():
            """One failing teardown must not leave the other channel system alive."""
            try:
                self.close()
            finally:
                legacy.teardown_channels()
        def action(name, params, settings):
            """Direct bridge actions share the same effect policy as model tools."""
            kernel = self.kernel(settings.uid, scoped=settings)
            actual = "daily" if name == "trigger_run" else name
            arguments = {} if name == "trigger_run" else params
            if kernel.skills.requires_approval(actual, arguments):
                raise ValueError("this outward action requires approval through chat /approve")
            return kernel.skills.execute(actual, arguments).text
        services = replace(legacy, handle_runtime_turn=self.handle_turn,
                           runtime_settings_factory=self.refresh_settings,
                           resolve_runtime_settings=self.scoped_settings,
                           run_action=action, startup_channels=start, teardown_channels=stop,
                           channel_health=lambda: {**legacy.channel_health(), **self.health()})
        return run_serve(self.settings, services)
