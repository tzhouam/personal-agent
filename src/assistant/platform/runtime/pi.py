"""Actual Pi SDK transport; Python dispatches tools but never controls its loop."""

from contextlib import contextmanager
from dataclasses import dataclass, field, replace
import fcntl
import base64
import hashlib
import json
import math
import mimetypes
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
import uuid

from assistant.platform.runtime.backends import BackendCancelled, BackendError, CLIBackend, _stop_process
from assistant.platform.runtime.contracts import BackendConfig, BackendRequest, Completion
from assistant.platform.secrets import mask_secrets

SDK_VERSION = "1.4.0"
_ASSETS = Path(__file__).with_name("pi_host")
_CLI_PROVIDERS = {"codex", "claude", "gemini"}
_MAX_WIRE = 16 * 1024 * 1024


@dataclass
class PiResult:
    """A finalized Pi turn, including durable context and an explicit pause state."""

    status: str
    text: str = ""
    session_file: str = ""
    session_id: str = ""
    usage: dict = field(default_factory=dict)
    error: str = ""
    approval_id: str = ""
    stop_reason: str = ""
    error_code: str = ""
    status_code: int | None = None


class PausePi(Exception):
    """A host effect guard pauses Pi before another tool can execute."""

    def __init__(self, approval_id: str, text: str):
        """Carry a receipt, never an implicit execution permission."""
        super().__init__(text)
        self.approval_id, self.text = approval_id, text


def _assets() -> list[Path]:
    """Only ship reviewed host sources and exact dependency metadata."""
    return [_ASSETS / "package.json", _ASSETS / "package-lock.json",
            *_source_assets()]


def _source_assets() -> list[Path]:
    """Required entrypoints cannot disappear from the readiness check after packaging."""
    return sorted({_ASSETS / "src" / "host.mjs", _ASSETS / "src" / "session.mjs",
                   *(_ASSETS / "src").glob("*.mjs")})


def _private_directory(base: Path, *parts: str) -> Path:
    """Refuse symlink redirects beneath the explicitly configured owner data root."""
    current = Path(base).resolve()
    current.mkdir(parents=True, exist_ok=True, mode=0o700)
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Pi private directories must not be symlinks")
        current.mkdir(exist_ok=True, mode=0o700)
        current.chmod(0o700)
    return current


def _node_status() -> tuple[bool, str]:
    """Check the installed runtime version without touching a model or credential."""
    executable = shutil.which("node")
    if not executable:
        return False, ""
    try:
        result = subprocess.run([executable, "--version"], capture_output=True,
                                text=True, timeout=5, check=False)
        version = result.stdout.strip().removeprefix("v")
        numbers = tuple(int(part) for part in version.split(".")[:3])
        return result.returncode == 0 and len(numbers) == 3 and numbers >= (22, 19, 0), version
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False, ""


def pi_status(data_dir: Path) -> dict:
    """Report exact SDK/source availability without running a model or credentials."""
    node_ok, node_version = _node_status()
    result = {"available": False, "version": SDK_VERSION, "entry": "", "assets_ok": False,
              "node_ok": node_ok, "node_version": node_version,
              "npm_available": bool(shutil.which("npm")),
              "fix": "assistant pi install --data-dir " + str(data_dir)}
    assets = _assets()
    if not all(path.is_file() and not path.is_symlink() for path in assets):
        return result
    result["assets_ok"] = True
    installed = Path(data_dir) / "pi" / "host"
    for root in (installed, _ASSETS):
        if root.is_symlink():
            continue
        package = root / "node_modules" / "@earendil-works" / "pi-coding-agent" / "package.json"
        try:
            if not package.is_file() or json.loads(package.read_text())["version"] != SDK_VERSION:
                continue
            # A same-version cached host must not silently run stale application code.
            if root != _ASSETS and any((root / path.relative_to(_ASSETS)).is_symlink()
                    or (root / path.relative_to(_ASSETS)).read_bytes() != path.read_bytes()
                    for path in assets):
                continue
            result.update(available=node_ok, entry=str(root / "src" / "host.mjs"))
            return result
        except (OSError, ValueError, KeyError):
            continue
    return result


def install_pi(data_dir: Path) -> dict:
    """Explicitly install pinned SDK dependencies without package lifecycle scripts."""
    if not _node_status()[0] or not shutil.which("npm"):
        raise BackendError("Pi requires Node >=22.19 and npm")
    target = _private_directory(Path(data_dir), "pi", "host")
    for source in _assets():
        relative = source.relative_to(_ASSETS)
        parent = _private_directory(target, *relative.parts[:-1])
        destination = parent / relative.name
        if not source.is_file() or source.is_symlink() or destination.is_symlink():
            raise ValueError("Pi host assets must be regular files")
        shutil.copyfile(source, destination)
        destination.chmod(0o600)
    try:
        proc = subprocess.run(["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
                              cwd=target, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackendError("Pi dependency installation failed; check Node/npm and network access") from exc
    if proc.returncode:
        raise BackendError("Pi dependency installation failed; check Node/npm and network access")
    return pi_status(data_dir)


def _check_request(request: BackendRequest, deadline: float) -> None:
    """Apply cancellation and the complete turn deadline, including lock waits."""
    if request.cancel and request.cancel.is_set():
        raise BackendCancelled("Pi session cancelled")
    if time.monotonic() >= deadline:
        raise BackendError("Pi session timed out")


@contextmanager
def _session_lock(path: Path, request: BackendRequest, deadline: float):
    """Serialize a transcript without an unbounded, uncancellable flock wait."""
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise BackendError("cannot open private Pi session lock") from exc
    acquired = False
    try:
        os.fchmod(descriptor, 0o600)
        while not acquired:
            _check_request(request, deadline)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                if request.cancel:
                    request.cancel.wait(0.05)
                else:
                    threading.Event().wait(0.05)
        yield
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _images(paths: list[str | dict]) -> list[dict]:
    """Encode bounded local images explicitly; never fetch URLs or silently drop them."""
    if len(paths) > 8:
        raise ValueError("Pi supports at most eight images per turn")
    total = 0
    encoded = []
    for item in paths:
        if isinstance(item, dict):
            if set(item) != {"type", "data", "mimeType"} or item.get("type") != "image":
                raise ValueError("invalid inline Pi image")
            mime = item["mimeType"]
            try:
                if len(item["data"]) > 14 * 1024 * 1024:
                    raise ValueError("Pi image payload is too large")
                raw = base64.b64decode(item["data"], validate=True)
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid base64 Pi image") from exc
        else:
            path = Path(item)
            mime = mimetypes.guess_type(str(path))[0]
            with path.open("rb") as handle:
                raw = handle.read(10 * 1024 * 1024 + 1 - total)
        if mime not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
            raise ValueError("Pi supports PNG, JPEG, WebP and GIF images")
        total += len(raw)
        if not raw or total > 10 * 1024 * 1024:
            raise ValueError("Pi image payload must be nonempty and at most 10 MiB")
        encoded.append({"type": "image", "mimeType": mime,
                        "data": base64.b64encode(raw).decode("ascii")})
    return encoded


def _safe(value):
    """Redact every persisted prompt/tool-result value before it enters Pi JSONL."""
    if isinstance(value, str):
        return mask_secrets(value)
    if isinstance(value, list):
        return [_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: _safe(item) for key, item in value.items()}
    return value


class PiBackend:
    """One configured model route running inside Pi's actual AgentSession."""

    kind = "pi"
    capabilities = frozenset({"pi_session", "tools"})

    def __init__(self, config: BackendConfig, data_dir: Path):
        """Scope all Node dependencies and Pi sessions to the owner's data root."""
        aliases = {"openai-compatible": "openai", "anthropic-compatible": "anthropic", "claudecode": "claude"}
        self.config = replace(config, kind=aliases.get(config.kind, config.kind))
        self.data_dir = Path(data_dir).resolve()

    def complete(self, request: BackendRequest) -> Completion:
        """Plain model tasks also use Pi; arbitrary host tools require a dispatcher."""
        if request.tools:
            raise BackendError("Pi tool sessions require run_session with a host dispatcher")
        result = self.run_session(request, session="completion-" + uuid.uuid4().hex)
        if result.status != "done" and result.stop_reason != "length":
            error = BackendError(result.error or "Pi completion did not finish")
            error.status_code = result.status_code
            error.body = {"error": {"message": str(error), "type": result.error_code}}
            raise error
        return Completion(text=result.text, session_id=result.session_id,
                          usage=result.usage, finish_reason="length" if result.stop_reason == "length" else "stop")

    def _model(self, request: BackendRequest) -> dict:
        """Pass credentials only in process memory, never argv or session resources."""
        model = self.config.model
        if not model and self.config.kind in _CLI_PROVIDERS:
            model = self.config.kind + "-configured-model"
        if not model:
            raise BackendError("select an explicit Pi model in runtime.toml")
        return {"kind": self.config.kind, "model": model,
                "base_url": self.config.base_url or None,
                "api_key": self.config.api_key, "max_tokens": request.max_tokens,
                **({"api": self.config.api} if self.config.api else {}),
                **({"provider": self.config.provider} if self.config.provider else {}),
                "thinking_level": request.thinking_level or self.config.thinking_level or "off",
                "context_window": self.config.context_window}

    def run_session(self, request: BackendRequest, tool_handler=None, *, session: str,
                    history: list[dict] | None = None, context_messages: list[dict] | None = None,
                    max_steps: int = 12) -> PiResult:
        """Invoke Pi once; exchange individual authorized tool RPCs until it settles."""
        status = pi_status(self.data_dir)
        if not status["available"]:
            raise BackendError("Pi SDK is not installed; " + status["fix"])
        timeout = min(float(request.timeout), float(self.config.timeout))
        if not math.isfinite(timeout) or timeout <= 0:
            raise BackendError("Pi timeout must be positive")
        deadline = time.monotonic() + timeout
        _check_request(request, deadline)
        directory = _private_directory(self.data_dir, "pi", "sessions",
                                       hashlib.sha256(session.encode()).hexdigest())
        # Conversation ownership is separate from short personal-store write locks.
        with _session_lock(directory / "session.lock", request, deadline):
            manifest = directory / "session.json"
            if manifest.is_symlink():
                raise ValueError("Pi session manifest must not be a symlink")
            try:
                resumed = json.loads(manifest.read_text()) if manifest.exists() else {}
            except (ValueError, OSError) as exc:
                raise BackendError("invalid Pi session manifest") from exc
            if not isinstance(resumed, dict):
                raise BackendError("invalid Pi session manifest")
            messages = list(request.messages)
            if not messages or messages[-1].get("role") != "user":
                raise ValueError("Pi requests must end in a user message")
            first = {"type": "start", "request_id": uuid.uuid4().hex,
                     "cwd": str(Path(request.cwd).resolve()), "session_dir": str(directory.resolve()),
                     "system": mask_secrets(request.system), "prompt": _safe(messages[-1]["content"]),
                     "model": self._model(request), "tools": request.tools,
                     "max_steps": max_steps, "timeout": max(0.001, deadline - time.monotonic()),
                     "images": _images(request.images),
                     "history": _safe(history if history is not None else messages[:-1]),
                     "context_messages": _safe(context_messages or [])}
            if resumed.get("session_file"):
                previous = Path(resumed["session_file"])
                if previous.parent.resolve() != directory or previous.is_symlink() or not previous.is_file():
                    raise BackendError("Pi resume requires the existing scoped session file")
                first["session_file"] = str(previous)
                first["history"] = []
            result = self._exchange(status["entry"], first,
                                    replace(request, timeout=first["timeout"]), tool_handler)
            if result.session_file:
                path = Path(result.session_file)
                if path.parent.resolve() != directory.resolve() or not path.is_file() or path.is_symlink():
                    raise BackendError("Pi returned an invalid scoped session file")
                path.chmod(0o600)
                staged = manifest.with_name("session-" + uuid.uuid4().hex + ".tmp")
                fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as handle:
                    json.dump({"session_file": str(path), "session_id": result.session_id}, handle)
                staged.replace(manifest)
            return result

    def _exchange(self, entry: str, first: dict, request: BackendRequest, tool_handler) -> PiResult:
        """Bound process I/O, lifetime and cancellation without interpreting model plans."""
        _check_request(request, time.monotonic() + request.timeout)
        allowed_tools = {tool.get("function", tool).get("name") for tool in first["tools"]}
        try:
            process = subprocess.Popen(["node", entry], cwd=request.cwd, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       start_new_session=os.name == "posix")
        except OSError as exc:
            raise BackendError("could not start Pi host") from exc
        events = queue.Queue(maxsize=256)
        outgoing = queue.Queue(maxsize=2)
        stopped = threading.Event()

        def publish(value):
            """Allow pipe threads to stop even if the caller has left the exchange."""
            while not stopped.is_set():
                try:
                    events.put(value, timeout=0.05)
                    return
                except queue.Full:
                    continue

        def read_output():
            """Bound individual lines before buffering and the complete process output."""
            count = 0
            try:
                while not stopped.is_set():
                    line = process.stdout.readline(_MAX_WIRE + 1)
                    if not line:
                        break
                    count += len(line)
                    if count > _MAX_WIRE:
                        publish(BackendError("Pi output exceeded wire budget"))
                        return
                    publish(line)
            except (OSError, ValueError):
                if not stopped.is_set():
                    publish(BackendError("Pi output pipe failed"))
            finally:
                publish(None)

        def discard_errors():
            """Drain bounded chunks without retaining provider responses or credentials."""
            try:
                while not stopped.is_set() and process.stderr.read(65536):
                    pass
            except (OSError, ValueError):
                pass

        def write_input():
            """Avoid blocking cancellation when a broken host stops reading its pipe."""
            try:
                while not stopped.is_set():
                    try:
                        encoded = outgoing.get(timeout=0.05)
                    except queue.Empty:
                        continue
                    process.stdin.write(encoded)
                    process.stdin.flush()
            except (OSError, ValueError):
                if not stopped.is_set():
                    publish(BackendError("Pi input pipe failed"))

        readers = [threading.Thread(target=read_output, daemon=True),
                   threading.Thread(target=discard_errors, daemon=True),
                   threading.Thread(target=write_input, daemon=True)]
        for reader in readers:
            reader.start()
        deadline = time.monotonic() + min(request.timeout, self.config.timeout) + 1

        def safe(value):
            """Provider credentials of every format are excluded from audit content."""
            if isinstance(value, str):
                return mask_secrets(value.replace(self.config.api_key, "[redacted credential]")
                                    if self.config.api_key else value)
            if isinstance(value, list):
                return [safe(item) for item in value]
            if isinstance(value, dict):
                return {key: safe(item) for key, item in value.items()}
            return value

        def send(message):
            """Send bounded JSON over stdin with a cancellation-aware queue wait."""
            encoded = json.dumps(message, ensure_ascii=False).encode() + b"\n"
            if len(encoded) > _MAX_WIRE:
                raise BackendError("Pi input exceeded wire budget")
            while True:
                _check_request(request, deadline)
                try:
                    outgoing.put(encoded, timeout=0.05)
                    return
                except queue.Full:
                    continue

        seen_calls = set()
        try:
            send(first)
            while True:
                _check_request(request, deadline)
                try:
                    line = events.get(timeout=0.05)
                except queue.Empty:
                    continue
                if isinstance(line, Exception):
                    raise line
                if line is None:
                    raise BackendError("Pi host exited without a terminal result")
                try:
                    message = json.loads(line)
                except (ValueError, TypeError) as exc:
                    raise BackendError("Pi host returned invalid JSON") from exc
                if not isinstance(message, dict):
                    raise BackendError("Pi host returned an invalid protocol object")
                kind = message.get("type")
                if kind in {"tool_call", "model_request"}:
                    call_id = message.get("id")
                    if not isinstance(call_id, str) or not call_id or call_id in seen_calls:
                        raise BackendError("invalid or repeated Pi RPC identifier")
                    seen_calls.add(call_id)
                if kind == "tool_call":
                    if tool_handler is None or message.get("name") not in allowed_tools:
                        raise BackendError("Pi requested an undeclared host tool")
                    if not isinstance(message.get("arguments"), dict):
                        raise BackendError("Pi returned invalid tool arguments")
                    try:
                        result = tool_handler(message["name"], message["arguments"])
                        send({"type": "tool_result", "id": message["id"], "result": safe(result)})
                    except PausePi as pause:
                        send({"type": "pause", "id": message["id"], "approval_id": pause.approval_id,
                              "reason": "approval_required", "text": safe(pause.text)})
                    except (ValueError, KeyError, TypeError) as exc:
                        send({"type": "tool_result", "id": message["id"],
                              "result": {"error": safe(str(exc))}, "is_error": True})
                elif kind == "model_request":
                    if self.config.kind not in _CLI_PROVIDERS:
                        raise BackendError("unexpected external model RPC from an API route")
                    call = message["request"]
                    if call.get("images"):
                        raise BackendError("configured CLI model transport does not support images; select an API route")
                    reply = CLIBackend(self.config).complete(BackendRequest(
                        messages=call["messages"], system=call.get("system", ""),
                        tools=call.get("tools", []), cwd=request.cwd,
                        timeout=max(0.001, deadline-time.monotonic()), cancel=request.cancel, controller_mode=True))
                    send({"type": "model_result", "id": message["id"], "result": safe({
                        "text": reply.text, "tool_calls": [vars(c) for c in reply.tool_calls],
                        "usage": reply.usage, "finish_reason": reply.finish_reason})})
                elif kind == "result":
                    if message.get("request_id") != first["request_id"]:
                        raise BackendError("Pi result correlation mismatch")
                    if message.get("status") not in {"done", "failed", "awaiting_approval"}:
                        raise BackendError("Pi returned an invalid terminal status")
                    if any(not isinstance(message.get(field, "") or "", str)
                           for field in ("text", "session_file", "session_id", "error", "approval_id", "stop_reason", "error_code")):
                        raise BackendError("Pi returned invalid terminal metadata")
                    status_code = message.get("status_code")
                    if status_code is not None and (isinstance(status_code, bool) or not isinstance(status_code, int)):
                        raise BackendError("Pi returned invalid status code")
                    return PiResult(status=message["status"], text=safe(message.get("text", "")),
                                    session_file=message.get("session_file", "") or "",
                                    session_id=message.get("session_id", "") or "", usage=message.get("usage", {}),
                                    error=safe(message.get("error", "")),
                                    approval_id=message.get("approval_id", "") or "",
                                    stop_reason=message.get("stop_reason", "") or "",
                                    error_code=message.get("error_code", "") or "", status_code=status_code)
                elif kind != "event":
                    raise BackendError("unknown Pi protocol message")
        finally:
            stopped.set()
            _stop_process(process)
            for reader in readers:
                reader.join(timeout=1)
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe:
                    pipe.close()


def build_pi_backend(config: BackendConfig, data_dir: Path) -> PiBackend:
    """Every route shares the real Pi agent engine, including CLI model adapters."""
    return PiBackend(config, data_dir)
