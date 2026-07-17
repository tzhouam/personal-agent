"""Thin Anthropic client wrapper for the agent.

Exports the ``LLM`` class: a traced ``messages.create`` call with exponential
retry on transient API errors and a JSON-coercing convenience method. Keeps
every call site provider-agnostic and degrade-friendly.
"""

import hashlib
import json
import re
import threading
import time as _time

import anthropic
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .config import Settings

_RETRYABLE = (
    anthropic.APIConnectionError,
    anthropic.RateLimitError,
    anthropic.InternalServerError,
)


_CHEAP_ROLES = frozenset({"cheap", "bulk", "research", "score"})


# ── provider circuit breaker (module-level: LLM is rebuilt per request) ──────
#
# A provider that is down must not cost every turn a fresh 40-60s retry window
# (2026-07-17 noon incident). Failures are tracked at two scopes per RESOLVED
# route — transport/auth failures poison the provider+credential (every model
# on it), 5xx only the one model:
#     ("prov",  resolved_base_url, cred_fp)
#     ("model", resolved_base_url, cred_fp, model)
# cred_fp = full sha256 of the resolved api key (in-memory only, never logged),
# so different tenants' credentials on the same endpoint never poison each
# other. State machine is generation-guarded: every recorded outcome carries
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


def _route_scopes(settings: Settings, base_url, api_key, model) -> tuple:
    """(provider_key, model_key) for the RESOLVED route — blanks resolve to the
    settings defaults exactly like `_client` does, so an omitted and an explicit
    default URL/key are the same route."""
    url = base_url or settings.anthropic_base_url or "<anthropic>"
    key = api_key or settings.anthropic_api_key or ""
    fp = hashlib.sha256(str(key).encode()).hexdigest()
    return ("prov", url, fp), ("model", url, fp, str(model))


def _classify_failure(exc) -> str | None:
    """Which breaker scope a failure trips: 'prov' (endpoint+credential dead —
    transport, timeout, 429, auth, or a connection-reset wrapped in a 400),
    'model' (5xx — one overloaded model), or None (request-specific/unknown —
    programming and validation errors must never poison a route)."""
    if isinstance(exc, anthropic.APIConnectionError):   # includes timeouts
        return "prov"
    status = getattr(exc, "status_code", None)
    if status in (401, 403, 429):
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

    def __init__(self, settings: Settings):
        """Cache the default provider, model tiers, the role map, and a lazy
        per-provider client cache."""
        self.settings = settings
        self.default_model = settings.anthropic_model
        self.cheap_model = settings.cheap_model
        self.roles: dict = settings.llm_roles or {}
        self.mixture: dict = settings.llm_mixture or {}
        # roles that run Mixture-of-Agents when >=2 members are configured;
        # defaults to the offline, quality-sensitive roles (interactive chat is
        # opt-in, since MoA ~doubles latency)
        self._mixture_roles: set = (
            set(self.mixture.get("roles") or ["pipeline", "research", "task", "evolve"])
            if len(self.mixture.get("members", [])) >= 2 else set())
        self._clients: dict = {}
        self.client = self._client(settings.anthropic_base_url,
                                   settings.anthropic_api_key)

    def _client(self, base_url: str | None, api_key: str | None):
        """Return an Anthropic client for (base_url, key), building and caching
        one per distinct provider; blanks fall back to the default provider."""
        base_url = base_url or self.settings.anthropic_base_url
        api_key = api_key or self.settings.anthropic_api_key
        cache_key = (base_url, api_key)
        if cache_key not in self._clients:
            kwargs: dict = {"api_key": api_key}
            if base_url:
                kwargs["base_url"] = base_url
            self._clients[cache_key] = anthropic.Anthropic(**kwargs)
        return self._clients[cache_key]

    def _resolve(self, role: str | None, model: str | None):
        """Map ``role``/``model`` to a concrete (client, model_id). An explicit
        ``model`` wins on the default provider; a configured role uses its
        model + optional provider override; an unconfigured role falls back to
        the cheap or default model on the default provider."""
        if model:
            return self.client, model
        spec = self.roles.get(role) if role else None
        if isinstance(spec, dict) and spec.get("model"):
            return (self._client(spec.get("base_url"), spec.get("api_key")),
                    spec["model"])
        if role in _CHEAP_ROLES:
            return self.client, self.cheap_model
        return self.client, self.default_model

    def complete(
        self,
        prompt: str,
        system: str | None = None,
        model: str | None = None,
        max_tokens: int = 4000,
        images: list[str] | None = None,
        role: str | None = None,
    ) -> str:
        """Send one user ``prompt`` (optional ``system``) and return the
        concatenated text blocks. ``role`` selects the model+provider via the
        role map (e.g. "chat", "research", "task"); an explicit ``model``
        overrides it on the default provider; both default to ``default_model``.
        ``images`` are local file paths attached as image content blocks before
        the text — only meaningful on a multimodal model. Each underlying API
        call (``_call``) is traced, retried on transient errors, and logs a
        warning — but does not raise — when the response is cut off at
        ``max_tokens``. Retry lives on ``_call`` (not here) so a mixture's
        proposers and aggregator each get their own retry rather than being
        dropped on the first blip, and an aggregator retry doesn't re-run every
        proposer."""
        content: str | list = prompt
        if images:
            content = [_image_block(p) for p in images] + [
                {"type": "text", "text": prompt}]
        if model is None and role and role in self._mixture_roles \
                and len(self.mixture.get("members", [])) >= 2:
            return self._mixture(content, system, max_tokens, role=role)
        client, model_id = self._resolve(role, model)
        return self._call(client, model_id, content, system, max_tokens)

    @retry(
        retry=retry_if_exception_type(_RETRYABLE),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, max=30),
        reraise=True,
    )
    def _call(self, client, model_id: str, content, system: str | None,
              max_tokens: int) -> str:
        """One traced ``messages.create`` returning the concatenated text; the
        shared core of the single-model and mixture paths. Retried on transient
        errors here (rather than on ``complete``) so each mixture proposer and
        the aggregator retry independently.

        Appends the temporal anchor to the TAIL of the user content — the
        model's only reliable clock. Tail placement adds nothing before any
        existing token, so the cacheable prompt prefix (system + stable prompt
        heads) stays byte-identical; never into ``system`` (that would bust the
        static prefix every request). List content is copied, never mutated —
        the mixture path passes one shared list to every proposer."""
        from .timeutil import temporal_anchor

        anchor = temporal_anchor()
        if isinstance(content, str):
            content = content + "\n\n" + anchor
        else:
            content = [*content, {"type": "text", "text": anchor}]
        kwargs: dict = {"model": model_id, "max_tokens": max_tokens,
                        "messages": [{"role": "user", "content": content}]}
        if system:
            kwargs["system"] = system
        from . import tracing

        with tracing.span("llm", model=model_id, max_tokens=max_tokens) as _sp:
            resp = client.messages.create(**kwargs)
            tracing.set_usage(_sp, getattr(resp, "usage", None),
                              stop_reason=getattr(resp, "stop_reason", "") or "")
        if resp.stop_reason == "max_tokens":
            import logging

            logging.getLogger("assistant").warning(
                "LLM response truncated at max_tokens=%s — raise the budget for this call",
                max_tokens)
        return "".join(b.text for b in resp.content if b.type == "text")

    def _mixture(self, content, system: str | None, max_tokens: int,
                 role: str | None = None) -> str:
        """Mixture-of-Agents: every member model proposes an answer in parallel,
        then the aggregator synthesizes them into one (Wang et al. 2024). With
        `layers` > 1 each further round of proposers refines against the last
        round's answers before the final aggregation. A member that errors is
        dropped as long as one proposal survives.

        **Chat latency bound**: for the interactive `chat` role, a proposer
        slower than `moa_chat_proposer_timeout_s` is abandoned once at least one
        proposal is in — a degraded provider must not stall a chat turn for
        minutes (2026-07-17 noon incident: an 8-minute turn outlived the bridge
        wait). Offline roles (pipeline/research/task/evolve) keep waiting for
        every proposer — there, quality beats latency."""
        import contextvars
        import logging
        from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

        members = self.mixture["members"]
        agg = self.mixture.get("aggregator") or members[0]
        layers = max(1, int(self.mixture.get("layers", 1)))
        timeout_s = (self.settings.moa_chat_proposer_timeout_s
                     if role == "chat" else 0)

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
                out = self._call(client, member["model"], layer_input, system, max_tokens)
                _breaker_record(scopes, gens, claimed,
                                "ok" if out.strip() else None, threshold, cooldown)
                return out if out.strip() else None   # empty = dropped, uncounted
            except Exception as exc:
                cls = _classify_failure(exc)
                _breaker_record(scopes, gens, claimed, cls, threshold, cooldown)
                call_failed.add(scopes[1])            # this model route
                if cls == "prov":
                    call_failed.add(scopes[0])        # whole endpoint+credential
                log_.warning("mixture proposer %s failed: %s", member.get("model"), exc)
                return None

        responses: list[str] = []
        for _ in range(layers):
            layer_input = content if not responses else _augment(content, responses)
            # breaker partition: run closed routes + at most one half-open probe
            # per route; cooling routes are skipped (never blanket-retried)
            runnable = []
            for m in members:
                scopes = _route_scopes(self.settings, m.get("base_url"),
                                       m.get("api_key"), m["model"])
                mode, gens, claimed = _breaker_check(scopes, cooldown)
                if mode == "open":
                    log_.warning("mixture: skipping %s (provider cooling down "
                                 "after repeated failures)", m.get("model"))
                    continue
                runnable.append((m, scopes, gens, claimed))
            if not runnable:
                if responses:
                    break                 # keep the prior layer's proposals
                return self._mixture_fallback(content, system, max_tokens, role,
                                              agg, call_failed, log_)
            # Propagate the current context into each worker (a raw pool thread
            # starts with a fresh context, which would drop the ContextVar-scoped
            # tracer — so proposer llm spans would vanish). One copy per member,
            # captured here in the calling thread. (tracing.py, DESIGN §3.)
            ctxs = [contextvars.copy_context() for _ in runnable]
            if timeout_s > 0:
                # Bounded wait: collect what finished inside the window; if
                # nothing did, wait for the FIRST completion (the provider SDK's
                # own timeouts bound that). Abandoned threads finish in the
                # background (can't be killed) and record their own eventual
                # outcome — generation-guarded, so a stale result can't flip a
                # newer breaker state; the abandonment itself counts nothing.
                ex = ThreadPoolExecutor(max_workers=min(8, len(runnable)))
                futs = [ex.submit(ctx.run, propose, m, sc, ge, cl, layer_input)
                        for ctx, (m, sc, ge, cl) in zip(ctxs, runnable)]
                done, pending = wait(futs, timeout=timeout_s)
                results = [f.result() for f in done]
                while not any(results) and pending:
                    done, pending = wait(pending, return_when=FIRST_COMPLETED)
                    results += [f.result() for f in done]
                for f in pending:
                    f.cancel()
                ex.shutdown(wait=False)
                if pending:
                    log_.warning("mixture: abandoned %d proposer(s) still running "
                                 "after %ds (chat latency bound)", len(pending), timeout_s)
                fresh = [r for r in results if r]
            else:
                with ThreadPoolExecutor(max_workers=min(8, len(runnable))) as ex:
                    fresh = [r for r in ex.map(
                        lambda a: a[0].run(propose, a[1][0], a[1][1], a[1][2],
                                           a[1][3], layer_input),
                        zip(ctxs, runnable)) if r]
            if fresh:
                responses = fresh
            elif responses:
                break                     # later-layer failure keeps prior proposals
            else:
                return self._mixture_fallback(content, system, max_tokens, role,
                                              agg, call_failed, log_)

        # The aggregator is otherwise a single point of failure: if it dies
        # after every proposer succeeded, fall back to the first surviving
        # proposal (itself a complete answer to the original prompt) rather
        # than sinking the whole call — symmetric with dropping a dead proposer.
        agg_scopes = _route_scopes(self.settings, agg.get("base_url"),
                                   agg.get("api_key"), agg["model"])
        agg_mode, agg_gens, agg_claimed = _breaker_check(agg_scopes, cooldown)
        if agg_mode == "open":
            log_.warning("mixture aggregator %s cooling down — returning a "
                         "proposer answer", agg.get("model"))
            return responses[0]
        agg_client = self._client(agg.get("base_url"), agg.get("api_key"))
        try:
            synthesis = self._call(agg_client, agg["model"],
                                   _augment(content, responses), system, max_tokens)
            _breaker_record(agg_scopes, agg_gens, agg_claimed,
                            "ok" if synthesis.strip() else None, threshold, cooldown)
        except Exception as exc:
            _breaker_record(agg_scopes, agg_gens, agg_claimed,
                            _classify_failure(exc), threshold, cooldown)
            logging.getLogger("assistant").warning(
                "mixture aggregator %s failed (%s) — returning a proposer answer",
                agg.get("model"), exc)
            return responses[0]
        # An empty synthesis is as useless as a raised one — a reasoning-model
        # aggregator that spends its whole budget on hidden thinking emits no
        # text. Don't hand back "" when a good proposal exists.
        if not synthesis.strip():
            logging.getLogger("assistant").warning(
                "mixture aggregator %s returned empty output — returning a "
                "proposer answer", agg.get("model"))
            return responses[0]
        return synthesis

    def _mixture_fallback(self, content, system, max_tokens, role, agg,
                          call_failed: set, log_) -> str:
        """Every proposer failed or was cooling — answer with ONE healthy model
        instead of failing the turn ("use other models"). Candidates in order:
        the aggregator, the role's configured route, the global default — each
        skipped if its model route or its provider scope already failed THIS
        call (no fresh retry window on a just-dead endpoint), duplicates a
        route already tried in this chain, or its breaker is open with no
        probe lease. Blank output = failed fallback (continue, uncounted).
        Exhausted → the original RuntimeError (genuinely nothing is up)."""
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
            mode, gens, claimed = _breaker_check(scopes, cooldown)
            if mode == "open":
                continue
            log_.warning("mixture: all proposers failed/cooling — trying %s "
                         "(%s) directly", model, label)
            try:
                out = self._call(self._client(base_url, api_key), model,
                                 content, system, max_tokens)
            except Exception as exc:
                cls = _classify_failure(exc)
                _breaker_record(scopes, gens, claimed, cls, threshold, cooldown)
                call_failed.add(model_key)
                if cls == "prov":
                    call_failed.add(prov_key)
                log_.warning("mixture fallback %s failed: %s", model, exc)
                continue
            if out.strip():
                _breaker_record(scopes, gens, claimed, "ok", threshold, cooldown)
                return out
            _breaker_record(scopes, gens, claimed, None, threshold, cooldown)
        raise RuntimeError("all mixture proposers failed")

    def complete_json(self, prompt: str, system: str | None = None, **kw):
        """One retry with error feedback if the first response isn't valid JSON."""
        text = self.complete(prompt, system=system, **kw)
        try:
            return _parse_json(text)
        except ValueError as exc:
            retry_prompt = (
                f"{prompt}\n\nYour previous response could not be parsed as JSON "
                f"({exc}). Respond again with ONLY valid JSON, no prose, no code fences."
            )
            return _parse_json(self.complete(retry_prompt, system=system, **kw))


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

    from .vision import media_type_for

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
