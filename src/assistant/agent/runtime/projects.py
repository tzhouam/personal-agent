"""Durable personal projects using Pi sessions and independent verification.

Only owner-registered directories are executable projects. Pi receives confined
host file tools; a subprocess tool requires explicit unrestricted-mode
opt-in because a process cwd alone is not an operating-system sandbox.
"""

from __future__ import annotations

from pathlib import Path
from threading import Event, Lock, Thread
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import tempfile
import time

from assistant.platform.config import Settings
from assistant.platform.runtime.contracts import BackendRequest
from assistant.platform.runtime.pi import PiBackend
from assistant.platform.runtime.store import RuntimeStore, _now
from assistant.platform.secrets import mask_secrets


class ProjectCancelled(RuntimeError):
    """A durable cancellation stopped the backend or verification process."""


class ProjectTimeout(RuntimeError):
    """The Pi session's budget also bounds workspace tool execution."""


_TOOLS = [
    {"type": "function", "function": {"name": "read_file", "description": "Read a UTF-8 file in the registered workspace.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "write_file", "description": "Write a UTF-8 file inside the registered workspace.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "list_files", "description": "List files below a relative workspace directory.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "run_command", "description": "Run an argv array in the workspace. Requires explicit owner unrestricted-mode opt-in; no shell expansion.",
     "parameters": {"type": "object", "properties": {"argv": {"type": "array", "items": {"type": "string"}}, "timeout": {"type": "number"}}, "required": ["argv"], "additionalProperties": False}}},
]
_SYSTEM = """You execute a personal project in its explicitly registered workspace.
Implement the user's goal using the available workspace tools. Read existing files
before changing them. Do not publish, push, send messages, access unrelated data,
or claim a test passed without its output. The host runs the owner's configured
verification commands independently after you finish. Report changed artifacts,
checks actually run, and any remaining limitations. Stop when the task is done or
blocked; do not replace an unavailable tool or provider with a fabricated result.
"""


def _argv(value) -> list[str]:
    """Accept argv rather than shell strings, including for trusted verification."""
    if (not isinstance(value, (list, tuple)) or not value or len(value) > 128
            or any(not isinstance(part, str) or "\x00" in part or len(part) > 8192 for part in value)
            or not value[0].strip()):
        raise ValueError("command must be a nonempty argv array")
    return list(value)


class ProjectManager:
    """Scope project registration, queued work, verification and cancellation to an owner."""

    def __init__(self, settings: Settings, backend: PiBackend, store: RuntimeStore | None = None):
        """Reuse the kernel store while keeping model calls outside database transactions."""
        self.settings, self.backend = settings, backend
        self.store = store or RuntimeStore(settings.data_dir)
        self._active: dict[str, Event] = {}
        self._active_lock = Lock()

    def register(self, name: str, path: Path | str,
                 verification: list[list[str]] | None = None) -> dict:
        """Owner CLI only: trust an existing path and optional exact verification argv."""
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", name):
            raise ValueError("project name must be a short identifier")
        target = Path(path).expanduser().resolve(strict=True)
        if not target.is_dir():
            raise ValueError("project path must be an existing directory")
        roots = getattr(self.settings, "runtime_project_roots", [])
        if roots and not any(target.is_relative_to(Path(root).expanduser().resolve()) for root in roots):
            raise ValueError("project path is outside configured project roots")
        private = Path(self.settings.data_dir).resolve()
        if (target == private or target.is_relative_to(private)
                or private.is_relative_to(target)):
            raise ValueError("project workspace must not overlap the assistant's private data directory")
        commands = [_argv(command) for command in (verification or [])]
        if len(commands) > 20:
            raise ValueError("at most twenty verification commands are allowed")
        result = self.store.register_project(name, target, commands)
        if commands:
            result["verification_isolation"] = "trusted-host"
            result["warning"] = ("Configured verification executes project code on the host, "
                                 "outside the model backend's sandbox.")
        return result

    def list(self) -> list[dict]:
        """List previously registered projects; never discover paths from channel text."""
        return self.store.projects()

    def submit(self, project: str, goal: str, session: str = "cli") -> dict:
        """Persist the complete request before a background worker acknowledges it."""
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 100_000:
            raise ValueError("project goal must contain 1–100000 characters")
        return self.store.create_run(project, goal.strip(), session)

    def status(self, task_id: str) -> dict:
        """Return durable status and real evidence, not a model's claimed success."""
        result = self.store.run(task_id)
        if result is None:
            raise KeyError(task_id)
        return result

    def next_queued(self) -> dict | None:
        """Offer FIFO work; run() still atomically claims it to avoid duplicate workers."""
        queued = self.store.runs(status="queued", limit=100)
        busy = {item["workspace"] for item in self.store.runs(status="running", limit=1000)}
        return next((item for item in queued if item["workspace"] not in busy), None)

    def cancel(self, task_id: str) -> dict:
        """Wake a local backend and persist cancellation for workers in other processes."""
        result = self.store.cancel_run(task_id)
        with self._active_lock:
            event = self._active.get(task_id)
            if event is not None:
                event.set()
        self.store.record_event(task_id, "cancel_requested" if result["cancel_requested"]
                                else "cancel_ignored", {"status": result["status"]})
        return result

    def recover(self) -> int:
        """Call only after acquiring the daemon's exclusive lifetime lock."""
        return self.store.recover_runs()

    def _check(self, task_id: str, cancel: Event) -> None:
        """Combine in-process cancellation with the durable cross-process request."""
        record = self.status(task_id)
        if cancel.is_set() or record["cancel_requested"]:
            cancel.set()
            raise ProjectCancelled("project cancelled")

    def run(self, task_id: str) -> dict:
        """Execute once in the frozen workspace and independently verify the result."""
        task = self.status(task_id)
        if not self.store.claim_run(task_id):
            return self.status(task_id)
        cancel, finished = Event(), Event()
        with self._active_lock:
            self._active[task_id] = cancel

        def watch_cancel():
            """Wake a bounded subprocess when another CLI requests cancellation."""
            while not finished.wait(0.15):
                if self.status(task_id)["cancel_requested"]:
                    cancel.set()
                    return

        watcher = Thread(target=watch_cancel, name=f"cancel-{task_id}", daemon=True)
        watcher.start()
        self.store.record_event(task_id, "started", {"backend": self.backend.kind})
        workspace = Path(task["workspace"])
        evidence: list[dict] = []
        try:
            if not workspace.is_dir() or str(workspace.resolve()) != task["workspace"]:
                raise ValueError("registered workspace moved or became a symlink; register it again")
            self._check(task_id, cancel)
            before = self._snapshot(workspace)
            report, session_id = self._run_session(task, workspace, cancel)
            self._check(task_id, cancel)
            for command in task["verification"]:
                self._check(task_id, cancel)
                item = self._command(workspace, _argv(command), cancel, timeout=120)
                item["kind"] = "verification"
                evidence.append(item)
                self.store.record_event(task_id, "verification", item)
                if item["returncode"] != 0:
                    artifacts = self._changed_artifacts(workspace, before)
                    result = self.store.update_run(
                        task_id, status="failed", report=report, error="configured verification failed",
                        artifacts=artifacts, evidence=evidence, backend_session_id=session_id,
                        finished_at=_now(), verified=False)
                    self.store.record_event(task_id, result["status"], {"error": result["error"]})
                    return result
            self._check(task_id, cancel)
            artifacts = self._changed_artifacts(workspace, before)
            verified = bool(task["verification"])
            result = self.store.update_run(
                task_id, status="done", report=report, artifacts=artifacts, evidence=evidence,
                verified=verified, evidence_path="", backend_session_id=session_id,
                finished_at=_now())
            if result["status"] == "done" and verified:
                # Commit the terminal verdict before writing its attestation. A late
                # cancellation then cannot leave proof claiming a cancelled run succeeded.
                evidence_path = self._write_evidence(task, artifacts, evidence)
                result = self.store.update_run(task_id, evidence_path=evidence_path)
            self.store.record_event(task_id, result["status"], {
                "verified": result["verified"], "evidence_path": result["evidence_path"],
                "artifacts": artifacts})
            return result
        except Exception as exc:
            cancelled = cancel.is_set() or self.status(task_id)["cancel_requested"]
            result = self.store.update_run(
                task_id, status="cancelled" if cancelled else "failed",
                error=mask_secrets(str(exc))[:2000], evidence=evidence, verified=False,
                finished_at=_now())
            self.store.record_event(task_id, result["status"], {"error": result["error"]})
            return result
        finally:
            finished.set()
            watcher.join(timeout=1)
            with self._active_lock:
                self._active.pop(task_id, None)

    def _run_session(self, task: dict, workspace: Path, cancel: Event) -> tuple[str, str]:
        """Let Pi own model turns while Python authorizes each workspace tool RPC."""
        steps = min(max(int(getattr(self.settings, "runtime_project_max_steps", 20)), 1), 100)
        backend = self.backend
        project_timeout = float(getattr(self.settings, "runtime_project_timeout", 300))
        timeout = min(project_timeout, float(getattr(getattr(backend, "config", None),
                                                     "timeout", project_timeout)))
        deadline = time.monotonic() + timeout

        def check_session():
            """Refuse effects and successful completion after the session budget expires."""
            self._check(task["id"], cancel)
            if time.monotonic() >= deadline:
                raise ProjectTimeout("Pi project session timed out")

        def dispatch(name: str, arguments: dict):
            """Keep workspace confinement, cancellation and receipts at the host boundary."""
            check_session()
            try:
                result = self._tool(workspace, name, arguments, cancel, deadline=deadline)
                check_session()
                self.store.record_event(task["id"], "tool", {"name": name, "ok": True})
            except (ValueError, OSError, RuntimeError) as exc:
                if isinstance(exc, (ProjectCancelled, ProjectTimeout)):
                    raise
                result = {"ok": False, "error": mask_secrets(str(exc))[:1000]}
                self.store.record_event(task["id"], "tool", {
                    "name": name, "ok": False, "error": result["error"]})
            check_session()
            return result

        result = backend.run_session(BackendRequest(
            messages=[{"role": "user", "content": task["goal"]}],
            system=_SYSTEM, tools=_TOOLS, cwd=workspace, cancel=cancel,
            timeout=timeout),
            dispatch, session="project:" + task["id"], max_steps=steps)
        check_session()
        if result.status != "done":
            raise RuntimeError(result.error or f"Pi session did not complete: {result.status}")
        if not result.text.strip():
            raise RuntimeError("Pi completed without a project report")
        return result.text, result.session_id

    def _path(self, workspace: Path, value: str) -> Path:
        """Reject traversal, absolute names and links escaping the workspace."""
        if not isinstance(value, str) or len(value) > 4096 or "\x00" in value:
            raise ValueError("invalid workspace-relative path")
        path = Path(value)
        if path.is_absolute():
            raise ValueError("use a workspace-relative path")
        resolved = (workspace / path).resolve()
        if not resolved.is_relative_to(workspace):
            raise ValueError("path escapes the registered workspace")
        if ".git" in path.parts or ".git" in resolved.relative_to(workspace).parts:
            raise ValueError("git metadata is not an editable project artifact")
        return resolved

    def _tool(self, workspace: Path, name: str, args: dict, cancel: Event,
              *, deadline: float | None = None):
        """Dispatch only fixed host tools; Markdown and model names never import code."""
        if not isinstance(args, dict):
            raise ValueError("tool arguments must be an object")
        if name == "read_file":
            path = self._path(workspace, args.get("path", ""))
            metadata = path.stat()
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("read_file requires a regular file")
            if metadata.st_size > 256_000:
                raise ValueError("file exceeds the 256KB read limit")
            return {"ok": True, "path": str(path.relative_to(workspace)), "content": path.read_text()}
        if name == "write_file":
            path = self._path(workspace, args.get("path", ""))
            content = args.get("content")
            if not isinstance(content, str) or len(content.encode()) > 256_000:
                raise ValueError("content must be UTF-8 text no larger than 256KB")
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as tmp:
                tmp.write(content)
                staged = Path(tmp.name)
            staged.replace(path)
            return {"ok": True, "path": str(path.relative_to(workspace)),
                    "sha256": hashlib.sha256(content.encode()).hexdigest()}
        if name == "list_files":
            path = self._path(workspace, args.get("path", "."))
            if not path.is_dir():
                raise ValueError("list_files requires a directory")
            entries = []
            for item in sorted(path.iterdir()):
                if item.name == ".git" or item.is_symlink():
                    continue
                entries.append({"path": str(item.relative_to(workspace)), "directory": item.is_dir()})
                if len(entries) >= 200:
                    break
            return {"ok": True, "files": entries}
        if name == "run_command":
            if not getattr(self.settings, "runtime_allow_unrestricted", False):
                raise ValueError("project commands require explicit RUNTIME_ALLOW_UNRESTRICTED=true; cwd alone is not a sandbox")
            timeout = min(max(float(args.get("timeout", 60)), 1), 120)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProjectTimeout("Pi project session timed out")
                timeout = min(timeout, remaining)
            return self._command(workspace, _argv(args.get("argv")), cancel, timeout)
        raise ValueError(f"unknown project tool: {name}")

    def _command(self, workspace: Path, argv: list[str], cancel: Event,
                 timeout: float) -> dict:
        """Run bounded argv and terminate its process group on cancellation or timeout."""
        started = time.monotonic()
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            process = subprocess.Popen(argv, cwd=workspace, stdout=stdout, stderr=stderr,
                                       start_new_session=(os.name != "nt"))
            reason = ""
            while process.poll() is None:
                if cancel.wait(0.05):
                    reason = "cancelled"
                    break
                if time.monotonic() - started >= timeout:
                    reason = "command timeout"
                    break
                if stdout.tell() + stderr.tell() > 2_000_000:
                    reason = "command output limit exceeded"
                    break
            if reason:
                if os.name != "nt":
                    # A leader exiting after SIGTERM does not mean its children
                    # stopped. Kill the entire group after a short grace even if
                    # wait() already reaped the leader (a resistant child may remain).
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=0.2)
                    except subprocess.TimeoutExpired:
                        pass
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=3)
                else:
                    process.terminate()
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)
                if reason == "cancelled":
                    raise ProjectCancelled("project cancelled")
                raise RuntimeError(reason)
            stdout.seek(max(0, stdout.tell() - 32_000))
            stderr.seek(max(0, stderr.tell() - 16_000))
            return {"argv": argv, "returncode": process.returncode,
                    "isolation": "trusted-host",
                    "stdout": mask_secrets(stdout.read().decode(errors="replace")),
                    "stderr": mask_secrets(stderr.read().decode(errors="replace")),
                    "duration_s": round(time.monotonic() - started, 3)}

    def _snapshot(self, workspace: Path) -> dict[str, str]:
        """Bound artifact scanning and omit VCS/dependency trees and outside symlinks."""
        found: dict[str, str] = {}
        for directory, dirs, files in os.walk(workspace, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in {".git", ".venv", "node_modules", "__pycache__"}
                             and not (Path(directory) / d).is_symlink())
            for filename in sorted(files):
                path = Path(directory) / filename
                if path.is_symlink() or not path.is_file() or path.stat().st_size > 2_000_000:
                    continue
                found[str(path.relative_to(workspace))] = hashlib.sha256(path.read_bytes()).hexdigest()
                if len(found) >= 5000:
                    return found
        return found

    def _changed_artifacts(self, workspace: Path, before: dict[str, str]) -> list[dict]:
        """Record independently observed additions, edits and deletions with hashes."""
        after = self._snapshot(workspace)
        result = [{"path": path, "sha256": digest, "change": "modified" if path in before else "added"}
                  for path, digest in after.items() if before.get(path) != digest]
        result.extend({"path": path, "change": "deleted"} for path in before.keys() - after.keys())
        return result

    def _write_evidence(self, task: dict, artifacts: list[dict], evidence: list[dict]) -> str:
        """Create a private attestation only after every registered check succeeded."""
        directory = Path(self.settings.data_dir) / "runtime" / "project-evidence"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (task["id"] + ".json")
        payload = {"run_id": task["id"], "project": task["project"], "verified": True,
                   "workspace": task["workspace"], "completed_at": _now(),
                   "artifacts": artifacts, "checks": evidence,
                   "verification_isolation": "trusted-host"}
        with tempfile.NamedTemporaryFile("w", dir=directory, delete=False, encoding="utf-8") as tmp:
            tmp.write(mask_secrets(json.dumps(payload, indent=2, ensure_ascii=False)))
            staged = Path(tmp.name)
        staged.chmod(0o600)
        staged.replace(path)
        return str(path)
