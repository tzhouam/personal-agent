"""Bench isolation (doc/BENCHMARKS.md §2.6) — mandatory before any A-layer run.

Three independent walls, because a scratch DATA_DIR is not a security
boundary: (1) `bench_settings` builds a Settings with `_env_file=None` (the
class otherwise auto-reads the repo/CWD .env) and every outward credential
blanked; (2) `sandboxed_executor` runs the action registry deny-by-default —
outward/risky actions become recording fakes; (3) `network_guard` denies
socket connects at the transport except the allowed LLM endpoints."""

import contextlib
import socket
import tempfile
from pathlib import Path

from assistant.agent.actions import registry as actions_registry
from assistant.agent.actions.registry import ACTIONS, validate
from assistant.platform.config import Settings

# Actions a bench turn may actually EXECUTE against its scratch stores.
# Everything else — outward, risky, or pipeline-triggering — is faked and
# recorded (deny by default: a new registry action is faked until someone
# consciously adds it here).
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

# Credentials/keys that must be EMPTY in a bench profile — every outward
# integration the config knows about (kept in sync with config.py; the
# isolation test asserts none of these survives into bench settings).
BLANKED_FIELDS = (
    "anthropic_api_key", "github_token", "github_user",
    "smtp_user", "smtp_password", "smtp_host", "imap_host",
    "resend_api_key", "digest_to",
    "website_repo", "website_token", "marks_repo", "marks_token",
    "resume_remote_url",
    "wecom_corp_id", "wecom_secret", "wecom_agent_id", "wecom_token",
    "wecom_aes_key", "wecom_owner_userid",
    "openclaw_bin", "announce_account", "announce_to",
    "vision_api_key", "vision_model",
    "search_api_key", "serper_api_key", "chrome_history_path",
)


def bench_settings(scratch: Path | None = None) -> Settings:
    """A hermetic Settings for one bench run: `_env_file=None` (no .env
    leakage), a fresh scratch DATA_DIR, and every outward credential field
    that exists on this Settings version blanked."""
    scratch = Path(scratch or tempfile.mkdtemp(prefix="pa-bench-"))
    settings = Settings(_env_file=None)
    settings.data_dir = scratch / "data"
    for field in BLANKED_FIELDS:
        if hasattr(settings, field):
            setattr(settings, field, "")
    return settings


class SandboxRecorder:
    """What the sandbox observed: executed vs. faked action invocations —
    faked calls are scoreable trace events (e.g. the approval-gate golden
    asserts a risky action was ATTEMPTED but never ran)."""

    def __init__(self):
        self.executed: list[dict] = []
        self.faked: list[dict] = []


def sandboxed_executor(recorder: SandboxRecorder):
    """An `executor_override` implementation: allowlisted actions run the
    REAL handlers (against the bench profile's scratch stores); everything
    else records and returns a benign line without side effects."""

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
                recorder.faked.append(dict(raw))
                results.append(f"[bench-sandbox] {kind} recorded, not executed")
                continue
            error = validate(action, raw)
            if error:
                results.append(error)
                continue
            try:
                recorder.executed.append(dict(raw))
                results.append(action.handler(settings, raw))
            except Exception as exc:  # mirror production containment
                results.append(f"action {kind} failed: {exc}")
        return results

    return _execute


@contextlib.contextmanager
def action_sandbox(recorder: SandboxRecorder):
    """Scope the executor override to this context — the isolation test
    asserts it never leaks outside."""
    token = actions_registry._executor_override.set(sandboxed_executor(recorder))
    try:
        yield recorder
    finally:
        actions_registry._executor_override.reset(token)


@contextlib.contextmanager
def network_guard(allowed_hosts: frozenset[str]):
    """Deny socket connects except to `allowed_hosts` (exact hostname/IP
    match on the connect address). Transport-level defense-in-depth on top
    of blanked credentials and the action sandbox — handler fakes alone do
    not bound every code path. Loopback stays allowed (local test servers).

    Scope honestly stated: this patches `socket.socket.connect`, which
    covers httpx/requests/smtplib/imaplib in-process; it does not confine
    subprocesses — which is one reason subprocess-spawning actions are not
    in ALLOWED_ACTIONS."""
    allowed = set(allowed_hosts) | {"127.0.0.1", "localhost", "::1"}
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
