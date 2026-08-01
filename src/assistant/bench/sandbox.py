"""Bench isolation (doc/BENCHMARKS.md §2.6) — mandatory before any A-layer run.

Hermeticity here means **no outward side effects and no network except the
LLM endpoints** — NOT hiding the LLM key from ourselves (the whole point of
the M layer is to benchmark the configured model). So `bench_settings`
COPIES the base settings' LLM config (key/base-url/model/roles/mixture) and
blanks only the outward-integration credentials; `sandboxed_executor` runs
the registry deny-by-default; `network_guard` denies socket connects at the
transport except the resolved LLM endpoint IPs."""

import contextlib
import socket
import tempfile
from pathlib import Path

from assistant.agent.actions import registry as actions_registry
from assistant.agent.actions.registry import ACTIONS, looks_failed, validate
from assistant.platform.config import Settings

# Bench-STRICT non-success markers: production's `looks_failed` is deliberately
# narrow (a dedup rejection is not a failure for the repair loop), but for
# scoring we must reject "nothing actually happened" outcomes too — e.g.
# `cancel_reminder` of a non-existent id returns "no pending reminder 'm2'",
# which has no production failure marker yet is NOT a successful execution
# (reviewer round 2). These are checked IN ADDITION to looks_failed.
_BENCH_NONSUCCESS = ("no pending", "no active", "no open", "no such",
                     "not found", "unknown action", "couldn't", "can't find",
                     "已经完成", "找不到", "没有找到", "不存在", "无此",
                     "没有该", "无法")


def bench_succeeded(outcome: str) -> bool:
    low = str(outcome).lower()
    return not looks_failed(outcome) and not any(m in low or m in str(outcome)
                                                 for m in _BENCH_NONSUCCESS)

# Actions a bench turn may EXECUTE against its scratch stores. Everything
# else — outward, risky, or pipeline-triggering — is faked and recorded
# (deny by default: a new registry action is faked until consciously added).
ALLOWED_ACTIONS = frozenset({
    "add_todo", "done_todo", "list_todos", "done_reading", "list_reading",
    "unrelated_reading", "set_reminder", "list_reminders", "cancel_reminder",
    "create_routine", "list_routines", "cancel_routine",
    "log_transaction", "void_transaction", "recategorize_transaction",
    "list_transactions", "finance_summary", "query_transactions",
    "log_meal", "log_exercise", "log_weight", "set_health_profile",
    "add_health_need", "done_health_need", "health_summary", "query_health",
    "learn_preference", "retire_preference", "list_preferences",
    "show_profile", "acknowledge_failure",
})

# Read-only allowed actions (emitting one alongside the expected action is
# harmless; a MUTATING extra is not — the golden oracle rejects those).
READONLY_ACTIONS = frozenset({
    "list_todos", "list_reading", "list_reminders", "list_routines",
    "list_transactions", "finance_summary", "query_transactions",
    "health_summary", "query_health", "list_preferences", "show_profile",
})

# OUTWARD-integration credentials blanked in a bench profile (LLM config is
# deliberately KEPT). Derived by listing every Settings field whose name
# matches a credential/endpoint pattern, MINUS the LLM fields — so a new
# outward key added to config.py is blanked automatically instead of leaking
# (the isolation test asserts no non-LLM secret survives).
_LLM_KEEP = frozenset({
    "anthropic_api_key", "anthropic_base_url", "anthropic_model",
    "anthropic_default_haiku_model", "llm_roles", "llm_mixture", "llm_review",
})
_OUTWARD_PATTERNS = ("token", "password", "secret", "api_key", "_key",
                     "smtp", "imap", "_repo", "_url", "_userid", "_to",
                     "_account", "_bin", "history_path", "digest_to",
                     "corp_id", "aes_key", "agent_id", "github_user")


def outward_credential_fields() -> list[str]:
    """Every Settings field that names an outward credential/endpoint, minus
    the LLM config we intentionally keep. Computed from the live model so a
    new field can't silently leak."""
    fields = getattr(Settings, "model_fields", {})
    out = []
    for name, info in fields.items():
        if name in _LLM_KEEP:
            continue
        # only string/dict/path fields carry credentials or endpoints — an int
        # like imap_port is config, not a secret, and is left intact
        ann = getattr(info, "annotation", None)
        if ann not in (str, dict) and ann is not None and "Path" not in str(ann):
            continue
        low = name.lower()
        if any(pat in low for pat in _OUTWARD_PATTERNS):
            out.append(name)
    return out


def bench_settings(base: Settings | None = None, scratch: Path | None = None) -> Settings:
    """A hermetic Settings for one bench run: a COPY of `base` (its LLM
    config preserved so the M layer benchmarks the real configured model),
    every outward credential blanked, and a fresh scratch DATA_DIR. When
    `base` is None a `Settings(_env_file=None)` is used (test default — no
    .env, no model configured)."""
    base = base if base is not None else Settings(_env_file=None)
    settings = base.model_copy(deep=True)
    scratch = Path(scratch or tempfile.mkdtemp(prefix="pa-bench-"))
    settings.data_dir = scratch / "data"
    settings.deployment_mode = "single_user"
    settings.bench_enabled = False
    for field in outward_credential_fields():
        cur = getattr(settings, field, None)
        if isinstance(cur, str):
            setattr(settings, field, "")
        elif isinstance(cur, dict):
            setattr(settings, field, {})
        elif isinstance(cur, Path):
            # input paths (e.g. chrome_history_path) point under the scratch,
            # so a read finds nothing rather than the real file
            setattr(settings, field, scratch / "blanked" / field)
    return settings


def route_fingerprint(settings: Settings) -> dict:
    """A KEY-FREE description of the routing under test — model names + hosts
    only, for the summary and paired-delta comparability (never persist raw
    route specs; they can carry api keys, §2.7)."""
    from urllib.parse import urlsplit

    def host(url: str) -> str:
        return urlsplit(url or settings.anthropic_base_url
                        or "https://api.anthropic.com").hostname or ""

    roles = {}
    for role, spec in (settings.llm_roles or {}).items():
        if isinstance(spec, dict):
            roles[role] = {"model": spec.get("model", ""),
                           "host": host(spec.get("base_url", ""))}
    mix = settings.llm_mixture or {}
    mixture = {"members": [{"model": m.get("model", ""),
                            "host": host(m.get("base_url", ""))}
                           for m in mix.get("members", []) if isinstance(m, dict)],
               "aggregator": {"model": (mix.get("aggregator") or {}).get("model", ""),
                              "host": host((mix.get("aggregator") or {}).get("base_url", ""))}
               if isinstance(mix.get("aggregator"), dict) else None,
               "roles": sorted(mix.get("roles", []))}
    return {"default_model": settings.anthropic_model,
            "default_host": host(settings.anthropic_base_url),
            "cheap_model": settings.anthropic_default_haiku_model,
            "roles": roles, "mixture": mixture}


class SandboxRecorder:
    """What the sandbox observed: executed vs. faked action invocations, each
    with its outcome line, so the golden oracle can score SUCCESSFUL
    execution (not mere attempts) and reject harmful extras."""

    def __init__(self):
        self.executed: list[dict] = []   # {"action": {...}, "outcome": str, "ok": bool}
        self.faked: list[dict] = []      # {"action": {...}}


def sandboxed_executor(recorder: SandboxRecorder):
    """An `executor_override`: allowlisted actions run the REAL handlers
    against the bench profile's scratch stores; everything else records and
    returns a benign line without side effects. Each executed action's
    outcome (and whether it looks_failed) is recorded."""

    def _execute(actions: list, settings: Settings, max_actions: int = 5) -> list[str]:
        results: list[str] = []
        for raw in (actions or [])[:max_actions]:
            if not isinstance(raw, dict) or not raw.get("type"):
                continue
            kind = raw["type"]
            action = ACTIONS.get(kind)
            if action is None or not action.llm:
                results.append(f"unknown action {kind!r} ignored")
                continue
            if kind not in ALLOWED_ACTIONS:
                recorder.faked.append({"action": dict(raw)})
                results.append(f"[bench-sandbox] {kind} recorded, not executed")
                continue
            error = validate(action, raw)
            if error:
                recorder.executed.append({"action": dict(raw), "outcome": error,
                                          "ok": False})
                results.append(error)
                continue
            try:
                outcome = action.handler(settings, raw)
            except Exception as exc:  # mirror production containment
                outcome = f"action {kind} failed: {exc}"
            recorder.executed.append({"action": dict(raw), "outcome": outcome,
                                      "ok": bench_succeeded(outcome)})
            results.append(outcome)
        return results

    return _execute


@contextlib.contextmanager
def action_sandbox(recorder: SandboxRecorder):
    """Scope the executor override to this context (leak-proofed by test)."""
    token = actions_registry._executor_override.set(sandboxed_executor(recorder))
    try:
        yield recorder
    finally:
        actions_registry._executor_override.reset(token)


def _resolve_hosts(hosts: frozenset[str]) -> set[str]:
    """Hostnames → the IPs socket.connect actually receives, plus the
    literal names (a caller may pass an IP)."""
    out: set[str] = set(hosts)
    for h in hosts:
        try:
            for info in socket.getaddrinfo(h, None):
                out.add(info[4][0])
        except OSError:
            pass
    return out


@contextlib.contextmanager
def network_guard(allowed_hosts: frozenset[str]):
    """Deny socket connects except to `allowed_hosts` — resolved to IPs at
    entry, since `socket.connect` receives the resolved address, not the
    hostname (the round-1 bug). Loopback stays allowed (local test servers).
    Scope honestly stated: patches `socket.socket.connect`, covering
    httpx/requests/smtplib/imaplib in-process; it does not confine
    subprocesses — one reason subprocess-spawning actions are not
    allowlisted."""
    allowed = _resolve_hosts(allowed_hosts) | {"127.0.0.1", "::1"}
    real_connect = socket.socket.connect

    def guarded(self, address, *a, **k):
        host = address[0] if isinstance(address, tuple) else str(address)
        if str(host) not in allowed:
            raise PermissionError(
                f"bench network guard: connect to {host!r} denied")
        return real_connect(self, address, *a, **k)

    socket.socket.connect = guarded
    try:
        yield
    finally:
        socket.socket.connect = real_connect
