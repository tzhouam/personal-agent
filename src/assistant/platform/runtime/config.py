"""Load explicit runtime routes and channels without installing or logging secrets."""

from dataclasses import dataclass, field, replace
import os
from pathlib import Path
import shutil
import tomllib

from assistant.platform.config import Settings
from assistant.platform.runtime.contracts import BackendConfig


@dataclass
class RuntimeConfig:
    """Configuration of one owner's runtime; route failure never selects another."""

    backend: str
    backends: dict[str, BackendConfig]
    channels: dict[str, dict] = field(default_factory=dict, repr=False)
    projects: dict[str, dict] = field(default_factory=dict)
    max_steps: int = 12
    workers: int = 2
    timeout: float = 120
    path: Path | None = None

    def route(self, name: str | None = None) -> BackendConfig:
        """Resolve an exact named route, refusing silent fallbacks."""
        key = name or self.backend
        if key not in self.backends:
            raise ValueError(f"unknown backend route {key!r}")
        return self.backends[key]


def load_runtime_config(settings: Settings, path: Path | None = None,
                        backend: str | None = None) -> RuntimeConfig:
    """Read runtime.toml plus existing API settings; do not mutate either file."""
    def env_value(name: str) -> str:
        """Honor exported variables and known Settings fields loaded from .env."""
        if settings.deployment_mode == "multi_tenant" and settings.uid != "default":
            from dotenv import dotenv_values

            owner_file = settings.data_dir / "config.env"
            own = dotenv_values(owner_file) if owner_file.is_file() else {}
            if name in own:
                return str(own[name] or "")
            return str(getattr(settings, name.lower(), ""))
        return os.environ.get(name, str(getattr(settings, name.lower(), "")))

    path = path or settings.runtime_config or settings.data_dir / "runtime.toml"
    data = tomllib.loads(path.read_text()) if path.exists() else {}
    runtime = data.get("runtime", {})
    timeout = float(runtime.get("timeout", 120))
    steps, workers = int(runtime.get("max_steps", 12)), int(runtime.get("workers", 2))
    if not 1 <= steps <= 64 or not 1 <= workers <= 16 or not 1 <= timeout <= 3600:
        raise ValueError("runtime limits: max_steps 1..64, workers 1..16, timeout 1..3600")
    routes = {
        name: BackendConfig(kind=name, timeout=timeout,
                            allow_unrestricted=settings.runtime_allow_unrestricted)
        for name in ("codex", "claude", "gemini")
    }
    routes["anthropic"] = BackendConfig(
        kind="anthropic", model=settings.anthropic_model,
        base_url=settings.anthropic_base_url or "https://api.anthropic.com",
        api_key=settings.anthropic_api_key, timeout=timeout)
    routes["openai"] = BackendConfig(
        kind="openai", model=settings.openai_model,
        base_url=settings.openai_base_url, api_key=settings.openai_api_key,
        timeout=timeout)
    for name, spec in data.get("backends", {}).items():
        if not isinstance(spec, dict):
            raise ValueError(f"backend {name!r} must be a TOML table")
        kind = str(spec.get("kind", name))
        base = routes.get(kind, BackendConfig(kind=kind, timeout=timeout))
        command = spec.get("command", base.command)
        if not isinstance(command, (list, tuple)) or not all(isinstance(x, str) for x in command):
            raise ValueError(f"backend {name!r} command must be an argv array")
        api_key = base.api_key
        if "api_key_env" in spec:
            api_key = env_value(str(spec["api_key_env"]))
        if "api_key" in spec:
            raise ValueError("store backend credentials in environment variables, not runtime.toml")
        route_timeout = float(spec.get("timeout", timeout))
        if not 1 <= route_timeout <= 3600:
            raise ValueError(f"backend {name!r} timeout must be 1..3600 seconds")
        unrestricted = spec.get("allow_unrestricted", base.allow_unrestricted)
        if not isinstance(unrestricted, bool):
            raise ValueError(f"backend {name!r} allow_unrestricted must be a boolean")
        context_window = spec.get("context_window", base.context_window)
        if (not isinstance(context_window, int) or isinstance(context_window, bool)
                or not 1024 <= context_window <= 10_000_000):
            raise ValueError(f"backend {name!r} context_window must be 1024..10000000")
        routes[name] = replace(
            base, kind=kind, model=str(spec.get("model", base.model)),
            command=tuple(command), base_url=str(spec.get("base_url", base.base_url)),
            api_key=api_key, timeout=route_timeout,
            allow_unrestricted=unrestricted,
            api=str(spec.get("api", base.api)), provider=str(spec.get("provider", base.provider)),
            thinking_level=str(spec.get("thinking_level", base.thinking_level)),
            context_window=context_window)
    selected = backend or settings.runtime_backend or str(runtime.get("backend", "auto"))
    if selected == "auto":
        # Auto only chooses at startup. A failing selected route is never retried
        # with a different provider/account or represented as successful.
        if settings.anthropic_api_key:
            selected = "anthropic"
        elif settings.openai_api_key:
            selected = "openai"
        else:
            selected = next((name for name in ("codex", "claude", "gemini")
                             if shutil.which(name)), "codex")
    channels = {}
    for name, spec in data.get("channels", {}).items():
        if not isinstance(spec, dict):
            raise ValueError(f"channel {name!r} must be a TOML table")
        if not isinstance(spec.get("enabled", False), bool):
            raise ValueError(f"channel {name!r} enabled must be a boolean")
        if not spec.get("enabled", False):
            continue
        resolved = dict(spec)
        resolved.pop("enabled", None)
        for key in list(resolved):
            if key.endswith("_env"):
                resolved[key[:-4]] = env_value(str(resolved.pop(key)))
        # A deployed multi-user installation must resolve per-owner settings at
        # the composition root; an adapter never trusts a payload-supplied uid.
        channels[name] = resolved
    config = RuntimeConfig(selected, routes, channels, data.get("projects", {}),
                           steps, workers, timeout, path)
    config.route()
    return config
