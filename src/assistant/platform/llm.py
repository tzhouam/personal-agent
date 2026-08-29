"""Thin Anthropic client wrapper for the agent.

Exports the ``LLM`` class: traced provider calls with platform-owned bounded
retry, durable route quarantine, and a JSON-coercing convenience method. Keeps
every call site provider-agnostic and degrade-friendly.
"""

import json
import re
import threading
import time as _time

import anthropic

from assistant.platform.config import Settings
from assistant.platform.llm_health import RouteHealthStore
from assistant.platform.llm_health import route_scopes as _durable_route_scopes

_RETRY_DELAYS_S = (1, 4)
_STRUCTURED_TOKEN_CAP = 16_000
_AUTH_PROVIDER_CODES = frozenset({"authentication_error", "invalid_api_key"})
_sleep = _time.sleep


_CHEAP_ROLES = frozenset({"cheap", "bulk", "research", "score"})
_DEFAULT_MIXTURE_ROLES = ("pipeline", "research", "task", "evolve")
_INTERACTIVE_TIMEOUT_S = 45
_OFFLINE_TIMEOUT_S = 120
_SENSITIVE_IDENTIFIER = re.compile(
    r"(?i)(?:://|^sk[-_]|api[_-]?key|password|credential|bearer|"
    r"(?:^|[-_])token(?:$|[-_])|(?:^|[-_])secret(?:$|[-_])|"
    r"(?:^|[-_])key(?:$|[-_]))")


def _bounded_identifier(value, *, redacted: str) -> str:
    """Return a bounded control-free label, redacting secret-like values."""
    raw = str(value or "")
    if _SENSITIVE_IDENTIFIER.search(raw):
        return redacted
    safe = re.sub(r"[^A-Za-z0-9_.+/-]", "?", raw)[:64]
    return safe or "unknown"


def _bounded_error_type(exc) -> str:
    """Return useful exception-class metadata without trusting its name."""
    return _bounded_identifier(type(exc).__name__, redacted="redacted_error")


def _bounded_route_label(value) -> str:
    """Return a privacy-safe model label for durable warning logs."""
    return _bounded_identifier(value, redacted="redacted_route")


def _normalized_route_spec(value) -> dict | None:
    """Return one safe model route spec, or None for malformed structure."""
    if not isinstance(value, dict):
        return None
    model = value.get("model")
    if not isinstance(model, str) or not model.strip():
        return None
    normalized = {"model": model.strip()}
    for field in ("base_url", "api_key"):
        item = value.get(field)
        if item is not None and not isinstance(item, str):
            return None
        if item is not None:
            normalized[field] = item
    return normalized


def normalize_mixture(value, settings: Settings | None = None) -> dict:
    """Normalize tolerant JSON into a runtime-safe MoA configuration.

    Invalid members are dropped; when ``settings`` is supplied, canonical-
    equivalent model routes are also deduplicated. Fewer than two surviving
    members naturally keeps MoA disabled. Invalid roles/aggregator/layers use
    their documented defaults so a syntactically valid but structurally
    malformed optional knob can never crash LLM construction or a later call.
    """
    if not isinstance(value, dict):
        return {}
    raw_members = value.get("members")
    raw_members = raw_members if isinstance(raw_members, list) else []
    members = [spec for item in raw_members
               if (spec := _normalized_route_spec(item)) is not None]
    if settings is not None:
        unique = []
        seen = set()
        for spec in members:
            scope = _route_scopes(
                settings, spec.get("base_url"), spec.get("api_key"),
                spec["model"])[1]
            if scope in seen:
                continue
            seen.add(scope)
            unique.append(spec)
        members = unique

    raw_roles = value.get("roles")
    if isinstance(raw_roles, list):
        roles = [role.strip() for role in raw_roles
                 if isinstance(role, str) and role.strip()]
    else:
        roles = []
    if not roles:
        roles = list(_DEFAULT_MIXTURE_ROLES)

    aggregator = _normalized_route_spec(value.get("aggregator"))
    if aggregator is None and members:
        aggregator = dict(members[0])

    try:
        raw_layers = value.get("layers", 1)
        if isinstance(raw_layers, (dict, list, bool)):
            raise ValueError("invalid layers")
        layers = max(1, int(raw_layers))
    except (TypeError, ValueError, OverflowError):
        layers = 1

    normalized = {"members": members, "roles": roles, "layers": layers}
    if aggregator is not None:
        normalized["aggregator"] = aggregator
    return normalized


class CompletionText(str):
    """String-compatible completion carrying provider termination metadata."""

    def __new__(cls, value: str, stop_reason: str = ""):
        """Attach ``stop_reason`` without changing callers' string contract."""
        result = super().__new__(cls, value)
        result.stop_reason = stop_reason
        return result


class StructuredOutputTruncatedError(ValueError):
    """Structured output stayed truncated at the platform's safe token cap."""


class RouteQuarantinedError(RuntimeError):
    """A durable auth/permission quarantine prevented a provider call."""

    def __init__(self, scope: str):
        """Expose only bounded scope metadata, never route configuration."""
        self.scope = scope
        super().__init__(f"LLM route quarantined at {scope} scope")


class RouteHealthPersistenceError(RuntimeError):
    """A successful route probe could not durably clear its quarantine."""


# Injected metrics sink `(settings, run_id, step, values) -> None`. The durable
# events.db is agent-owned, so llm.py never imports it — the agent registers a
# default here (`agent.observability`) and any LLM without an explicit
# `metrics_sink` uses it. None → metrics are skipped. This module holds the
# registration for the whole platform layer: `notify.py` reads it too, so a
# reminder give-up lands in the same events.db as the MoA rows.
_default_metrics_sink = None


def set_default_metrics_sink(sink) -> None:
    """Register the agent-side metrics sink. Keeps llm.py agent-free."""
    global _default_metrics_sink
    _default_metrics_sink = sink


def get_default_metrics_sink():
    """The registered sink, or None when no composition root wired one up."""
    return _default_metrics_sink


# ── provider circuit breaker (module-level: LLM is rebuilt per request) ──────
#
# A provider that is down must not cost every turn a fresh 40-60s retry window
# (2026-07-17 noon incident). Failures are tracked at two scopes per RESOLVED
# route — transport/auth failures poison the provider+credential (every model
# on it), 5xx only the one model:
#     ("prov",  resolved_base_url, cred_fp)
#     ("model", resolved_base_url, cred_fp, model)
# cred_fp is a credential-keyed HMAC shared with the durable quarantine (never
# the credential itself), so different tenants on one endpoint do not poison
# each other. State machine is generation-guarded: every recorded outcome carries
# the gen snapshot from when its call STARTED, so a stale in-flight completion
# can neither close nor re-open a newer state. After a cooldown expires exactly
# one caller claims the half-open probe lease (no retry stampede); a neutral
# probe outcome releases the lease without counting.

_BREAKER_LOCK = threading.Lock()
_BREAKER: dict = {}   # scope key → {fails, open, until, gen, lease, lease_ts}
_RESET_PATTERN = re.compile(r"connection reset|recvaddress", re.IGNORECASE)


def _reset_breaker() -> None:
    """Test hook: forget all provider health state."""
    with _BREAKER_LOCK:
        _BREAKER.clear()


def _breaker_clear_route(scopes: tuple) -> None:
    """Close both route scopes after an explicit successful health probe.

    Generations advance instead of deleting entries, so stale in-flight calls
    that started before the probe cannot re-open the freshly verified route.
    """
    with _BREAKER_LOCK:
        for key in scopes:
            entry = _entry(key)
            entry.update(fails=0, open=False, until=0.0,
                         gen=entry["gen"] + 1, lease=None, lease_ts=0.0)


def _route_scopes(settings: Settings, base_url, api_key, model) -> tuple:
    """(provider_key, model_key) for the RESOLVED route — blanks resolve to the
    settings defaults exactly like `_client` does, so an omitted and an explicit
    default URL/key are the same route."""
    url = base_url or settings.anthropic_base_url
    key = api_key or settings.anthropic_api_key or ""
    return _durable_route_scopes(url, key, model)


def _status_code(exc) -> int | None:
    """Return a real integer HTTP status without trusting status-like text."""
    try:
        status = getattr(exc, "status_code", None)
    except Exception:
        return None
    return int(status) if isinstance(status, int) and not isinstance(status, bool) else None


def _normalize_provider_code(value) -> str | None:
    """Normalize one bounded provider error code for exact policy matching."""
    if not isinstance(value, str) or not value or len(value) > 100:
        return None
    normalized = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    return normalized or None


def _provider_error_code(exc) -> str | None:
    """Extract a structured provider code without retaining body/message data."""
    candidates = []
    try:
        body = getattr(exc, "body", None)
    except Exception:
        body = None
    if isinstance(body, dict):
        candidates.append(body)
    try:
        response = getattr(exc, "response", None)
        response_body = response.json() if response is not None else None
    except Exception:
        response_body = None
    if isinstance(response_body, dict) and response_body is not body:
        candidates.append(response_body)
    for payload in candidates:
        nodes = [payload]
        error = payload.get("error")
        if isinstance(error, dict):
            nodes.insert(0, error)
        for node in nodes:
            for key in ("code", "type", "error_code"):
                normalized = _normalize_provider_code(node.get(key))
                if normalized:
                    return normalized
    return None


def _auth_quarantine_scope(exc) -> str | None:
    """Return the durable scope for terminal 401/403 route failures."""
    status = _status_code(exc)
    if status == 401:
        return "prov"
    if status == 403:
        return ("prov" if _provider_error_code(exc) in _AUTH_PROVIDER_CODES
                else "model")
    return None


def _classify_failure(exc) -> str | None:
    """Which breaker scope a failure trips: 'prov' (endpoint+credential dead —
    transport, timeout, 429, 401/auth-coded 403, or a connection-reset wrapped
    in a 400), 'model' (5xx or ordinary 403 — one overloaded/unauthorized
    model), or None (request-specific/unknown — programming and validation
    errors must never poison a route)."""
    if isinstance(exc, anthropic.APIConnectionError):   # includes timeouts
        return "prov"
    status = _status_code(exc)
    auth_scope = _auth_quarantine_scope(exc)
    if auth_scope:
        return auth_scope
    if status == 429:
        return "prov"
    if status == 400 and _RESET_PATTERN.search(str(exc)):
        return "prov"                                   # MiMo wraps resets in 400
    if isinstance(status, int) and status >= 500:
        return "model"
    try:
        import httpx

        if isinstance(exc, httpx.TransportError):
            return "prov"
    except Exception:
        pass
    return None


def _is_transient(exc) -> bool:
    """Whether the platform may retry this failure within its three attempts."""
    if _status_code(exc) in (401, 403):
        return False
    if isinstance(exc, (anthropic.APIConnectionError,
                        anthropic.RateLimitError,
                        anthropic.InternalServerError)):
        return True
    status = _status_code(exc)
    if status == 429 or (isinstance(status, int) and status >= 500):
        return True
    try:
        import httpx

        return isinstance(exc, httpx.TransportError)
    except Exception:
        return False


def _request_timeout_s(role: str | None) -> int:
    """Bound provider requests: interactive chat 45s, offline work 120s."""
    return _INTERACTIVE_TIMEOUT_S if role == "chat" else _OFFLINE_TIMEOUT_S


def _failure_span_attrs(exc) -> dict:
    """Return bounded, privacy-safe metadata for a failed provider call.

    Exception messages, response bodies, prompts, and credentials are
    deliberately excluded: they may contain owner data or secrets.  Failure
    classification is best-effort so observability can never mask the original
    provider exception.
    """
    classification = None
    try:
        classification = _classify_failure(exc)
    except Exception:
        pass
    attrs = {
        "error_type": _bounded_error_type(exc),
        "breaker_classification": classification or "none",
    }
    try:
        status = _status_code(exc)
    except Exception:
        status = None
    if isinstance(status, int) and not isinstance(status, bool):
        attrs["status_code"] = int(status)
    return attrs


def _entry(key: tuple) -> dict:
    e = _BREAKER.get(key)
    if e is None:
        e = _BREAKER[key] = {"fails": 0, "open": False, "until": 0.0,
                             "gen": 0, "lease": None, "lease_ts": 0.0}
    return e


def _breaker_check(scopes: tuple, cooldown_s: float, now: float | None = None):
    """Atomically classify a route: `("closed"|"open"|"probe", gens, claimed)`.

    `gens` snapshots each scope's generation for later recording. "probe" means
    this caller claimed the half-open lease on EVERY open-expired scope in one
    lock section (all-or-nothing); a lease older than a full cooldown is treated
    as abandoned and stolen (a prober that died without recording)."""
    now = _time.monotonic() if now is None else now
    with _BREAKER_LOCK:
        gens = {}
        open_expired = []
        for key in scopes:
            e = _entry(key)
            gens[key] = e["gen"]
            if not e["open"]:
                continue
            if e["until"] > now:
                return "open", gens, frozenset()
            leased = (e["lease"] is not None
                      and now - e["lease_ts"] <= cooldown_s)
            if leased:
                return "open", gens, frozenset()   # someone else is probing
            open_expired.append(key)
        if not open_expired:
            return "closed", gens, frozenset()
        for key in open_expired:                   # claim all leases atomically
            e = _entry(key)
            e["lease"] = gens[key]
            e["lease_ts"] = now
        return "probe", gens, frozenset(open_expired)


def _breaker_record(scopes: tuple, gens: dict, claimed: frozenset,
                    outcome: str | None, threshold: int, cooldown_s: float) -> None:
    """Record one call outcome (`"ok"`, `"prov"`, `"model"`, or None=neutral)
    against both scopes, generation-guarded: a stale completion (gen advanced
    since the call started) is ignored entirely. Success closes/resets; a
    classified failure counts against exactly its scope (immediately re-opening
    an already-open scope being probed); everything else only releases any
    claimed lease, uncounted."""
    with _BREAKER_LOCK:
        for key in scopes:
            e = _entry(key)
            if e["gen"] != gens.get(key):
                continue                            # stale — a newer cycle owns this scope
            scope_type = key[0]
            if outcome == "ok":
                e.update(fails=0, open=False, until=0.0,
                         gen=e["gen"] + 1, lease=None, lease_ts=0.0)
            elif outcome == scope_type:
                e["fails"] += 1
                if e["open"] or e["fails"] >= threshold:
                    e.update(open=True, fails=0,
                             until=_time.monotonic() + cooldown_s,
                             gen=e["gen"] + 1, lease=None, lease_ts=0.0)
            elif key in claimed:                    # neutral / other-scope failure
                e["lease"] = None                   # release the probe, uncounted
                e["lease_ts"] = 0.0


class LLM:
    """Anthropic chat client with per-role model routing.

    The ``ANTHROPIC_*`` settings are the default provider (base URL + key) and
    model. ``settings.llm_roles`` (the ``LLM_ROLES`` JSON map) can route named
    task roles to a different model and — since a different model often lives
    on a different endpoint — a different base URL + key, so e.g. chat runs on
    mimo-v2.5 while research runs on qwen3.6-plus at the same time. A role with
    no entry falls back to the default (cheap tier for cheap-ish roles, else
    the main model); clients are cached per (base_url, key)."""

    def __init__(self, settings: Settings, metrics_sink=None):
        """Cache the default provider, model tiers, the role map, and a lazy
        per-provider client cache.

        `metrics_sink` — an optional `(settings, run_id, step, values) -> None`
        callback for the durable MoA metrics row. Injected so this platform
        module imports no agent code (the events.db sink is agent-owned); when
        omitted it falls back to the registered default (`agent.observability`),
        and if nothing is registered, MoA metrics are silently skipped."""
        self.settings = settings
        self.metrics_sink = metrics_sink
        self.default_model = settings.anthropic_model
        self.cheap_model = settings.cheap_model
        self.roles: dict = dict(settings.llm_roles or {})
        # LLM_REVIEW seeds the "review" role (an explicit LLM_ROLES entry
        # wins): the strongest-reasoning slot for plan/design review — always
        # single-model, never in the MoA role set
        if (settings.llm_review or {}).get("model"):
            self.roles.setdefault("review", settings.llm_review)
        self.mixture: dict = normalize_mixture(settings.llm_mixture, settings)
        # roles that run Mixture-of-Agents when >=2 members are configured;
        # defaults to the offline, quality-sensitive roles (interactive chat is
        # opt-in, since MoA ~doubles latency)
        self._mixture_roles: set = (
            set(self.mixture["roles"])
            if len(self.mixture["members"]) >= 2 else set())
        self._clients: dict = {}
        try:
            self._health = RouteHealthStore(settings.shared_dir)
        except Exception:
            import logging

            logging.getLogger("assistant").warning(
                "LLM route health store unavailable; durable quarantine disabled",
                exc_info=True)
            self._health = None
        self.client = self._client(settings.anthropic_base_url,
                                   settings.anthropic_api_key)

    def _client(self, base_url: str | None, api_key: str | None):
        """Return an Anthropic client for (base_url, key), building and caching
        one per distinct provider; blanks fall back to the default provider."""
        base_url = base_url or self.settings.anthropic_base_url
        api_key = api_key or self.settings.anthropic_api_key
        cache_key = (base_url, api_key)
        if cache_key not in self._clients:
            # The SDK otherwise retries twice internally, multiplying the
            # platform's explicit three-attempt policy into hidden attempts.
            kwargs: dict = {"api_key": api_key, "max_retries": 0,
                            "timeout": _OFFLINE_TIMEOUT_S}
            if base_url:
                kwargs["base_url"] = base_url
            self._clients[cache_key] = anthropic.Anthropic(**kwargs)
        return self._clients[cache_key]

    def _resolve(self, role: str | None, model: str | None):
        """Map ``role``/``model`` to a concrete (client, model_id). An explicit
        ``model`` wins on the default provider; a configured role uses its
        model + optional provider override; an unconfigured role falls back to
        the cheap or default model on the default provider."""
        client, model_id, _base_url, _api_key = self._resolve_route(role, model)
        return client, model_id

    def _resolve_route(self, role: str | None, model: str | None):
        """Resolve a call to client/model plus its effective URL/credential."""
        # Preserve the lightweight ``LLM.__new__`` provider seam used by image
        # integration tests and embedders that inject an already-built client.
        if not hasattr(self, "settings"):
            return self.client, model or self.default_model, None, None
        if model:
            return (self.client, model, self.settings.anthropic_base_url,
                    self.settings.anthropic_api_key)
        spec = self.roles.get(role) if role else None
        if isinstance(spec, dict) and spec.get("model"):
            return (self._client(spec.get("base_url"), spec.get("api_key")),
                    spec["model"],
                    spec.get("base_url") or self.settings.anthropic_base_url,
                    spec.get("api_key") or self.settings.anthropic_api_key)
        if role in _CHEAP_ROLES:
            return (self.client, self.cheap_model,
                    self.settings.anthropic_base_url,
                    self.settings.anthropic_api_key)
        return (self.client, self.default_model,
                self.settings.anthropic_base_url,
                self.settings.anthropic_api_key)

    def _quarantine_scope(self, scopes: tuple) -> str | None:
        """Best-effort durable lookup; storage trouble must not block LLM use."""
        if getattr(self, "_health", None) is None:
            return None
        try:
            return self._health.quarantine_scope(scopes)
        except Exception:
            import logging

            logging.getLogger("assistant").warning(
                "LLM route health lookup failed; allowing provider call",
                exc_info=True)
            return None

    def _quarantine(self, scopes: tuple, scope: str) -> None:
        """Best-effort persistence of a terminal auth/permission failure."""
        if getattr(self, "_health", None) is None:
            return
        try:
            self._health.quarantine(scopes, scope)
        except Exception:
            import logging

            logging.getLogger("assistant").warning(
                "LLM route quarantine write failed", exc_info=True)

    def _clear_quarantine(self, scopes: tuple) -> None:
        """Clear durable state or raise a bounded error before transient reset."""
        if getattr(self, "_health", None) is None:
            raise RouteHealthPersistenceError(
                "LLM route health store unavailable during probe recovery")
        try:
            self._health.clear_route(scopes)
        except Exception:
            import logging

            logging.getLogger("assistant").warning(
                "LLM route quarantine clear failed")
            raise RouteHealthPersistenceError(
                "LLM route quarantine clear failed") from None

    def complete(
        self,
        prompt: str,
        system: str | None = None,
        model: str | None = None,
        max_tokens: int = 4000,
        images: list[str] | None = None,
        role: str | None = None,
        mixture: bool = True,
    ) -> str:
        """Send one user ``prompt`` (optional ``system``) and return the
        concatenated text blocks. ``role`` selects the model+provider via the
        role map (e.g. "chat", "research", "task"); an explicit ``model``
        overrides it on the default provider; both default to ``default_model``.
        ``images`` are local file paths attached as image content blocks before
        the text — only meaningful on a multimodal model. ``mixture=False``
        forces single-model execution on the resolved route even when the role
        is MoA-configured — the escape hatch for latency/cost-floor calls
        (task-tier classification, simple/medium task turns) that must never
        pay the ~2× MoA overhead. Each underlying API call (``_call``) is
        traced, retried on transient errors, and retains ``stop_reason`` on its
        string-compatible result. Retry lives on
        ``_call`` (not here) so a mixture's proposers and aggregator each get
        their own retry rather than being dropped on the first blip, and an
        aggregator retry doesn't re-run every proposer."""
        content: str | list = prompt
        if images:
            content = [_image_block(p) for p in images] + [
                {"type": "text", "text": prompt}]
        if model is None and mixture and role and role in self._mixture_roles \
                and len(self.mixture.get("members", [])) >= 2:
            return self._mixture(content, system, max_tokens, role=role)
        client, model_id, base_url, api_key = self._resolve_route(role, model)
        scopes = (_route_scopes(self.settings, base_url, api_key, model_id)
                  if hasattr(self, "settings") else None)
        return self._call(client, model_id, content, system, max_tokens,
                          route_scopes=scopes,
                          timeout_s=_request_timeout_s(role))

    def force_probe(
        self,
        prompt: str,
        system: str | None = None,
        model: str | None = None,
        max_tokens: int = 1500,
        role: str | None = None,
    ) -> str:
        """Probe one route despite quarantine, clearing it only after success.

        This is the narrow recovery hook for ``assistant init --check`` after an
        operator fixes credentials or provider access.  It remains a normal
        traced, bounded-retry call; only the pre-call quarantine gate is bypassed.
        """
        client, model_id, base_url, api_key = self._resolve_route(role, model)
        return self._force_probe_resolved(
            client, model_id, base_url, api_key, prompt, system, max_tokens)

    def force_probe_route(
        self,
        model: str,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        prompt: str = "Reply with the single word: ok",
        system: str | None = None,
        max_tokens: int = 1500,
    ) -> str:
        """Probe one explicit configured route and clear only proven health.

        ``base_url``/``api_key`` follow normal blank-to-default resolution and
        are never logged or returned. This lets the config doctor recover
        role, mixture-member, aggregator, and review routes that are otherwise
        unreachable once a model-scoped 403 quarantine exists.
        """
        if not model:
            raise ValueError("LLM route probe requires a model")
        resolved_url = base_url or self.settings.anthropic_base_url
        resolved_key = api_key or self.settings.anthropic_api_key
        client = self._client(resolved_url, resolved_key)
        return self._force_probe_resolved(
            client, str(model), resolved_url, resolved_key,
            prompt, system, max_tokens)

    def _force_probe_resolved(
        self,
        client,
        model_id: str,
        base_url: str | None,
        api_key: str | None,
        prompt: str,
        system: str | None,
        max_tokens: int,
    ) -> str:
        """Run one resolved forced probe and clear both health layers on success."""
        scopes = _route_scopes(self.settings, base_url, api_key, model_id)
        result = self._call(client, model_id, prompt, system, max_tokens,
                            route_scopes=scopes, allow_quarantined=True,
                            timeout_s=_INTERACTIVE_TIMEOUT_S)
        if not result.strip():
            return result
        self._clear_quarantine(scopes)
        _breaker_clear_route(scopes)
        return result

    def _call(self, client, model_id: str, content, system: str | None,
              max_tokens: int, span_attrs: dict | None = None,
              route_scopes: tuple | None = None,
              allow_quarantined: bool = False,
              timeout_s: float = _OFFLINE_TIMEOUT_S) -> str:
        """One traced ``messages.create`` returning the concatenated text; the
        shared core of the single-model and mixture paths. ``span_attrs`` ride
        on the ``llm`` span — the mixture path tags each call with its stage
        (proposer/aggregator/fallback) so MoA overhead is measurable per call.
        The platform makes at most three transient attempts, with 1s and 4s
        waits; SDK retries are disabled. 401/403 are never retried and instead
        enter the durable deployment-wide quarantine. ``allow_quarantined`` is
        reserved for the explicit operator health probe.

        Appends the temporal anchor to the TAIL of the user content — the
        model's only reliable clock. Tail placement adds nothing before any
        existing token, so the cacheable prompt prefix (system + stable prompt
        heads) stays byte-identical; never into ``system`` (that would bust the
        static prefix every request). List content is copied, never mutated —
        the mixture path passes one shared list to every proposer."""
        from assistant.platform.timeutil import temporal_anchor

        scopes = route_scopes
        if scopes is None and hasattr(self, "settings"):
            scopes = _route_scopes(self.settings, None, None, model_id)
        if scopes is not None and not allow_quarantined:
            quarantined = self._quarantine_scope(scopes)
            if quarantined:
                raise RouteQuarantinedError(quarantined)

        anchor = temporal_anchor()
        if isinstance(content, str):
            content = content + "\n\n" + anchor
        else:
            content = [*content, {"type": "text", "text": anchor}]
        kwargs: dict = {"model": model_id, "max_tokens": max_tokens,
                        "timeout": timeout_s,
                        "messages": [{"role": "user", "content": content}]}
        if system:
            kwargs["system"] = system
        from assistant.platform import tracing

        resp = None
        for attempt in range(1, len(_RETRY_DELAYS_S) + 2):
            if scopes is not None and attempt > 1 and not allow_quarantined:
                quarantined = self._quarantine_scope(scopes)
                if quarantined:
                    raise RouteQuarantinedError(quarantined)
            with tracing.span("llm", model=model_id, max_tokens=max_tokens,
                              retry_attempt=attempt,
                              **(span_attrs or {})) as _sp:
                try:
                    resp = client.messages.create(**kwargs)
                except Exception as exc:
                    _sp.set(**_failure_span_attrs(exc))
                    auth_scope = _auth_quarantine_scope(exc)
                    if auth_scope and scopes is not None:
                        self._quarantine(scopes, auth_scope)
                    if attempt > len(_RETRY_DELAYS_S) or not _is_transient(exc):
                        raise
                else:
                    tracing.set_usage(
                        _sp, getattr(resp, "usage", None),
                        stop_reason=getattr(resp, "stop_reason", "") or "")
                    break
            _sleep(_RETRY_DELAYS_S[attempt - 1])

        stop_reason = getattr(resp, "stop_reason", "") or ""
        if stop_reason == "max_tokens":
            import logging

            logging.getLogger("assistant").warning(
                "LLM response truncated at max_tokens=%s — raise the budget for this call",
                max_tokens)
        text = "".join(b.text for b in resp.content if b.type == "text")
        return CompletionText(text, stop_reason)

    def _mixture(self, content, system: str | None, max_tokens: int,
                 role: str | None = None) -> str:
        """Observability shell around `_mixture_run`: a parent `mixture` trace
        span plus a durable numeric `moa` metrics row per call (events.db —
        chat/task turns have no tracer, so spans alone would under-count the
        interactive workload). Never lets recording failures into the call
        path."""
        from assistant.platform import tracing

        stats = {"members_total": len(self.mixture.get("members", [])),
                 "members_attempted": 0, "members_skipped": 0,
                 "members_failed": 0, "proposals_ok": 0,
                 "proposals_final": 0,
                 "aggregator_attempted": 0, "aggregator_skipped": 0,
                 "aggregator_failed": 0, "aggregator_ok": 0,
                 "fallback_used": 0, "abandoned": 0, "degraded": 1}
        start = _time.monotonic()
        with tracing.span("mixture", role=role or "") as sp:
            try:
                return self._mixture_run(content, system, max_tokens, role, stats)
            finally:
                # A useful MoA result requires both independent evidence and a
                # successful synthesis.  Compute this once at finalization so
                # every exit path emits the same deterministic health signal.
                stats["degraded"] = int(
                    stats["proposals_final"] < 2
                    or stats["aggregator_ok"] != 1)
                sp.set(**stats)
                self._record_moa_metrics(stats, _time.monotonic() - start)

    def _record_moa_metrics(self, stats: dict, duration_s: float) -> None:
        """Best-effort durable MoA sink (doc/PIPELINE_METRICS.md §0 cost/step):
        one numeric row per mixture call, day-keyed like `chat_turn` rows. The
        sink is injected (agent-owned events.db) so this platform module imports
        no agent code; with no sink registered, metrics are silently skipped."""
        sink = self.metrics_sink or _default_metrics_sink
        if sink is None:
            return
        try:
            from datetime import date

            sink(self.settings, f"moa-{date.today().isoformat()}", "moa",
                 {**stats, "duration_s": round(duration_s, 2)})
        except Exception:
            import logging

            logging.getLogger("assistant").debug("moa metrics failed", exc_info=True)

    def _mixture_run(self, content, system: str | None, max_tokens: int,
                     role: str | None, stats: dict) -> str:
        """Mixture-of-Agents: every member model proposes an answer in parallel,
        then the aggregator synthesizes them into one (Wang et al. 2024). With
        `layers` > 1 each further round of proposers refines against the last
        round's answers before the final aggregation. A member that errors is
        dropped as long as one proposal survives. `stats` (mutated in place)
        feeds the `mixture` span and the `moa` metrics row.

        **Chat latency bound**: for the interactive `chat` role, a proposer
        slower than `moa_chat_proposer_timeout_s` is abandoned once at least one
        proposal is in — a degraded provider must not stall a chat turn for
        minutes (2026-07-17 noon incident: an 8-minute turn outlived the bridge
        wait). Offline roles (pipeline/research/task/evolve) keep waiting for
        every proposer — there, quality beats latency."""
        import contextvars
        import logging
        from concurrent.futures import ThreadPoolExecutor, wait

        members = self.mixture["members"]
        agg = self.mixture.get("aggregator") or members[0]
        layers = max(1, int(self.mixture.get("layers", 1)))
        configured_timeout = self.settings.moa_chat_proposer_timeout_s
        timeout_s = (min(float(configured_timeout), _INTERACTIVE_TIMEOUT_S)
                     if role == "chat" and configured_timeout > 0
                     else 0)

        log_ = logging.getLogger("assistant")
        threshold = self.settings.moa_member_fail_threshold
        cooldown = self.settings.moa_member_cooldown_s
        # call-local failure memory: scopes that ALREADY failed in this call —
        # the fallback chain must not hand a just-dead endpoint a fresh retry
        # window even though the cross-call breaker needs `threshold` failures
        call_failed: set = set()

        def propose(member, scopes, gens, claimed, layer_input):
            try:
                client = self._client(member.get("base_url"), member.get("api_key"))
                out = self._call(client, member["model"], layer_input, system,
                                 max_tokens, span_attrs={
                                     "mixture_stage": "proposer",
                                     "mixture_role": role or ""},
                                 route_scopes=scopes,
                                 timeout_s=_request_timeout_s(role))
                _breaker_record(scopes, gens, claimed,
                                "ok" if out.strip() else None, threshold, cooldown)
                if out.strip():
                    return out, False
                return None, True                   # empty = failed proposal
            except Exception as exc:
                cls = _classify_failure(exc)
                _breaker_record(scopes, gens, claimed, cls, threshold, cooldown)
                call_failed.add(scopes[1])            # this model route
                if cls == "prov":
                    call_failed.add(scopes[0])        # whole endpoint+credential
                log_.warning(
                    "mixture proposer %s failed (%s, status=%s)",
                    _bounded_route_label(member.get("model")),
                    _bounded_error_type(exc), _status_code(exc))
                return None, True

        responses: list[str] = []
        for _ in range(layers):
            layer_input = content if not responses else _augment(content, responses)
            # breaker partition: run closed routes + at most one half-open probe
            # per route; cooling routes are skipped (never blanket-retried)
            runnable = []
            for m in members:
                scopes = _route_scopes(self.settings, m.get("base_url"),
                                       m.get("api_key"), m["model"])
                quarantine_scope = self._quarantine_scope(scopes)
                if quarantine_scope:
                    log_.warning("mixture: skipping %s (durable %s route "
                                 "quarantine)",
                                 _bounded_route_label(m.get("model")),
                                 quarantine_scope)
                    stats["members_skipped"] += 1
                    continue
                mode, gens, claimed = _breaker_check(scopes, cooldown)
                if mode == "open":
                    log_.warning("mixture: skipping %s (provider cooling down "
                                 "after repeated failures)",
                                 _bounded_route_label(m.get("model")))
                    stats["members_skipped"] += 1
                    continue
                runnable.append((m, scopes, gens, claimed))
            if not runnable:
                if responses:
                    break                 # keep the prior layer's proposals
                return self._mixture_fallback(content, system, max_tokens, role,
                                              agg, call_failed, log_, stats)
            stats["members_attempted"] += len(runnable)
            # Propagate the current context into each worker (a raw pool thread
            # starts with a fresh context, which would drop the ContextVar-scoped
            # tracer — so proposer llm spans would vanish). One copy per member,
            # captured here in the calling thread. (tracing.py, DESIGN §3.)
            ctxs = [contextvars.copy_context() for _ in runnable]
            if timeout_s > 0:
                # Bounded wait: collect what finished inside the window; if
                # nothing did, fail at that same outer bound. Abandoned threads
                # finish in the background (can't be killed) and record their
                # own eventual outcome — generation-guarded, so a stale result
                # can't flip a newer breaker state; the abandonment itself
                # counts nothing.
                ex = ThreadPoolExecutor(max_workers=min(8, len(runnable)))
                futs = [ex.submit(ctx.run, propose, m, sc, ge, cl, layer_input)
                        for ctx, (m, sc, ge, cl) in zip(ctxs, runnable)]
                done, pending = wait(futs, timeout=timeout_s)
                outcomes = [f.result() for f in done]
                timed_out_without_answer = (
                    bool(pending)
                    and not any(out for out, _failed in outcomes))
                for f in pending:
                    f.cancel()
                ex.shutdown(wait=False)
                if pending:
                    stats["abandoned"] += len(pending)
                    log_.warning("mixture: abandoned %d proposer(s) still running "
                                 "after %ds (chat latency bound)", len(pending), timeout_s)
                stats["members_failed"] += sum(
                    1 for _out, failed in outcomes if failed)
                fresh = [out for out, _failed in outcomes if out]
                if timed_out_without_answer:
                    if responses:
                        # A later refinement layer timing out must not discard
                        # the independent answers from the preceding layer.
                        break
                    stats["aggregator_skipped"] = 1
                    raise TimeoutError(
                        "all chat mixture proposers exceeded the latency bound")
            else:
                with ThreadPoolExecutor(max_workers=min(8, len(runnable))) as ex:
                    outcomes = list(ex.map(
                        lambda a: a[0].run(propose, a[1][0], a[1][1], a[1][2],
                                           a[1][3], layer_input),
                        zip(ctxs, runnable)))
                stats["members_failed"] += sum(
                    1 for _out, failed in outcomes if failed)
                fresh = [out for out, _failed in outcomes if out]
            stats["proposals_ok"] += len(fresh)
            if fresh:
                responses = fresh
            elif responses:
                break                     # later-layer failure keeps prior proposals
            else:
                return self._mixture_fallback(content, system, max_tokens, role,
                                              agg, call_failed, log_, stats)

        # `proposals_ok` is total successful proposer work across every layer;
        # health depends on how many independent answers actually reach the
        # final synthesis after later-layer failures narrow the set.
        stats["proposals_final"] = len(responses)
        # Synthesis needs at least two independent final answers. With only one,
        # an aggregator adds cost and a false appearance of consensus; return
        # the surviving proposal and make the degradation explicit in metrics.
        if len(responses) < 2:
            stats["aggregator_skipped"] = 1
            stats["fallback_used"] = 1
            log_.warning("mixture: fewer than two healthy final proposers — "
                         "skipping synthesis")
            return responses[0]
        # The aggregator is otherwise a single point of failure: if it dies
        # after every proposer succeeded, fall back to the first surviving
        # proposal (itself a complete answer to the original prompt) rather
        # than sinking the whole call — symmetric with dropping a dead proposer.
        agg_scopes = _route_scopes(self.settings, agg.get("base_url"),
                                   agg.get("api_key"), agg["model"])
        agg_quarantine = self._quarantine_scope(agg_scopes)
        if agg_quarantine:
            stats["aggregator_skipped"] = 1
            stats["fallback_used"] = 1
            log_.warning("mixture aggregator %s durably quarantined — returning "
                         "a proposer answer",
                         _bounded_route_label(agg.get("model")))
            return responses[0]
        agg_mode, agg_gens, agg_claimed = _breaker_check(agg_scopes, cooldown)
        if agg_mode == "open":
            stats["aggregator_skipped"] = 1
            stats["fallback_used"] = 1
            log_.warning("mixture aggregator %s cooling down — returning a "
                         "proposer answer",
                         _bounded_route_label(agg.get("model")))
            return responses[0]
        agg_client = self._client(agg.get("base_url"), agg.get("api_key"))
        stats["aggregator_attempted"] = 1
        try:
            synthesis = self._call(agg_client, agg["model"],
                                   _augment(content, responses), system, max_tokens,
                                   span_attrs={"mixture_stage": "aggregator",
                                               "mixture_role": role or ""},
                                   route_scopes=agg_scopes,
                                   timeout_s=_request_timeout_s(role))
            _breaker_record(agg_scopes, agg_gens, agg_claimed,
                            "ok" if synthesis.strip() else None, threshold, cooldown)
        except Exception as exc:
            stats["aggregator_failed"] = 1
            stats["fallback_used"] = 1
            _breaker_record(agg_scopes, agg_gens, agg_claimed,
                            _classify_failure(exc), threshold, cooldown)
            logging.getLogger("assistant").warning(
                "mixture aggregator %s failed (%s, status=%s) — returning a "
                "proposer answer", _bounded_route_label(agg.get("model")),
                _bounded_error_type(exc),
                _status_code(exc))
            return responses[0]
        # An empty synthesis is as useless as a raised one — a reasoning-model
        # aggregator that spends its whole budget on hidden thinking emits no
        # text. Don't hand back "" when a good proposal exists.
        if not synthesis.strip():
            stats["aggregator_failed"] = 1
            stats["fallback_used"] = 1
            logging.getLogger("assistant").warning(
                "mixture aggregator %s returned empty output — returning a "
                "proposer answer", _bounded_route_label(agg.get("model")))
            return responses[0]
        stats["aggregator_ok"] = 1
        return synthesis

    def _mixture_fallback(self, content, system, max_tokens, role, agg,
                          call_failed: set, log_, stats: dict | None = None) -> str:
        """Every proposer failed or was cooling — answer with ONE healthy model
        instead of failing the turn ("use other models"). Candidates in order:
        the aggregator, the role's configured route, the global default — each
        skipped if its model route or its provider scope already failed THIS
        call (no fresh retry window on a just-dead endpoint), duplicates a
        route already tried in this chain, or its breaker is open with no
        probe lease. Blank output = failed fallback (continue, uncounted).
        Exhausted → the original RuntimeError (genuinely nothing is up)."""
        if stats is not None:
            stats["fallback_used"] = 1
            stats["aggregator_skipped"] = 1
        threshold = self.settings.moa_member_fail_threshold
        cooldown = self.settings.moa_member_cooldown_s
        role_spec = self.roles.get(role) if role else None
        candidates = [(agg.get("base_url"), agg.get("api_key"),
                       agg.get("model"), "aggregator")]
        if isinstance(role_spec, dict) and role_spec.get("model"):
            candidates.append((role_spec.get("base_url"), role_spec.get("api_key"),
                               role_spec["model"], f"role:{role}"))
        candidates.append((None, None, self.default_model, "default"))

        tried: set = set()
        for base_url, api_key, model, label in candidates:
            if not model:
                continue
            scopes = _route_scopes(self.settings, base_url, api_key, model)
            prov_key, model_key = scopes
            if model_key in tried or model_key in call_failed \
                    or prov_key in call_failed:
                continue
            tried.add(model_key)
            if self._quarantine_scope(scopes):
                continue
            mode, gens, claimed = _breaker_check(scopes, cooldown)
            if mode == "open":
                continue
            log_.warning("mixture: all proposers failed/cooling — trying %s "
                         "(%s) directly", _bounded_route_label(model), label)
            try:
                out = self._call(self._client(base_url, api_key), model,
                                 content, system, max_tokens,
                                 span_attrs={"mixture_stage": "fallback",
                                             "mixture_role": role or ""},
                                 route_scopes=scopes,
                                 timeout_s=_request_timeout_s(role))
            except Exception as exc:
                cls = _classify_failure(exc)
                _breaker_record(scopes, gens, claimed, cls, threshold, cooldown)
                call_failed.add(model_key)
                if cls == "prov":
                    call_failed.add(prov_key)
                log_.warning("mixture fallback %s failed (%s, status=%s)",
                             _bounded_route_label(model),
                             _bounded_error_type(exc), _status_code(exc))
                continue
            if out.strip():
                _breaker_record(scopes, gens, claimed, "ok", threshold, cooldown)
                return out
            _breaker_record(scopes, gens, claimed, None, threshold, cooldown)
        raise RuntimeError("all mixture proposers failed")

    def complete_json(self, prompt: str, system: str | None = None, **kw):
        """Parse JSON with one stop-aware recovery attempt.

        A normal ``end_turn`` parse failure gets one same-budget repair prompt
        containing bounded parse feedback and the prior output. A truncated
        response gets one fresh attempt at twice the token budget, capped at
        16k. The cap is never retried at an identical budget; persistent/capped
        truncation raises ``StructuredOutputTruncatedError`` explicitly.
        """
        text = self.complete(prompt, system=system, **kw)
        try:
            return _parse_json(text)
        except ValueError as exc:
            stop_reason = getattr(text, "stop_reason", "") or ""
            retry_kw = dict(kw)
            if stop_reason == "max_tokens":
                current_budget = retry_kw.get("max_tokens", 4000)
                next_budget = min(_STRUCTURED_TOKEN_CAP, current_budget * 2)
                if current_budget >= _STRUCTURED_TOKEN_CAP \
                        or next_budget <= current_budget:
                    raise StructuredOutputTruncatedError(
                        "structured LLM response truncated at the 16000-token cap"
                    ) from exc
                retry_kw["max_tokens"] = next_budget
                retry_prompt = prompt
            else:
                retry_prompt = (
                    f"{prompt}\n\n[JSON repair feedback]\n"
                    f"The previous response could not be parsed: {exc}. "
                    "Respond again with ONLY valid JSON, no prose or code fences."
                    f"\n\n[Previous invalid response]\n{text}"
                )

            repaired = self.complete(retry_prompt, system=system, **retry_kw)
            try:
                return _parse_json(repaired)
            except ValueError as repair_exc:
                if getattr(repaired, "stop_reason", "") == "max_tokens":
                    raise StructuredOutputTruncatedError(
                        "structured LLM response remained truncated after one retry"
                    ) from repair_exc
                raise


_MOA_SYNTH = (
    "\n\n[Reference answers]\nSeveral models answered the request above. Synthesize "
    "them into ONE best response: keep what is correct and useful, discard errors, "
    "bias, and hallucination, and match EXACTLY the format the request requires "
    "(if it asks for JSON, reply with only that JSON). Do not mention the other "
    "answers.\n\n")


def _augment(content, responses: list[str]):
    """Append the aggregator's reference block (proposer answers) to the prompt
    content, preserving image blocks when content is a message list."""
    block = _MOA_SYNTH + "\n\n".join(f"[Answer {i + 1}]\n{r}"
                                     for i, r in enumerate(responses))
    if isinstance(content, list):
        return content + [{"type": "text", "text": block}]
    return content + block


def _image_block(path: str) -> dict:
    """Anthropic base64 image content block for a local image file."""
    import base64

    from pathlib import Path

    from assistant.platform.vision import media_type_for

    return {"type": "image",
            "source": {"type": "base64",
                       "media_type": media_type_for(path) or "image/png",
                       "data": base64.b64encode(Path(path).read_bytes()).decode()}}


def _parse_json(text: str):
    """Best-effort extraction of a JSON object/array from a model response.

    Tolerates the common ways models wrap JSON: strips a ```json fence, seeks
    the first ``{`` or ``[``, then shrinks the tail until ``json.loads``
    succeeds (handling trailing prose). Raises ValueError if nothing parses."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    if start < 0:
        raise ValueError("no JSON object or array found in response")
    for end in range(len(text), start, -1):
        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError:
            continue
    raise ValueError("unparseable JSON in response")
