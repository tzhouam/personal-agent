"""Thin Anthropic client wrapper for the agent.

Exports the ``LLM`` class: a traced ``messages.create`` call with exponential
retry on transient API errors and a JSON-coercing convenience method. Keeps
every call site provider-agnostic and degrade-friendly.
"""

import json
import re

import anthropic
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .config import Settings

_RETRYABLE = (
    anthropic.APIConnectionError,
    anthropic.RateLimitError,
    anthropic.InternalServerError,
)


_CHEAP_ROLES = frozenset({"cheap", "bulk", "research", "score"})


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

    @retry(
        retry=retry_if_exception_type(_RETRYABLE),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, max=30),
        reraise=True,
    )
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
        the text — only meaningful on a multimodal model. The call is wrapped in
        a trace span recording usage/stop reason, retried on transient errors
        (via the decorator), and logs a warning — but does not raise — when the
        response is cut off at ``max_tokens``."""
        content: str | list = prompt
        if images:
            content = [_image_block(p) for p in images] + [
                {"type": "text", "text": prompt}]
        if model is None and role and role in self._mixture_roles \
                and len(self.mixture.get("members", [])) >= 2:
            return self._mixture(content, system, max_tokens)
        client, model_id = self._resolve(role, model)
        return self._call(client, model_id, content, system, max_tokens)

    def _call(self, client, model_id: str, content, system: str | None,
              max_tokens: int) -> str:
        """One traced ``messages.create`` returning the concatenated text; the
        shared core of the single-model and mixture paths."""
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

    def _mixture(self, content, system: str | None, max_tokens: int) -> str:
        """Mixture-of-Agents: every member model proposes an answer in parallel,
        then the aggregator synthesizes them into one (Wang et al. 2024). With
        `layers` > 1 each further round of proposers refines against the last
        round's answers before the final aggregation. A member that errors is
        dropped as long as one proposal survives."""
        import logging
        from concurrent.futures import ThreadPoolExecutor

        members = self.mixture["members"]
        agg = self.mixture.get("aggregator") or members[0]
        layers = max(1, int(self.mixture.get("layers", 1)))

        def propose(member, layer_input):
            try:
                client = self._client(member.get("base_url"), member.get("api_key"))
                return self._call(client, member["model"], layer_input, system, max_tokens)
            except Exception as exc:
                logging.getLogger("assistant").warning(
                    "mixture proposer %s failed: %s", member.get("model"), exc)
                return None

        responses: list[str] = []
        for _ in range(layers):
            layer_input = content if not responses else _augment(content, responses)
            with ThreadPoolExecutor(max_workers=min(8, len(members))) as ex:
                responses = [r for r in ex.map(lambda m: propose(m, layer_input), members) if r]
            if not responses:
                raise RuntimeError("all mixture proposers failed")

        agg_client = self._client(agg.get("base_url"), agg.get("api_key"))
        return self._call(agg_client, agg["model"], _augment(content, responses),
                          system, max_tokens)

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
